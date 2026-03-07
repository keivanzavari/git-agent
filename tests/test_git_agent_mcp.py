"""Tests for git_agent_mcp.py tool handlers.

Import the tool functions directly (not via MCP protocol) and mock out
git_agent (ga) so no real git commands or network calls are made.

Run with:  python3 -m pytest tests/ -v
"""

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import git_agent_mcp from the repo root (no install needed).
# We need to ensure git_agent is importable too; the mcp library must be
# installed (pip install mcp).
# ---------------------------------------------------------------------------
_REPO = Path(__file__).parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Import the module under test.  If `mcp` is not installed the import will
# fail with an informative error.
import git_agent_mcp  # noqa: E402  (after sys.path tweak)
import git_agent as ga  # noqa: E402


# ===========================================================================
# get_git_status
# ===========================================================================
class TestGetGitStatus:
    def test_returns_expected_keys(self):
        with patch.object(ga, "current_branch", return_value="feature/PROJ-1/my-feature"), \
             patch.object(ga, "staged_files", return_value=["src/auth.py"]), \
             patch.object(ga, "unstaged_files", return_value=["README.md"]), \
             patch.object(ga, "recent_log", return_value="abc1234 Add login\ndef5678 Fix bug"):
            result = git_agent_mcp.get_git_status()

        assert result["branch"] == "feature/PROJ-1/my-feature"
        assert result["staged_files"] == ["src/auth.py"]
        assert result["unstaged_files"] == ["README.md"]
        assert "abc1234" in result["recent_log"]

    def test_returns_dict_with_all_four_keys(self):
        with patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "staged_files", return_value=[]), \
             patch.object(ga, "unstaged_files", return_value=[]), \
             patch.object(ga, "recent_log", return_value=""):
            result = git_agent_mcp.get_git_status()

        assert set(result.keys()) == {"branch", "staged_files", "unstaged_files", "recent_log"}

    def test_empty_staged_and_unstaged_files(self):
        with patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "staged_files", return_value=[]), \
             patch.object(ga, "unstaged_files", return_value=[]), \
             patch.object(ga, "recent_log", return_value=""):
            result = git_agent_mcp.get_git_status()

        assert result["staged_files"] == []
        assert result["unstaged_files"] == []


# ===========================================================================
# get_staged_diff
# ===========================================================================
class TestGetStagedDiff:
    def test_returns_expected_keys(self):
        with patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "extract_ticket_id", return_value="AUTH-42"), \
             patch.object(ga, "diff_stat", return_value="auth.py | 20 +++"), \
             patch.object(ga, "full_diff", return_value="diff --git a/auth.py ..."):
            result = git_agent_mcp.get_staged_diff()

        assert set(result.keys()) == {"branch", "ticket_id", "stat", "diff"}

    def test_ticket_id_extracted_from_branch(self):
        with patch.object(ga, "current_branch", return_value="feature/PROJ-99/new-thing"), \
             patch.object(ga, "extract_ticket_id", return_value="PROJ-99") as mock_extract, \
             patch.object(ga, "diff_stat", return_value=""), \
             patch.object(ga, "full_diff", return_value=""):
            result = git_agent_mcp.get_staged_diff()

        mock_extract.assert_called_once_with("feature/PROJ-99/new-thing")
        assert result["ticket_id"] == "PROJ-99"

    def test_empty_ticket_id_when_no_pattern_match(self):
        with patch.object(ga, "current_branch", return_value="hotfix/fix-crash"), \
             patch.object(ga, "extract_ticket_id", return_value=""), \
             patch.object(ga, "diff_stat", return_value="file.py | 5 ++"), \
             patch.object(ga, "full_diff", return_value="diff content"):
            result = git_agent_mcp.get_staged_diff()

        assert result["ticket_id"] == ""
        assert result["branch"] == "hotfix/fix-crash"

    def test_diff_and_stat_returned(self):
        expected_stat = "models.py | 15 +++\nviews.py  |  3 +"
        expected_diff = "diff --git a/models.py b/models.py\n+++ added line"
        with patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "extract_ticket_id", return_value=""), \
             patch.object(ga, "diff_stat", return_value=expected_stat), \
             patch.object(ga, "full_diff", return_value=expected_diff):
            result = git_agent_mcp.get_staged_diff()

        assert result["stat"] == expected_stat
        assert result["diff"] == expected_diff


