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
    """
    Create a pull request or merge request on the repository's detected remote platform.
    
    Determines the platform from the repository 'origin' remote URL and creates a PR (GitHub/Bitbucket) or MR (GitLab) from the current branch into `base` (or the repository default). Supports opening the request as a draft when `draft` is True.
    
    Parameters:
        title (str): PR/MR title.
        body (str): PR/MR description (Markdown supported).
        draft (bool): If True, create the request as a draft. Default False.
        base (str): Base branch to merge into; if empty, the repository default branch is used.
    
    Returns:
        str: The PR/MR identifier or URL as returned by the platform client.
    
    Raises:
        ValueError: If the remote URL's platform is unsupported (not GitHub, GitLab, or Bitbucket).
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
    """
    Fetch comments and reviews for the open pull/merge request of the current branch.
    
    Determines the repository platform from the remote URL and returns the PR/MR number along with a list of comment objects. For some platforms (e.g., Bitbucket in this implementation) the comments list may be empty.
    
    Returns:
        dict: {
            'pr_number': int or None,
            'comments': list of dicts with keys 'author', 'body', 'created_at', 'state'
        }
    
    Raises:
        ValueError: If the remote URL's platform is unsupported (supported: github.com, gitlab.com, bitbucket.org).
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


if __name__ == "__main__":
    mcp_server.run()
