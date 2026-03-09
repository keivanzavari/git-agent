#!/usr/bin/env python3
"""git-agent MCP server — exposes git-agent tools over the MCP stdio protocol.

Allows MCP-compatible clients (VS Code Copilot agent mode, Claude Desktop, etc.)
to call git-agent operations directly from their agent loops.

Usage:
    python3 git_agent_mcp.py          # run as stdio MCP server
    pip install mcp                   # required dependency
"""

import sys
from pathlib import Path
from typing import Optional

# Ensure git_agent.py is importable regardless of cwd
sys.path.insert(0, str(Path(__file__).parent))
import git_agent as ga

from mcp.server.fastmcp import FastMCP

mcp_server = FastMCP("git-agent")


@mcp_server.tool()
def get_git_status() -> dict:
    """Return current repo state: branch, staged files, unstaged files, and recent log.

    Useful for an agent to understand the repository state before deciding what
    to do next (commit, push, open PR, etc.).
    """
    return {
        "branch": ga.current_branch(),
        "staged_files": ga.staged_files(),
        "unstaged_files": ga.unstaged_files(),
        "recent_log": ga.recent_log(),
    }


@mcp_server.tool()
def get_staged_diff() -> dict:
    """Return the staged diff, stat, branch name, and extracted ticket ID.

    Useful for agents that want to inspect what is staged before generating a
    commit message or deciding whether to commit.
    """
    branch = ga.current_branch()
    ticket_id = ga.extract_ticket_id(branch)
    stat = ga.diff_stat()
    diff = ga.full_diff()
    return {
        "branch": branch,
        "ticket_id": ticket_id,
        "stat": stat,
        "diff": diff,
    }


@mcp_server.tool()
def generate_commit_message(context: str = "") -> str:
    """Generate a structured commit message for the currently staged changes.

    Uses an LLM (Claude or GPT-4o) when an API key is configured; otherwise
    falls back to $EDITOR or stdin. Pass optional context (Jira ticket summary,
    design decisions, etc.) to improve the generated message.

    Args:
        context: Optional free-text context to pass to the LLM (e.g. Jira ticket
                 summary, key design decisions, reviewer notes).
    """
    branch = ga.current_branch()
    ticket_id = ga.extract_ticket_id(branch)
    log = ga.recent_log()
    stat = ga.diff_stat()
    diff = ga.full_diff()
    return ga.generate_commit_msg(ticket_id, branch, log, stat, diff, context)


@mcp_server.tool()
def commit(message: str, no_push: bool = False) -> str:
    """Commit staged changes with the given message, then push unless no_push is True.

    Raises RuntimeError if there are no staged files.

    Args:
        message:  The commit message to use.
        no_push:  If True, commit only — skip the push to origin. Default False.
    """
    if not ga.staged_files():
        raise RuntimeError(
            "No staged files. Use 'git add' to stage changes before committing."
        )
    ga.run(["git", "commit", "-m", message])
    if not no_push:
        ga.run(["git", "push", "origin", "HEAD"])
        return f"Committed and pushed: {message!r}"
    return f"Committed (no push): {message!r}"


@mcp_server.tool()
def create_pr(
    title: str,
    body: str,
    draft: bool = False,
    base: str = "",
) -> str:
    """Create a pull request / merge request on the auto-detected platform.

    Supports GitHub, GitLab, and Bitbucket. The platform and repository are
    determined from the 'origin' remote URL automatically.

    Args:
        title:  PR/MR title.
        body:   PR/MR description body (Markdown supported).
        draft:  If True, open as a draft PR/MR. Default False.
        base:   Base branch to merge into. Defaults to the repository default branch.
    """
    rem_url = ga.remote_url()
    platform = ga.detect_platform(rem_url)
    branch = ga.current_branch()
    resolved_base = base or ga.default_base_branch()

    if platform == "github":
        return ga.create_github_pr(title, body, branch, resolved_base, draft)
    elif platform == "gitlab":
        return ga.create_gitlab_mr(title, body, branch, resolved_base, draft)
    elif platform == "bitbucket":
        return ga.create_bitbucket_pr(title, body, branch, resolved_base, draft)
    else:
        raise ValueError(
            f"Unsupported platform for remote URL: {rem_url!r}. "
            "Supported: github.com, gitlab.com, bitbucket.org"
        )


@mcp_server.tool()
def get_pr_comments() -> dict:
    """Fetch comments and reviews on the open PR/MR for the current branch.

    Supports GitHub (gh CLI) and GitLab (glab CLI). Returns a dict with
    'pr_number' (int or None) and 'comments' (list of dicts, each with keys
    'author', 'body', 'created_at', 'state').

    Useful for agents that need to read reviewer feedback and address it.
    Bitbucket is fully supported via bb-cli (``bb pr comments``).
    """
    rem_url = ga.remote_url()
    platform = ga.detect_platform(rem_url)

    if platform == "github":
        return ga.get_github_pr_comments()
    elif platform == "gitlab":
        return ga.get_gitlab_mr_comments()
    elif platform == "bitbucket":
        return ga.get_bitbucket_pr_comments()
    else:
        raise ValueError(
            f"Unsupported platform for remote URL: {rem_url!r}. "
            "Supported: github.com, gitlab.com, bitbucket.org"
        )


@mcp_server.tool()
def update_pr(
    title: str = "",
    body: str = "",
    base: str = "",
    draft: Optional[bool] = None,
) -> str:
    """Update an existing PR/MR on the current branch.

    Supports GitHub (gh pr edit / gh pr ready), GitLab (glab mr update),
    and Bitbucket Server (bb pr update). Only pass the fields you want to
    change — omitted or empty fields are left untouched.

    Args:
        title: New PR/MR title. Pass empty string to leave unchanged.
        body:  New PR/MR description/body. Pass empty string to leave unchanged.
        base:  New base/target branch. Pass empty string to leave unchanged.
        draft: True → convert to draft. False → mark ready. None → leave unchanged.
    """
    return ga.update_pr(title, body, base, draft)


if __name__ == "__main__":
    mcp_server.run()