# ===========================================================================
# generate_commit_message
# ===========================================================================
class TestGenerateCommitMessage:
    def test_calls_generate_commit_msg_with_correct_args(self):
        with patch.object(ga, "current_branch", return_value="feature/ENG-7/auth"), \
             patch.object(ga, "extract_ticket_id", return_value="ENG-7"), \
             patch.object(ga, "recent_log", return_value="abc1234 Prior commit"), \
             patch.object(ga, "diff_stat", return_value="auth.py | 10 ++"), \
             patch.object(ga, "full_diff", return_value="diff content"), \
             patch.object(ga, "generate_commit_msg", return_value="[ENG-7] Add auth") as mock_gen:
            result = git_agent_mcp.generate_commit_message(context="OAuth via Google")

        mock_gen.assert_called_once_with(
            "ENG-7",
            "feature/ENG-7/auth",
            "abc1234 Prior commit",
            "auth.py | 10 ++",
            "diff content",
            "OAuth via Google",
        )
        assert result == "[ENG-7] Add auth"

    def test_default_context_is_empty_string(self):
        with patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "extract_ticket_id", return_value=""), \
             patch.object(ga, "recent_log", return_value=""), \
             patch.object(ga, "diff_stat", return_value=""), \
             patch.object(ga, "full_diff", return_value=""), \
             patch.object(ga, "generate_commit_msg", return_value="Fix typo") as mock_gen:
            result = git_agent_mcp.generate_commit_message()

        _, _, _, _, _, context_arg = mock_gen.call_args[0]
        assert context_arg == ""
        assert result == "Fix typo"

    def test_returns_string_from_generate_commit_msg(self):
        expected = "[AUTH-1] Implement SSO\n\n- Use httpOnly cookies\n- Short-lived JWTs"
        with patch.object(ga, "current_branch", return_value="feature/AUTH-1/sso"), \
             patch.object(ga, "extract_ticket_id", return_value="AUTH-1"), \
             patch.object(ga, "recent_log", return_value=""), \
             patch.object(ga, "diff_stat", return_value=""), \
             patch.object(ga, "full_diff", return_value=""), \
             patch.object(ga, "generate_commit_msg", return_value=expected):
            result = git_agent_mcp.generate_commit_message(context="Use JWTs")

        assert result == expected


# ===========================================================================
# commit
# ===========================================================================
class TestCommit:
    def test_commits_and_pushes_by_default(self):
        with patch.object(ga, "staged_files", return_value=["src/app.py"]), \
             patch.object(ga, "run") as mock_run:
            result = git_agent_mcp.commit(message="Add feature")

        assert mock_run.call_count == 2
        commit_call, push_call = mock_run.call_args_list
        assert commit_call == call(["git", "commit", "-m", "Add feature"])
        assert push_call == call(["git", "push", "origin", "HEAD"])
        assert "Committed and pushed" in result

    def test_no_push_skips_push(self):
        with patch.object(ga, "staged_files", return_value=["src/app.py"]), \
             patch.object(ga, "run") as mock_run:
            result = git_agent_mcp.commit(message="Fix bug", no_push=True)

        assert mock_run.call_count == 1
        commit_call = mock_run.call_args_list[0]
        assert commit_call == call(["git", "commit", "-m", "Fix bug"])
        assert "no push" in result

    def test_empty_staged_files_raises_error(self):
        with patch.object(ga, "staged_files", return_value=[]), \
             patch.object(ga, "run") as mock_run:
            with pytest.raises(RuntimeError, match="No staged files"):
                git_agent_mcp.commit(message="Some message")

        mock_run.assert_not_called()

    def test_message_included_in_return_string(self):
        with patch.object(ga, "staged_files", return_value=["file.py"]), \
             patch.object(ga, "run"):
            result = git_agent_mcp.commit(message="My commit message")

        assert "My commit message" in result

    def test_no_push_true_does_not_call_push(self):
        with patch.object(ga, "staged_files", return_value=["file.py"]), \
             patch.object(ga, "run") as mock_run:
            git_agent_mcp.commit(message="Commit only", no_push=True)

        called_commands = [c[0][0] for c in mock_run.call_args_list]
        assert ["git", "push", "origin", "HEAD"] not in called_commands


