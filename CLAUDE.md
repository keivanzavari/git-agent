# git-agent

This project is the home for the `/git-agent` Claude Code skill.

## What it does

`/git-agent` is a Claude Code skill that automates git commits and GitHub PRs.
It is designed to be invoked by an agent (or directly) after code review.

Skill file: `~/.claude/skills/git-agent/SKILL.md`

## Usage

### Direct invocation (you calling it)
```
/git-agent
```

### With Jira context (parent agent calling it)
```
/git-agent PROJECT-1234 is about adding OAuth2 login via Google. Key decision: we use
short-lived JWTs stored in httpOnly cookies rather than localStorage.
```

### With PR creation
```
/git-agent open a PR. Jira ticket: "As a user I want SSO login". Reviewers: @alice @bob.
```

## Branch naming convention

The skill expects branch names in one of these formats:
- `feature/PROJECT-1234/short-description`
- `PROJECT-1234-short-description`
- `fix/PROJECT-1234/what-was-fixed`

The Jira ID is extracted with the regex `[A-Z]+-[0-9]+`.

## Workflow

1. Parent agent (or you) retrieves Jira ticket context (via Atlassian MCP or manually)
2. Code is written, reviewed
3. Changes are staged (`git add`)
4. Call `/git-agent [jira context] [extra notes] [open a PR?]`
5. Skill generates commit message, asks for confirmation, commits, pushes, optionally creates PR

## GitHub PR creation

- If `gh` CLI is installed: uses `gh pr create`
- Otherwise: uses GitHub REST API via `curl` with `$GITHUB_TOKEN`

Set `GITHUB_TOKEN` in your shell environment for API-based PR creation:
```bash
export GITHUB_TOKEN=ghp_...
```

## Extending this skill

To update the skill behavior, edit `~/.claude/skills/git-agent/SKILL.md`.

Ideas for future enhancements:
- Auto-fetch Jira context if `JIRA_TOKEN` and `JIRA_BASE_URL` are set
- Add `--draft` flag for draft PRs
- Auto-assign reviewers from CODEOWNERS
- Auto-label PRs based on changed file paths
