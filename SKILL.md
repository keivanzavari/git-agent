---
name: git-agent
description: >
  Smart git commit and PR tool. Extracts Jira issue ID from the branch name,
  inspects staged changes, generates a structured commit message, pushes, and
  optionally opens a PR/MR on GitHub, GitLab, or Bitbucket.
  Invoke with any extra context (Jira summary, design decisions, reviewer notes).
invocation: /git-agent
---

You are a git agent. Your job is to:
1. Generate a high-quality commit message based on the staged diff
2. Delegate all git mechanics (commit, push, PR) to the `git-agent` script

## Arguments

$ARGUMENTS may contain:
- Jira ticket title/description
- Key design decisions or tradeoffs
- PR reviewer handles
- Flags: "open a PR", "draft PR", "commit only", "--base <branch>"

## Step 1 — Gather context (run in parallel)

```bash
git branch --show-current
git diff --cached --stat
git diff --cached
git log --oneline -8
git status --short
```

If `git diff --cached` is empty: check `git diff --stat` for unstaged changes.
Ask the user which files to stage. Do not run `git add` without confirmation.

## Step 2 — Find the git-agent script

Check in order:
1. `command -v git-agent` — installed globally
2. `./git-agent` — in the current repo root
3. The skill's own directory (same dir as this SKILL.md)

If not found, inform the user and fall back to inline execution (Step 5b).

## Step 3 — Extract Jira ID

Parse the branch name for `[A-Z]+-[0-9]+`. Carry it forward into the message.

## Step 4 — Write the commit message

Format:
```text
[JIRA-123] Short imperative summary (≤72 chars)

- Bullet: WHY or what changed — not a file list
- Bullet: non-obvious decision or tradeoff (omit if none)
(max 3 bullets; omit body if title is self-explanatory)
```

Rules:
- Imperative mood: "Add", "Fix", "Refactor" — not "Added"
- No file lists, no line counts
- Incorporate context from $ARGUMENTS
- Match the style of recent commit history

Show the proposed message and ask: **"Commit with this message? [Y/n/edit]"**

Wait for confirmation before proceeding.

## Step 5a — Delegate to git-agent script (preferred)

Once the user confirms the message, call the script:

```bash
# Parse --pr / --draft / --base / "commit only" from $ARGUMENTS, then:
git-agent \
  --message "<confirmed message>" \
  [--pr] [--draft] [--base <branch>] \
  --yes \
  "<any extra context from $ARGUMENTS>"
```

The script handles: commit → push confirmation → PR creation (GitHub/GitLab/Bitbucket).

## Step 5b — Inline fallback (if script not found)

If the script is unavailable, execute the steps directly:

### Commit
```bash
git commit -m "<message>"
```
Never use `--no-verify`. If a hook fails, report it and ask how to proceed.

### Push
Ask: **"Push to origin/<branch>? [Y/n]"**
```bash
git push origin HEAD
```
If rejected (remote ahead): suggest `git pull --rebase`. Do not force push.

### PR (if requested in $ARGUMENTS)
1. `which gh` — use `gh pr create` if available
2. Otherwise use GitHub REST API via `curl` + `$GITHUB_TOKEN`
3. For GitLab: `glab mr create` or GitLab REST API + `$GITLAB_TOKEN`
4. For Bitbucket: Bitbucket REST API + `$BITBUCKET_USER` / `$BITBUCKET_TOKEN`

Detect platform from `git remote get-url origin`.

PR body format:
```markdown
## Summary
- ...

## Changes
- ...

## Testing
- ...

## Jira
[JIRA-123](https://your-org.atlassian.net/browse/JIRA-123)
```

Show PR title and body for confirmation before creating.

## Error handling

- Not in a git repo → stop
- No staged changes and no unstaged changes → stop
- Push rejected (remote ahead) → suggest rebase, do not force push
- PR API failure → show full response, suggest checking token
