#!/usr/bin/env python3
"""git-agent — smart commit, push, and PR/MR tool.

Works standalone (with an LLM API key) or as a tool called by any AI agent.
Supports GitHub, GitLab, and Bitbucket.

Usage:  git-agent [OPTIONS] [CONTEXT...]
Docs:   https://github.com/keivanzavari/git-agent
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from typing import Optional

VERSION = "0.3.0"


try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()  # loads .env from cwd or any parent directory; existing vars are not overwritten
except ImportError:
    pass  # python-dotenv not installed — rely on environment variables set in the shell

HTTP_TIMEOUT = 30

# ── ANSI colours ──────────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


def info(msg: str) -> None:
    print(_c("34", "ℹ") + "  " + msg)


def success(msg: str) -> None:
    print(_c("32", "✓") + "  " + msg)


def warn(msg: str) -> None:
    print(_c("33;1", "⚠") + "  " + msg)


def die(msg: str, code: int = 1) -> None:
    print(_c("31", "✗") + "  " + msg, file=sys.stderr)
    sys.exit(code)


def header(msg: str) -> None:
    print("\n" + _c("1", msg))


# ── subprocess helpers ────────────────────────────────────────────────────────

def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def capture(cmd: list[str], *, default: str = "") -> str:
    """Run a command and return stripped stdout; return *default* on error."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else default


