# git-agent

A smart git commit, push, and PR tool. Works as a standalone CLI, with any AI agent, or as a Claude Code skill.

## What it does

- Reads your staged diff and branch name
- Generates a structured commit message (via Claude, GPT-4o, or your `$EDITOR`)
- Extracts a ticket/issue ID from the branch name and prefixes the commit
- Works with any ticketing system — Jira, Linear, GitHub Issues, YouTrack, and more
- Commits, pushes, and optionally opens a PR/MR
- Supports **GitHub**, **GitLab**, and **Bitbucket** (auto-detected from remote URL)

## Install

Requires Python 3.9+ and `git`. No extra packages — stdlib only.

```bash
git clone https://github.com/keivanzavari/git-agent
# Option A: symlink globally
ln -sf "$PWD/git-agent/git-agent" /usr/local/bin/git-agent

# Option B: add to PATH in your shell profile
export PATH="$PWD/git-agent:$PATH"
```

### Claude Code skill (optional)

```bash
mkdir -p ~/.claude/skills/git-agent
cp git-agent/SKILL.md ~/.claude/skills/git-agent/SKILL.md
```

Then invoke with `/git-agent` inside Claude Code.

## Usage

```bash
# Stage your changes first
git add -p

# Standalone — LLM generates the commit message
git-agent

# With ticket context (Jira, Linear, GitHub Issues, etc.)
git-agent "AUTH-42: adds Google SSO via short-lived JWTs in httpOnly cookies"

# Commit + push + open PR
git-agent --pr

# Draft PR on a specific base branch
git-agent --pr --draft --base develop

# Agent-driven: message already written, skip all prompts
git-agent --message "Add OAuth2 login via Google" --pr --yes

# Commit only, no push
git-agent --no-push
```

## Options

| Flag | Description |
|---|---|
| `-m, --message MSG` | Use this commit message (skip LLM generation) |
| `--title TITLE` | Override PR/MR title |
| `--pr` | Open a PR/MR after pushing |
| `--draft` | Open a draft PR/MR (implies `--pr`) |
| `--base BRANCH` | Base branch for PR/MR (default: auto-detected) |
| `--no-push` | Commit only |
| `-y, --yes` | Skip all confirmation prompts |

## Configuration

Set environment variables in your shell profile (`~/.zshrc`, `~/.bashrc`):

```bash
# LLM for commit message generation (pick one)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# GitHub (only needed if gh CLI is not installed)
export GITHUB_TOKEN=ghp_...

# GitLab (only needed if glab CLI is not installed)
export GITLAB_TOKEN=glpat-...

# Bitbucket (no official CLI — always required)
export BITBUCKET_USER=your-username
export BITBUCKET_TOKEN=your-app-password

# Optional: clickable ticket links in PR bodies — use {id} as the placeholder
export TICKET_URL_TEMPLATE=https://yourorg.atlassian.net/browse/{id}  # Jira
# export TICKET_URL_TEMPLATE=https://linear.app/myteam/issue/{id}     # Linear
# export TICKET_URL_TEMPLATE=https://github.com/org/repo/issues/{id}  # GitHub Issues

# Optional: override the default ticket ID regex ([A-Z]+-[0-9]+)
# export TICKET_PATTERN='[0-9]+'       # plain issue numbers (GitHub / GitLab)
# export TICKET_PATTERN='sc-[0-9]+'    # Shortcut
# export TICKET_PATTERN='AB#[0-9]+'    # Azure DevOps
```

If no API key is set, the script falls back to your `$EDITOR` for message input.

## Platform support

| Platform | CLI (preferred) | API fallback |
|---|---|---|
| GitHub | `gh` | `GITHUB_TOKEN` + curl |
| GitLab | `glab` | `GITLAB_TOKEN` + curl |
| Bitbucket | — | `BITBUCKET_USER` + `BITBUCKET_TOKEN` + curl |

The platform is auto-detected from `git remote get-url origin`.

## Ticket ID extraction

A ticket/issue ID is extracted from the branch name via the `TICKET_PATTERN` regex
(default: `[A-Z]+-[0-9]+`, which covers Jira, Linear, YouTrack, and similar systems):

```text
feature/AUTH-42/google-sso   →   [AUTH-42] ...
fix/PLAT-7-null-pointer      →   [PLAT-7] ...
main                         →   (no prefix)
```

For other systems, override `TICKET_PATTERN`:

```text
TICKET_PATTERN='[0-9]+'      issue/123-fix-login   →   [123] ...   (GitHub/GitLab)
TICKET_PATTERN='sc-[0-9]+'   sc-456/dark-mode      →   [sc-456] ...  (Shortcut)
```

## Using with AI agents

Any agent with `Bash` tool access can call `git-agent` directly:

```bash
# The agent generates the message, the script handles the mechanics
git-agent --message "<agent-generated message>" --pr --yes "<extra context>"
```

For Claude Code, use the `/git-agent` skill which handles message generation
and delegates execution to this script automatically.

## Running tests

```bash
python3 -m pytest tests/ -v
```

No test dependencies beyond `pytest` (install with `pip install pytest`).

## Requirements

- Python 3.9+ (stdlib only — no pip dependencies)
- `git`
- `gh` CLI (optional, preferred for GitHub PRs)
- `glab` CLI (optional, preferred for GitLab MRs)