# ===========================================================================
# create_pr
# ===========================================================================
class TestCreatePr:
    def test_github_pr_created_and_url_returned(self):
        expected_url = "https://github.com/myorg/myrepo/pull/42"
        with patch.object(ga, "remote_url", return_value="git@github.com:myorg/myrepo.git"), \
             patch.object(ga, "detect_platform", return_value="github"), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-1/sso"), \
             patch.object(ga, "default_base_branch", return_value="main"), \
             patch.object(ga, "create_github_pr", return_value=expected_url) as mock_create:
            result = git_agent_mcp.create_pr(title="Add SSO", body="PR body")

        mock_create.assert_called_once_with(
            "Add SSO", "PR body", "feature/AUTH-1/sso", "main", False,
        )
        assert result == expected_url

    def test_gitlab_mr_created(self):
        expected_url = "https://gitlab.com/myorg/myrepo/-/merge_requests/7"
        with patch.object(ga, "remote_url", return_value="git@gitlab.com:myorg/myrepo.git"), \
             patch.object(ga, "detect_platform", return_value="gitlab"), \
             patch.object(ga, "current_branch", return_value="feature/T-5/thing"), \
             patch.object(ga, "default_base_branch", return_value="main"), \
             patch.object(ga, "create_gitlab_mr", return_value=expected_url) as mock_create:
            result = git_agent_mcp.create_pr(title="Add thing", body="Body", draft=True)

        mock_create.assert_called_once_with(
            "Add thing", "Body", "feature/T-5/thing", "main", True,
        )
        assert result == expected_url

    def test_bitbucket_pr_created(self):
        expected_url = "https://bitbucket.org/myorg/myrepo/pull-requests/3"
        with patch.object(ga, "remote_url", return_value="https://bitbucket.org/myorg/myrepo.git"), \
             patch.object(ga, "detect_platform", return_value="bitbucket"), \
             patch.object(ga, "current_branch", return_value="feature/my-feat"), \
             patch.object(ga, "default_base_branch", return_value="main"), \
             patch.object(ga, "create_bitbucket_pr", return_value=expected_url) as mock_create:
            result = git_agent_mcp.create_pr(title="My feat", body="Body")

        mock_create.assert_called_once_with(
            "My feat", "Body", "feature/my-feat", "main", False,
        )
        assert result == expected_url

    def test_custom_base_branch_used_when_provided(self):
        with patch.object(ga, "remote_url", return_value="git@github.com:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="github"), \
             patch.object(ga, "current_branch", return_value="feature/x"), \
             patch.object(ga, "default_base_branch", return_value="main") as mock_default, \
             patch.object(ga, "create_github_pr", return_value="https://github.com/org/repo/pull/1") as mock_create:
            git_agent_mcp.create_pr(title="T", body="B", base="develop")

        mock_default.assert_not_called()
        _, _, _, base_arg, *_ = mock_create.call_args[0]
        assert base_arg == "develop"

    def test_unsupported_platform_raises_value_error(self):
        with patch.object(ga, "remote_url", return_value="https://codeberg.org/org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="unknown"), \
             patch.object(ga, "current_branch", return_value="feature/x"), \
             patch.object(ga, "default_base_branch", return_value="main"):
            with pytest.raises(ValueError, match="Unsupported platform"):
                git_agent_mcp.create_pr(title="T", body="B")


# ===========================================================================
# get_pr_comments
# ===========================================================================
class TestGetPrComments:
    _expected = {"pr_number": 42, "comments": [{"author": "alice", "body": "LGTM",
                                                 "created_at": "2026-01-01T00:00:00Z",
                                                 "state": ""}]}

    def test_github_dispatches_to_github_fn(self):
        with patch.object(ga, "remote_url", return_value="git@github.com:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="github"), \
             patch.object(ga, "get_github_pr_comments", return_value=self._expected) as mock_fn:
            result = git_agent_mcp.get_pr_comments()

        mock_fn.assert_called_once()
        assert result == self._expected

    def test_gitlab_dispatches_to_gitlab_fn(self):
        with patch.object(ga, "remote_url", return_value="git@gitlab.com:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="gitlab"), \
             patch.object(ga, "get_gitlab_mr_comments", return_value=self._expected) as mock_fn:
            result = git_agent_mcp.get_pr_comments()

        mock_fn.assert_called_once()
        assert result == self._expected

    def test_bitbucket_dispatches_to_bitbucket_fn(self):
        empty = {"pr_number": None, "comments": []}
        with patch.object(ga, "remote_url", return_value="https://bitbucket.org/org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="bitbucket"), \
             patch.object(ga, "get_bitbucket_pr_comments", return_value=empty) as mock_fn:
            result = git_agent_mcp.get_pr_comments()

        mock_fn.assert_called_once()
        assert result == empty

    def test_unsupported_platform_raises_value_error(self):
        with patch.object(ga, "remote_url", return_value="https://codeberg.org/org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="unknown"):
            with pytest.raises(ValueError, match="Unsupported platform"):
                git_agent_mcp.get_pr_comments()