# ── argument parsing ──────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="git-agent",
        description=(
            "Generates a structured commit message, commits staged changes, "
            "pushes, and optionally opens a PR/MR. "
            "Works with GitHub, GitLab, and Bitbucket."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""\
ENVIRONMENT
  ANTHROPIC_API_KEY   Use Claude to generate commit message and PR body
  OPENAI_API_KEY      Use GPT-4o to generate commit message and PR body
  TICKET_PATTERN      Regex to extract the ticket/issue ID from the branch name.
                      Default: [A-Z]+-[0-9]+ (matches Jira, Linear, YouTrack, etc.)
                      Override for other systems, e.g.:
                        [0-9]+        GitHub / GitLab issue numbers
                        sc-[0-9]+     Shortcut
                        AB#[0-9]+     Azure DevOps
  TICKET_URL_TEMPLATE URL template for linking to a ticket in PR bodies.
                      Use {{id}} as the placeholder for the extracted ticket ID.
                      Example: https://yourorg.atlassian.net/browse/{{id}}  (Jira)
                               https://linear.app/myteam/issue/{{id}}       (Linear)
                               https://github.com/org/repo/issues/{{id}}    (GitHub)
                      When unset, the bare ticket ID is included as plain text.
  JIRA_BASE_URL       Deprecated. Equivalent to setting
                      TICKET_URL_TEMPLATE=$JIRA_BASE_URL/browse/{{id}}.
  BITBUCKET_SERVER_URL  Base URL of your Bitbucket Server instance. Used to
                        detect the platform for self-hosted installs and
                        forwarded to bb-cli for API calls.
                        Example: https://bitbucket.mycompany.com
  BITBUCKET_TOKEN     API token for bb-cli (HTTP access token from Bitbucket
                      Server: Personal Settings → HTTP access tokens).

EXAMPLES
  # Standalone — LLM generates the commit message
  git-agent
  git-agent --pr "AUTH-42: adds Google SSO via short-lived JWTs in httpOnly cookies"

  # Agent-driven — message already written by the calling agent
  git-agent --message "Add OAuth2 login via Google" --pr --yes

  # GitLab with draft MR
  git-agent --pr --draft --base develop

  # Commit only, no push
  git-agent --no-push
""",
    )
    p.add_argument("-m", "--message", metavar="MSG",
                   help="Use this commit message (skip LLM generation)")
    p.add_argument("--title", metavar="TITLE",
                   help="Override PR/MR title (defaults to commit title)")
    p.add_argument("--pr", action="store_true",
                   help="Open a PR/MR after pushing")
    p.add_argument("--draft", action="store_true",
                   help="Open a draft PR/MR (implies --pr)")
    p.add_argument("--base", metavar="BRANCH",
                   help="Base branch for PR/MR (default: auto-detected)")
    p.add_argument("--no-push", dest="no_push", action="store_true",
                   help="Commit only, skip push and PR")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip all confirmation prompts")
    p.add_argument("-v", "--version", action="version", version=f"git-agent {VERSION}")
    p.add_argument("context", nargs="*", metavar="CONTEXT",
                   help="Extra context passed to the LLM (ticket summary, decisions, etc.)")
    return p


# ── git helpers ───────────────────────────────────────────────────────────────

def ensure_git_repo() -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
    )
    if result.returncode != 0:
        die("Not inside a git repository.")


def staged_files() -> list[str]:
    return capture(["git", "diff", "--cached", "--name-only"]).splitlines()


def unstaged_files() -> list[str]:
    return capture(["git", "diff", "--name-only"]).splitlines()


def current_branch() -> str:
    return capture(["git", "branch", "--show-current"])


def diff_stat() -> str:
    return capture(["git", "diff", "--cached", "--stat"])


def full_diff() -> str:
    return capture(["git", "diff", "--cached"])


def recent_log(n: int = 5) -> str:
    return capture(["git", "log", "--oneline", f"-{n}"])


def _pr_title_from_log() -> str:
    """First line of the most recent commit subject."""
    return capture(["git", "log", "-1", "--format=%s"])


def default_base_branch() -> str:
    out = capture(["git", "remote", "show", "origin"])
    m = re.search(r"HEAD branch:\s*(\S+)", out)
    return m.group(1) if m else "main"


def remote_url() -> str:
    url = capture(["git", "remote", "get-url", "origin"])
    if not url:
        die("No remote 'origin' configured.")
    return url


# ── ticket ID extraction ──────────────────────────────────────────────────────

def resolve_ticket_url_template() -> str:
    """Return TICKET_URL_TEMPLATE, falling back to JIRA_BASE_URL for compat."""
    tmpl = os.environ.get("TICKET_URL_TEMPLATE", "")
    if not tmpl:
        jira_base = os.environ.get("JIRA_BASE_URL", "")
        if jira_base:
            tmpl = f"{jira_base}/browse/{{id}}"
    return tmpl


def extract_ticket_id(branch: str) -> str:
    pattern = os.environ.get("TICKET_PATTERN", r"[A-Z]+-[0-9]+")
    m = re.search(pattern, branch)
    return m.group(0) if m else ""


def ticket_url(ticket_id: str, template: str) -> str:
    """Return a Markdown link if template is set, otherwise the bare ID."""
    if not template:
        return ticket_id
    encoded = urllib.parse.quote(ticket_id, safe="")
    url = template.replace("{id}", encoded)
    return f"[{ticket_id}]({url})"


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        die(f"HTTP {exc.code} from {url}:\n{body}")
    except urllib.error.URLError as exc:
        die(f"Request to {url} failed: {exc.reason}")
    except json.JSONDecodeError as exc:
        die(f"Invalid JSON from {url}: {exc}")


def call_anthropic(prompt: str) -> str:
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    resp = _http_post("https://api.anthropic.com/v1/messages", payload, headers)
    return resp["content"][0]["text"].strip()


def call_openai(prompt: str) -> str:
    payload = {
        "model": "gpt-4o",
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        "content-type": "application/json",
    }
    resp = _http_post("https://api.openai.com/v1/chat/completions", payload, headers)
    return resp["choices"][0]["message"]["content"].strip()


def call_llm(prompt: str) -> Optional[str]:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return call_anthropic(prompt)
    if os.environ.get("OPENAI_API_KEY"):
        return call_openai(prompt)
    return None


# ── commit message generation ─────────────────────────────────────────────────

def commit_prompt(ticket_id: str, branch: str, log: str,
                  stat: str, diff: str, context: str) -> str:
    return f"""\
Generate a git commit message for the following staged changes.

Format:
  <title — imperative mood, ≤72 chars, no trailing period>

  - Bullet: WHY or WHAT changed (not just file names or line counts)
  - Bullet: non-obvious decisions or tradeoffs (omit if none)
  (maximum 3 bullets; omit body entirely if title is self-explanatory)

Rules:
  - Prefix title with ticket ID if present: "[PROJ-123] Title"
  - Imperative mood: "Add", "Fix", "Refactor" — never "Added" or "Adding"
  - Do NOT list file names or line counts — git show handles that
  - Match style of recent commit history if a pattern is evident

Ticket ID: {ticket_id or 'none'}
Context from caller: {context or 'none'}
Branch: {branch}
Recent history:
{log or 'none'}

Diff stat:
{stat}

Full diff:
{diff}

Reply with ONLY the commit message. No explanation, no markdown fences.
"""


def generate_commit_msg(ticket_id: str, branch: str, log: str,
                        stat: str, diff: str, context: str) -> str:
    prompt = commit_prompt(ticket_id, branch, log, stat, diff, context)
    msg = call_llm(prompt)
    if msg:
        return msg

    # No LLM — open $EDITOR or read from stdin
    prefix = f"[{ticket_id}] " if ticket_id else ""
    commented_stat = "\n".join(f"# {line}" for line in stat.splitlines())
    template = (
        f"{prefix}Short summary in imperative mood\n"
        f"# Staged changes:\n{commented_stat}\n"
        f"# Context: {context or 'none'}\n"
        "# Delete comment lines before saving.\n"
    )
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="git-agent-msg-", delete=False
        ) as tmp:
            tmp.write(template)
            tmp_path = tmp.name
        try:
            subprocess.run([*shlex.split(editor), tmp_path], check=True)
            with open(tmp_path) as f:
                edited = "\n".join(
                    line for line in f.read().splitlines()
                    if not line.startswith("#")
                ).strip()
        finally:
            os.unlink(tmp_path)
        if not edited:
            die("Empty commit message after editing. Aborted.")
        return edited
    else:
        warn("No LLM API key and no $EDITOR set. Enter commit message (Ctrl-D to finish):")
        msg = sys.stdin.read().strip()
        if not msg:
            die("Empty commit message. Aborted.")
        return msg


# ── PR body generation ────────────────────────────────────────────────────────

def pr_body_prompt(ticket_id: str, ticket_link: str, commit_msg: str,
                   stat: str, context: str) -> str:
    ticket_section = f"\n## Ticket\n{ticket_link}" if ticket_id else ""
    return f"""\
Write a GitHub/GitLab PR description for this change.

Format (use exactly these headings):
## Summary
2-4 bullet points: what this PR does and WHY

## Changes
Key implementation details or non-obvious decisions

## Testing
How to verify this works; what a reviewer should check
{ticket_section}

Commit message:
{commit_msg}

Diff stat:
{stat}

Extra context: {context or 'none'}

Reply with ONLY the PR body. No extra explanation.
"""


def generate_pr_body(ticket_id: str, ticket_link: str, commit_msg: str,
                     stat: str, context: str) -> str:
    prompt = pr_body_prompt(ticket_id, ticket_link, commit_msg, stat, context)
    body = call_llm(prompt)
    if body:
        return body

    # Fallback: structured template
    bullets = "\n".join(
        line for line in commit_msg.splitlines()[2:]
        if line.startswith("-")
    ) or "- See commit message"
    parts = [
        "## Summary\n",
        bullets,
        "\n## Changes\n",
        f"```\n{stat}\n```",
        "\n## Testing\n",
        "- Verify the changes work as expected",
    ]
    if context:
        parts += ["\n## Context\n", context]
    if ticket_id:
        parts += ["\n## Ticket\n", ticket_link]
    return "\n".join(parts)


# ── platform detection & PR creation ─────────────────────────────────────────

def detect_platform(url: str) -> str:
    if "github" in url:
        return "github"
    if "gitlab" in url:
        return "gitlab"
    if "bitbucket" in url:
        return "bitbucket"
    # Bitbucket Server: check if the remote URL hostname matches BITBUCKET_SERVER_URL
    server_url = os.environ.get("BITBUCKET_SERVER_URL", "")
    if server_url:
        server_host = urllib.parse.urlparse(server_url).hostname or ""
        if server_host and server_host in url:
            return "bitbucket"
    return "unknown"


def parse_remote_path(url: str) -> str:
    """Return the full repo path (owner[/groups]/repo) from any remote URL form."""
    for pattern in [
        r"ssh://[^@]*@?[^/]+/(.+?)(?:\.git)?$",   # ssh://
        r"https?://[^/]+/(.+?)(?:\.git)?$",         # https://
        r"[^@]*@[^:]+:(.+?)(?:\.git)?$",            # SCP-like git@host:path
    ]:
        m = re.match(pattern, url)
        if m:
            return m.group(1)
    return ""


def parse_bitbucket_server_path(url: str) -> tuple[str, str]:
    """Extract (project_key, repo_slug) from a Bitbucket Server remote URL.

    Handles all common remote URL forms:
      - SCP-like:  git@host:PROJECT/repo.git       → (PROJECT, repo)
      - HTTPS:     https://host/scm/PROJECT/repo.git → (PROJECT, repo)
      - SSH URI:   ssh://git@host:7999/PROJECT/repo.git → (PROJECT, repo)
    """
    path = parse_remote_path(url)                     # e.g. "scm/PROJECT/repo"
    parts = [p for p in path.split("/") if p]
    if parts and parts[0].lower() == "scm":
        parts = parts[1:]                             # strip leading scm/
    if len(parts) >= 2:
        return parts[-2], parts[-1]                   # (project_key, repo_slug)
    return "", ""


def create_github_pr(pr_title: str, pr_body: str, branch: str,
                     base: str, draft: bool) -> str:
    if not _cmd_exists("gh"):
        die("Install the gh CLI to create GitHub PRs: https://cli.github.com")
    args = ["gh", "pr", "create",
            "--title", pr_title, "--body", pr_body, "--base", base]
    if draft:
        args.append("--draft")
    return run(args, capture=True).stdout.strip()


def create_gitlab_mr(pr_title: str, pr_body: str, branch: str,
                     base: str, draft: bool) -> str:
    if not _cmd_exists("glab"):
        die("Install the glab CLI to create GitLab MRs: https://gitlab.com/gitlab-org/cli")
    args = ["glab", "mr", "create",
            "--title", pr_title, "--description", pr_body,
            "--target-branch", base, "--source-branch", branch]
    if draft:
        args.append("--draft")
    return run(args, capture=True).stdout.strip()


def create_bitbucket_pr(pr_title: str, pr_body: str, branch: str,
                        base: str, draft: bool) -> str:
    if _cmd_exists("bb"):
        project, repo = parse_bitbucket_server_path(remote_url())
        args = ["bb", "pr", "create",
                "--title", pr_title, "--description", pr_body,
                "--source", branch, "--target", base]
        if project:
            args += ["--project", project]
        if repo:
            args += ["--repo", repo]
        if draft:
            args.append("--draft")
        return run(args, capture=True).stdout.strip()
    elif _cmd_exists("bkt"):
        if draft:
            warn("bkt does not support draft PRs; creating a regular PR.")
        if pr_body:
            warn("bkt does not support setting a PR description; body will not be included.")
        args = ["bkt", "pr", "create",
                "--title", pr_title, "--source", branch, "--target", base]
        return run(args, capture=True).stdout.strip()
    else:
        die("Install bb (bb-cli) to create Bitbucket PRs.")


def _cmd_exists(name: str) -> bool:
    import shutil
    return shutil.which(name) is not None


# ── PR comment retrieval ──────────────────────────────────────────────────────

def get_github_pr_comments() -> dict:
    """Return comments and reviews on the current branch's GitHub PR.

    Requires the ``gh`` CLI. Returns ``{"pr_number": None, "comments": []}``
    when no open PR exists for this branch.
    """
    if not _cmd_exists("gh"):
        die("Install the gh CLI to read GitHub PR comments: https://cli.github.com")
    raw = capture(["gh", "pr", "view", "--json", "number,comments,reviews"])
    if not raw:
        return {"pr_number": None, "comments": []}
    data = json.loads(raw)
    comments = [
        {
            "author":     c["author"]["login"],
            "body":       c["body"],
            "created_at": c["createdAt"],
            "state":      "",
        }
        for c in data.get("comments", [])
    ]
    reviews = [
        {
            "author":     r["author"]["login"],
            "body":       r["body"],
            "created_at": r["submittedAt"],
            "state":      r["state"],
        }
        for r in data.get("reviews", [])
        if r.get("body")  # skip empty review submissions (e.g. approve-without-comment)
    ]
    return {"pr_number": data.get("number"), "comments": comments + reviews}


def get_gitlab_mr_comments() -> dict:
    """Return notes (comments) on the current branch's GitLab MR.

    Requires the ``glab`` CLI. Returns ``{"pr_number": None, "comments": []}``
    when no open MR exists for this branch.
    """
    if not _cmd_exists("glab"):
        die("Install the glab CLI to read GitLab MR comments: https://gitlab.com/gitlab-org/cli")
    raw = capture(["glab", "mr", "view", "--output", "json"])
    if not raw:
        return {"pr_number": None, "comments": []}
    data = json.loads(raw)
    notes = data.get("notes", [])
    comments = [
        {
            "author":     n.get("author", {}).get("username", ""),
            "body":       n.get("body", ""),
            "created_at": n.get("created_at", ""),
            "state":      "",
        }
        for n in (notes if isinstance(notes, list) else [])
    ]
    return {"pr_number": data.get("iid"), "comments": comments}


def get_bitbucket_pr_comments() -> dict:
    """Return comments on the open PR for the current branch using bb-cli.

    Requires the ``bb`` CLI (bb-cli). Returns ``{"pr_number": None, "comments": []}``
    when bb is not available or no open PR exists for this branch.
    """
    if not _cmd_exists("bb"):
        warn("bb (bb-cli) is not installed; cannot list Bitbucket PR comments.")
        return {"pr_number": None, "comments": []}

    branch = current_branch()

    # Find the open PR for the current branch via bb pr list --json
    raw = capture(["bb", "pr", "list", "--json"])
    if not raw:
        return {"pr_number": None, "comments": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        warn("Could not parse bb pr list output.")
        return {"pr_number": None, "comments": []}

    # bb pr list --json returns {pullRequests: [{id, fromBranch, ...}], ...}
    pr = next(
        (p for p in data.get("pullRequests", []) if p.get("fromBranch") == branch),
        None,
    )
    if not pr:
        return {"pr_number": None, "comments": []}

    pr_id = pr["id"]

    # Fetch comments for the found PR
    raw = capture(["bb", "pr", "comments", str(pr_id), "--json"])
    if not raw:
        return {"pr_number": pr_id, "comments": []}
    try:
        cdata = json.loads(raw)
    except json.JSONDecodeError:
        warn("Could not parse bb pr comments output.")
        return {"pr_number": pr_id, "comments": []}

    # bb pr comments --json passes through the raw Bitbucket API response:
    # {values: [{text, author: {displayName, name}, createdDate, state, deleted}]}
    comments = [
        {
            "author":     c.get("author", {}).get("displayName") or c.get("author", {}).get("name", ""),
            "body":       c.get("text", ""),
            "created_at": str(c.get("createdDate", "")),
            "state":      c.get("state", ""),
        }
        for c in cdata.get("values", [])
        if not c.get("deleted") and c.get("text")
    ]
    return {"pr_number": pr_id, "comments": comments}


# ── PR/MR update ──────────────────────────────────────────────────────────────

def update_github_pr(title: str = "", body: str = "", base: str = "",
                     draft: Optional[bool] = None) -> str:
    """Update the open PR for the current branch on GitHub.

    Uses ``gh pr edit`` for title/body/base changes and
    ``gh pr ready`` / ``gh pr ready --undo`` for draft toggling.
    Requires the ``gh`` CLI.
    """
    if not _cmd_exists("gh"):
        die("Install the gh CLI to update GitHub PRs: https://cli.github.com")

    result_parts = []

    edit_args = ["gh", "pr", "edit"]
    if title:
        edit_args += ["--title", title]
    if body:
        edit_args += ["--body", body]
    if base:
        edit_args += ["--base", base]

    if len(edit_args) > 3:
        out = run(edit_args, capture=True).stdout.strip()
        if out:
            result_parts.append(out)

    if draft is True:
        out = run(["gh", "pr", "ready", "--undo"], capture=True).stdout.strip()
        if out:
            result_parts.append(out)
    elif draft is False:
        out = run(["gh", "pr", "ready"], capture=True).stdout.strip()
        if out:
            result_parts.append(out)

    return "\n".join(result_parts) if result_parts else "PR updated."


def update_gitlab_mr(title: str = "", body: str = "", base: str = "",
                     draft: Optional[bool] = None) -> str:
    """Update the open MR for the current branch on GitLab.

    Uses a single ``glab mr update`` call. Requires the ``glab`` CLI.
    """
    if not _cmd_exists("glab"):
        die("Install the glab CLI to update GitLab MRs: https://gitlab.com/gitlab-org/cli")

    args = ["glab", "mr", "update"]
    if title:
        args += ["--title", title]
    if body:
        args += ["--description", body]
    if base:
        args += ["--target-branch", base]
    if draft is True:
        args.append("--draft")
    if draft is False:
        args.append("--ready")

    if len(args) == 3:
        return "Nothing to update."

    return run(args, capture=True).stdout.strip() or "MR updated."


def update_bitbucket_pr(title: str = "", body: str = "", base: str = "",
                        draft: Optional[bool] = None) -> str:
    """Update the open PR for the current branch on Bitbucket Server.

    Finds the PR ID via ``bb pr list --json``, then calls ``bb pr update``.
    Requires the ``bb`` CLI (bb-cli).
    """
    if not _cmd_exists("bb"):
        die("Install bb (bb-cli) to update Bitbucket PRs.")

    branch = current_branch()
    raw = capture(["bb", "pr", "list", "--json"])
    if not raw:
        die("Could not retrieve PR list. Check bb configuration.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        die("Could not parse bb pr list output.")

    pr = next(
        (p for p in data.get("pullRequests", []) if p.get("fromBranch") == branch),
        None,
    )
    if not pr:
        die(f"No open PR found for branch '{branch}'.")

    pr_id = pr["id"]
    project, repo = parse_bitbucket_server_path(remote_url())

    args = ["bb", "pr", "update", str(pr_id)]
    if title: args += ["--title", title]
    if body:  args += ["--description", body]
    if base:  args += ["--target", base]
    if draft is True:  args.append("--draft")
    if draft is False: args.append("--ready")
    if project: args += ["--project", project]
    if repo:    args += ["--repo", repo]

    if len(args) == 4:  # only "bb pr update <id>" with no changes
        return "Nothing to update."

    return run(args, capture=True).stdout.strip() or "PR updated."


def update_pr(title: str = "", body: str = "", base: str = "",
              draft: Optional[bool] = None) -> str:
    """Update the open PR/MR for the current branch on the auto-detected platform."""
    rem_url = remote_url()
    platform = detect_platform(rem_url)

    if platform == "github":
        return update_github_pr(title, body, base, draft)
    elif platform == "gitlab":
        return update_gitlab_mr(title, body, base, draft)
    elif platform == "bitbucket":
        return update_bitbucket_pr(title, body, base, draft)
    else:
        raise ValueError(
            f"Unsupported platform for remote URL: {rem_url!r}. "
            "Supported: github.com, gitlab.com, bitbucket.org / Bitbucket Server"
        )


# ── readline + REPL setup ────────────────────────────────────────────────────

try:
    import readline as _readline
    _READLINE_AVAILABLE = True
except ImportError:
    _READLINE_AVAILABLE = False

HISTORY_PATH = os.path.expanduser("~/.git_agent_history")

HELP_TEXT = """\
Built-in commands:
  add [args]      Stage files (no args → interactive patch via git add -p)
  commit [msg]    Commit staged changes; LLM-generated message if none given
  create          Create a PR/MR for the current branch
  update          Update the open PR/MR for the current branch
  git <cmd>       Pass any git subcommand through (e.g. git log --oneline -5)
  help            Show this help
  exit / quit     Exit the console (also Ctrl-D)"""


# ── interactive confirmation ──────────────────────────────────────────────────

def confirm(prompt: str, *, default_yes: bool = True) -> bool:
    hint = "[Y/n]" if default_yes else "[y/N]"
    ans = input(f"{prompt} {hint} ").strip().lower()
    if ans == "":
        return default_yes
    return ans == "y"


# ── interactive REPL console ──────────────────────────────────────────────────

class GitConsole:
    def __init__(self) -> None:
        self._running = False
        self._branch: str = ""
        if _READLINE_AVAILABLE:
            _readline.set_completer(self._completer)
            _readline.parse_and_bind("tab: complete")
            try:
                _readline.read_history_file(HISTORY_PATH)
            except FileNotFoundError:
                pass
            _readline.set_history_length(500)

    def _completer(self, text: str, state: int):
        commands = ["add", "commit", "create", "update", "git", "help", "exit", "quit"]
        matches = [c for c in commands if c.startswith(text)]
        return matches[state] if state < len(matches) else None

    @property
    def prompt(self) -> str:
        b = self._branch or "detached"
        return f"({_c('36', b)}) git> "

    def run(self) -> None:
        self._running = True
        info("git-agent console  (type 'help' for commands, Ctrl-D to exit)")
        while self._running:
            self._branch = current_branch()
            try:
                line = input(self.prompt).strip()
            except KeyboardInterrupt:
                print(); continue
            except EOFError:
                print(); break
            if not line:
                continue
            try:
                self._dispatch(line)
            except SystemExit:
                pass
        if _READLINE_AVAILABLE:
            _readline.write_history_file(HISTORY_PATH)
        success("Goodbye.")

    def _dispatch(self, line: str) -> None:
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if cmd in ("exit", "quit"):
            self._running = False
        elif cmd == "help":
            print(HELP_TEXT)
        elif cmd == "add":
            self._cmd_add(rest)
        elif cmd == "commit":
            self._cmd_commit(rest)
        elif cmd == "create":
            self._cmd_create()
        elif cmd == "update":
            self._cmd_update()
        elif cmd == "git":
            self._passthrough(rest)
        else:
            warn(f"Unknown command: {cmd!r}. Type 'help' for available commands.")

    def _cmd_add(self, args: str) -> None:
        if args:
            subprocess.run(["git", "add"] + shlex.split(args))
        else:
            subprocess.run(["git", "add", "-p"])
        stat = diff_stat()
        if stat:
            print(stat)

    def _cmd_commit(self, message: str) -> None:
        files = staged_files()
        if not files:
            warn("Nothing staged. Use `add` first.")
            return

        if message:
            commit_msg = message
        else:
            header("Generating commit message...")
            branch = self._branch
            ticket_id = extract_ticket_id(branch)
            stat = diff_stat()
            diff = full_diff()
            log = recent_log()
            commit_msg = generate_commit_msg(ticket_id, branch, log, stat, diff, "")

        header("Proposed commit message:")
        print()
        print(commit_msg)
        print()

        ans = input("Commit with this message? [Y/n/edit] ").strip().lower()
        if ans in ("n", "no"):
            info("Aborted.")
            return
        if ans in ("e", "edit"):
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="git-agent-msg-", delete=False
            ) as tmp:
                tmp.write(commit_msg)
                tmp_path = tmp.name
            try:
                subprocess.run([*shlex.split(editor), tmp_path], check=True)
                with open(tmp_path) as f:
                    commit_msg = f.read().strip()
            finally:
                os.unlink(tmp_path)

        run(["git", "commit", "-m", commit_msg])
        success("Committed.")

        if confirm(f"Push to origin/{self._branch}?"):
            result = subprocess.run(["git", "push", "origin", "HEAD"])
            if result.returncode != 0:
                warn("Push failed. If the remote is ahead, run: git pull --rebase")
                return
            success("Pushed.")
            if confirm("Open a PR/MR?", default_yes=False):
                self._cmd_create()

    def _cmd_create(self) -> None:
        branch = self._branch
        base = default_base_branch()
        if branch == base:
            warn(f"Current branch is the base branch ({base}). Create a feature branch first.")
            return

        unpushed = capture(["git", "log", f"origin/{branch}..HEAD", "--oneline"])
        if unpushed:
            info(f"Unpushed commits:\n{unpushed}")
            if confirm("Push first?"):
                result = subprocess.run(["git", "push", "origin", "HEAD"])
                if result.returncode != 0:
                    warn("Push failed.")
                    return
                success("Pushed.")

        pr_title = _pr_title_from_log()
        commit_msg = capture(["git", "log", "-1", "--format=%B"])
        ticket_id = extract_ticket_id(branch)
        url_template = resolve_ticket_url_template()
        t_link = ticket_url(ticket_id, url_template)
        stat = diff_stat()

        header("Generating PR body...")
        pr_body = generate_pr_body(ticket_id, t_link, commit_msg, stat, "")

        header("Proposed PR:")
        print(f"  Title : {pr_title}")
        print(f"  Base  : {base} ← {branch}")
        print()
        print(pr_body)
        print()

        ans = input("[Y/n/edit title/edit body] ").strip().lower()
        if ans in ("n", "no"):
            info("Aborted.")
            return
        if ans in ("edit title", "e title"):
            pr_title = input("New title: ").strip() or pr_title
        elif ans in ("edit body", "e body"):
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="git-agent-pr-", delete=False
            ) as tmp:
                tmp.write(pr_body)
                tmp_path = tmp.name
            try:
                subprocess.run([*shlex.split(editor), tmp_path], check=True)
                with open(tmp_path) as f:
                    pr_body = f.read().strip()
            finally:
                os.unlink(tmp_path)

        rem_url = remote_url()
        platform = detect_platform(rem_url)
        if platform == "github":
            pr_url = create_github_pr(pr_title, pr_body, branch, base, False)
        elif platform == "gitlab":
            pr_url = create_gitlab_mr(pr_title, pr_body, branch, base, False)
        elif platform == "bitbucket":
            pr_url = create_bitbucket_pr(pr_title, pr_body, branch, base, False)
        else:
            warn(f"Unrecognised platform. Remote: {rem_url}")
            return
        success(f"PR created: {pr_url}")

    def _cmd_update(self) -> None:
        branch = self._branch

        unpushed = capture(["git", "log", f"origin/{branch}..HEAD", "--oneline"])
        if unpushed:
            info(f"Unpushed commits:\n{unpushed}")
            if confirm("Push first?"):
                result = subprocess.run(["git", "push", "origin", "HEAD"])
                if result.returncode != 0:
                    warn("Push failed.")
                    return
                success("Pushed.")

        pr_title = _pr_title_from_log()
        commit_msg = capture(["git", "log", "-1", "--format=%B"])
        ticket_id = extract_ticket_id(branch)
        url_template = resolve_ticket_url_template()
        t_link = ticket_url(ticket_id, url_template)
        stat = diff_stat()

        header("Generating PR body...")
        pr_body = generate_pr_body(ticket_id, t_link, commit_msg, stat, "")

        header("Proposed PR update:")
        print(f"  Title : {pr_title}")
        print()
        print(pr_body)
        print()

        if not confirm("Update PR with this content?"):
            info("Aborted.")
            return

        result = update_pr(title=pr_title, body=pr_body)
        success(f"PR updated: {result}")

    def _passthrough(self, rest: str) -> None:
        if not rest:
            warn("Usage: git <subcommand>")
            return
        parts = shlex.split(rest)
        subprocess.run(["git"] + parts)
        if parts[0] in {"checkout", "switch", "rebase", "merge", "pull", "reset"}:
            self._branch = current_branch()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if not sys.argv[1:] and sys.stdin.isatty():
        ensure_git_repo()
        GitConsole().run()
        return

    parser = build_parser()
    args = parser.parse_args()

    if args.draft:
        args.pr = True

    # ── sanity checks ──────────────────────────────────────────────────────────
    ensure_git_repo()

    # ── staging check ──────────────────────────────────────────────────────────
    if not staged_files():
        unstaged = unstaged_files()
        if not unstaged:
            die("Nothing to commit — working tree is clean.")
        warn("No staged changes. Unstaged files detected.")
        run(["git", "diff", "--stat"], capture=False)
        print()
        if args.yes:
            die("Use 'git add' to stage changes before running git-agent.")
        if not confirm("Stage all changes?", default_yes=False):
            die("Aborted. Stage changes with 'git add' first.")
        run(["git", "add", "-A"])

    # ── gather git context ─────────────────────────────────────────────────────
    branch = current_branch()
    if not branch:
        die("Detached HEAD detected. Check out a branch before running git-agent.")
    stat = diff_stat()
    diff = full_diff()
    log = recent_log()
    context = " ".join(args.context)

    # ── extract ticket ID ──────────────────────────────────────────────────────
    url_template = resolve_ticket_url_template()
    ticket_id = extract_ticket_id(branch)
    if ticket_id:
        info(f"Ticket ID: {ticket_id}")

    # ── build commit message ───────────────────────────────────────────────────
    if args.message:
        commit_msg = args.message
    else:
        header("Generating commit message...")
        if os.environ.get("ANTHROPIC_API_KEY"):
            info("Using Claude (Haiku)")
        elif os.environ.get("OPENAI_API_KEY"):
            info("Using GPT-4o")
        commit_msg = generate_commit_msg(ticket_id, branch, log, stat, diff, context)

    # ── confirm commit message ─────────────────────────────────────────────────
    header("Proposed commit message:")
    print()
    print(commit_msg)
    print()

    if not args.yes:
        ans = input("Commit with this message? [Y/n/edit] ").strip().lower()
        if ans in ("n", "no"):
            print("Aborted.")
            sys.exit(0)
        if ans in ("e", "edit"):
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", prefix="git-agent-msg-", delete=False
            ) as tmp:
                tmp.write(commit_msg)
                tmp_path = tmp.name
            try:
                subprocess.run([*shlex.split(editor), tmp_path], check=True)
                with open(tmp_path) as f:
                    commit_msg = f.read().strip()
            finally:
                os.unlink(tmp_path)

    # ── commit ─────────────────────────────────────────────────────────────────
    run(["git", "commit", "-m", commit_msg])
    success("Committed.")

    if args.no_push:
        sys.exit(0)

    # ── push ───────────────────────────────────────────────────────────────────
    if not args.yes:
        if not confirm(f"Push to origin/{branch}?"):
            print("Aborted.")
            sys.exit(0)

    result = subprocess.run(["git", "push", "origin", "HEAD"], capture_output=False)
    if result.returncode != 0:
        warn("Push failed. If the remote is ahead, run: git pull --rebase")
        sys.exit(1)
    success("Pushed.")

    # ── prompt for PR if not requested ────────────────────────────────────────
    if not args.pr and not args.yes:
        if confirm("Open a PR/MR?", default_yes=False):
            args.pr = True

    if not args.pr:
        sys.exit(0)

    # ── detect platform ────────────────────────────────────────────────────────
    rem_url = remote_url()
    platform = detect_platform(rem_url)
    owner_repo = parse_remote_path(rem_url)
    info(f"Platform: {platform} ({owner_repo})")

    # ── detect base branch ─────────────────────────────────────────────────────
    base = args.base or default_base_branch()
    if branch == base:
        die(f"Current branch is the base branch ({base}). Create a feature branch first.")

    # ── PR title & body ────────────────────────────────────────────────────────
    pr_title = args.title or commit_msg.splitlines()[0]
    t_link = ticket_url(ticket_id, url_template)
    pr_body = generate_pr_body(ticket_id, t_link, commit_msg, stat, context)

    # ── confirm PR ─────────────────────────────────────────────────────────────
    header("Proposed PR:")
    print(f"  Title  : {pr_title}")
    print(f"  Base   : {base} ← {branch}")
    print(f"  Draft  : {args.draft}")
    print()
    print(pr_body)
    print()

    if not args.yes:
        if not confirm("Create PR?"):
            print("Aborted.")
            sys.exit(0)

    # ── create PR ──────────────────────────────────────────────────────────────
    if platform == "github":
        pr_url = create_github_pr(pr_title, pr_body, branch, base, args.draft)
    elif platform == "gitlab":
        pr_url = create_gitlab_mr(pr_title, pr_body, branch, base, args.draft)
    elif platform == "bitbucket":
        pr_url = create_bitbucket_pr(pr_title, pr_body, branch, base, args.draft)
    else:
        warn(f"Unrecognised platform. Remote: {rem_url}")
        warn("Supported: github.com, gitlab.com, bitbucket.org")
        sys.exit(1)

    success(f"PR created: {pr_url}")


if __name__ == "__main__":
    main()
