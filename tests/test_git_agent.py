"""Tests for git_agent.py.

Run with:  python3 -m pytest tests/ -v
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test from the repo root (no install needed)
# ---------------------------------------------------------------------------
_REPO = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("git_agent", _REPO / "git_agent.py")
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)


# ===========================================================================
# extract_ticket_id
# ===========================================================================
class TestExtractTicketId:
    def test_jira_style_feature_branch(self):
        assert ga.extract_ticket_id("feature/AUTH-42/google-sso") == "AUTH-42"

    def test_jira_style_fix_branch(self):
        assert ga.extract_ticket_id("fix/PLAT-7-null-pointer") == "PLAT-7"

    def test_linear_style(self):
        assert ga.extract_ticket_id("feature/ENG-123-add-auth") == "ENG-123"

    def test_main_branch_returns_empty(self):
        assert ga.extract_ticket_id("main") == ""

    def test_no_ticket_returns_empty(self):
        assert ga.extract_ticket_id("feature/add-dark-mode") == ""

    def test_custom_pattern_numeric(self, monkeypatch):
        monkeypatch.setenv("TICKET_PATTERN", r"[0-9]+")
        assert ga.extract_ticket_id("issue/123-fix-login") == "123"

    def test_custom_pattern_shortcut(self, monkeypatch):
        monkeypatch.setenv("TICKET_PATTERN", r"sc-[0-9]+")
        assert ga.extract_ticket_id("sc-456/dark-mode") == "sc-456"

    def test_custom_pattern_azure_devops(self, monkeypatch):
        monkeypatch.setenv("TICKET_PATTERN", r"AB#[0-9]+")
        assert ga.extract_ticket_id("feature/AB#789-new-feature") == "AB#789"

    def test_returns_first_match(self):
        # branch with two Jira-like IDs — first one wins
        assert ga.extract_ticket_id("feature/PROJ-1-relates-to-PROJ-2") == "PROJ-1"


# ===========================================================================
# resolve_ticket_url_template
# ===========================================================================
class TestResolveTicketUrlTemplate:
    def test_explicit_template_takes_priority(self, monkeypatch):
        monkeypatch.setenv("TICKET_URL_TEMPLATE", "https://linear.app/t/{id}")
        monkeypatch.setenv("JIRA_BASE_URL", "https://old.atlassian.net")
        assert ga.resolve_ticket_url_template() == "https://linear.app/t/{id}"

    def test_jira_base_url_fallback(self, monkeypatch):
        monkeypatch.delenv("TICKET_URL_TEMPLATE", raising=False)
        monkeypatch.setenv("JIRA_BASE_URL", "https://myorg.atlassian.net")
        assert ga.resolve_ticket_url_template() == "https://myorg.atlassian.net/browse/{id}"

    def test_neither_set_returns_empty(self, monkeypatch):
        monkeypatch.delenv("TICKET_URL_TEMPLATE", raising=False)
        monkeypatch.delenv("JIRA_BASE_URL", raising=False)
        assert ga.resolve_ticket_url_template() == ""


# ===========================================================================
# ticket_url
# ===========================================================================
class TestTicketUrl:
    def test_no_template_returns_bare_id(self):
        assert ga.ticket_url("AUTH-42", "") == "AUTH-42"

    def test_with_template_returns_markdown_link(self):
        tmpl = "https://myorg.atlassian.net/browse/{id}"
        assert ga.ticket_url("AUTH-42", tmpl) == "[AUTH-42](https://myorg.atlassian.net/browse/AUTH-42)"

    def test_reserved_char_hash_is_encoded(self):
        # Azure DevOps: '#' must be percent-encoded in the URL
        tmpl = "https://dev.azure.com/org/proj/_workitems/{id}"
        result = ga.ticket_url("AB#123", tmpl)
        assert result == "[AB#123](https://dev.azure.com/org/proj/_workitems/AB%23123)"
        # Display text should still be readable
        assert result.startswith("[AB#123]")

    def test_spaces_in_id_are_encoded(self):
        tmpl = "https://example.com/issues/{id}"
        result = ga.ticket_url("MY TICKET", tmpl)
        assert "MY%20TICKET" in result
        assert result.startswith("[MY TICKET]")

    def test_empty_ticket_id_with_template(self):
        # ticket_url should still return the bare (empty) string gracefully
        assert ga.ticket_url("", "") == ""


# ===========================================================================
# detect_platform
# ===========================================================================
class TestDetectPlatform:
    @pytest.mark.parametrize("url,expected", [
        ("git@github.com:org/repo.git", "github"),
        ("https://github.com/org/repo.git", "github"),
        ("git@gitlab.com:org/repo.git", "gitlab"),
        ("https://gitlab.com/org/team/repo.git", "gitlab"),
        ("https://gitlab.mycompany.com/org/repo.git", "gitlab"),
        ("git@bitbucket.org:org/repo.git", "bitbucket"),
        ("https://bitbucket.org/org/repo.git", "bitbucket"),
        ("https://codeberg.org/org/repo.git", "unknown"),
    ])
    def test_platforms(self, url, expected):
        assert ga.detect_platform(url) == expected


# ===========================================================================
# parse_remote_path
# ===========================================================================
class TestParseRemotePath:
    @pytest.mark.parametrize("url,expected", [
        # SCP-like
        ("git@github.com:org/repo.git", "org/repo"),
        ("git@gitlab.com:org/team/repo.git", "org/team/repo"),
        # HTTPS
        ("https://github.com/org/repo.git", "org/repo"),
        ("https://gitlab.com/org/team/sub/repo.git", "org/team/sub/repo"),
        ("https://github.com/org/repo", "org/repo"),
        # ssh://
        ("ssh://git@github.com/org/repo.git", "org/repo"),
        ("ssh://git@gitlab.com/org/team/repo.git", "org/team/repo"),
    ])
    def test_paths(self, url, expected):
        assert ga.parse_remote_path(url) == expected

    def test_gitlab_subgroup_preserved(self):
        url = "git@gitlab.com:company/platform/service.git"
        assert ga.parse_remote_path(url) == "company/platform/service"


# ===========================================================================
# LLM helpers
# ===========================================================================
class TestCallLlm:
    def test_prefers_anthropic_over_openai(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        with patch.object(ga, "call_anthropic", return_value="anthropic msg") as mock_ant, \
             patch.object(ga, "call_openai", return_value="openai msg") as mock_oai:
            result = ga.call_llm("prompt")
        assert result == "anthropic msg"
        mock_ant.assert_called_once()
        mock_oai.assert_not_called()

    def test_falls_back_to_openai(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        with patch.object(ga, "call_openai", return_value="openai msg") as mock_oai:
            result = ga.call_llm("prompt")
        assert result == "openai msg"
        mock_oai.assert_called_once()

    def test_returns_none_when_no_keys(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert ga.call_llm("prompt") is None

    def test_call_anthropic_parses_response(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_resp = {"content": [{"text": "  Add OAuth login  "}]}
        with patch.object(ga, "_http_post", return_value=fake_resp):
            result = ga.call_anthropic("prompt")
        assert result == "Add OAuth login"

    def test_call_openai_parses_response(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        fake_resp = {"choices": [{"message": {"content": "  Fix null pointer  "}}]}
        with patch.object(ga, "_http_post", return_value=fake_resp):
            result = ga.call_openai("prompt")
        assert result == "Fix null pointer"


# ===========================================================================
# commit_prompt / pr_body_prompt
# ===========================================================================
class TestPromptGeneration:
    def test_commit_prompt_includes_ticket_id(self):
        prompt = ga.commit_prompt("AUTH-42", "feature/AUTH-42/sso",
                                  "abc1234 prev commit", "file.py | 10 +++",
                                  "diff content", "OAuth via Google")
        assert "AUTH-42" in prompt
        assert "OAuth via Google" in prompt
        assert "[PROJ-123] Title" in prompt  # format example

    def test_commit_prompt_handles_missing_ticket(self):
        prompt = ga.commit_prompt("", "main", "", "stat", "diff", "")
        assert "Ticket ID: none" in prompt

    def test_pr_body_prompt_includes_ticket_link(self):
        link = "[AUTH-42](https://myorg.atlassian.net/browse/AUTH-42)"
        prompt = ga.pr_body_prompt("AUTH-42", link, "Add SSO", "stat", "context")
        assert link in prompt
        assert "## Ticket" in prompt

    def test_pr_body_prompt_no_ticket_section_when_empty(self):
        prompt = ga.pr_body_prompt("", "", "Add SSO", "stat", "")
        assert "## Ticket" not in prompt


# ===========================================================================
# create_github_pr
# ===========================================================================
class TestCreateGithubPr:
    def test_uses_gh_cli(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://github.com/org/repo/pull/1\n")
            url = ga.create_github_pr("Title", "Body", "feature/x", "main", False)
        assert url == "https://github.com/org/repo/pull/1"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"
        assert "--draft" not in cmd

    def test_gh_cli_adds_draft_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://github.com/org/repo/pull/2\n")
            ga.create_github_pr("Title", "Body", "feature/x", "main", True)
        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd

    def test_dies_without_gh(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.create_github_pr("T", "B", "feat/x", "main", False)


# ===========================================================================
# create_gitlab_mr
# ===========================================================================
class TestCreateGitlabMr:
    def test_uses_glab_cli(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://gitlab.com/org/repo/-/merge_requests/1\n")
            url = ga.create_gitlab_mr("Title", "Body", "feature/x", "main", False)
        assert "merge_requests" in url

    def test_draft_mr_adds_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://gitlab.com/org/repo/-/merge_requests/2\n")
            ga.create_gitlab_mr("Title", "Body", "feature/x", "main", True)
        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd

    def test_dies_without_glab(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.create_gitlab_mr("T", "B", "feat/x", "main", False)


# ===========================================================================
# create_bitbucket_pr
# ===========================================================================
class TestCreateBitbucketPr:
    def test_uses_bkt_cli(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://bitbucket.org/org/repo/pull-requests/1\n")
            url = ga.create_bitbucket_pr("Title", "Body", "feature/x", "main", False)
        assert url == "https://bitbucket.org/org/repo/pull-requests/1"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bkt"
        assert "--source" in cmd
        assert "--target" in cmd

    def test_bkt_cmd_structure(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("My PR", "Body", "feature/x", "main", False)
        cmd = mock_run.call_args[0][0]
        assert cmd == ["bkt", "pr", "create",
                       "--title", "My PR", "--source", "feature/x", "--target", "main"]

    def test_draft_warns_and_creates_anyway(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run, \
             patch.object(ga, "warn") as mock_warn:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("Title", "Body", "feature/x", "main", True)
        mock_warn.assert_called_once()
        assert "draft" in mock_warn.call_args[0][0].lower()
        mock_run.assert_called_once()

    def test_dies_without_bkt(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.create_bitbucket_pr("T", "B", "feat/x", "main", False)


# ===========================================================================
# generate_commit_msg (no LLM — editor / stdin path)
# ===========================================================================
class TestGenerateCommitMsg:
    def test_uses_llm_result_when_available(self):
        with patch.object(ga, "call_llm", return_value="Add OAuth login"):
            result = ga.generate_commit_msg("AUTH-42", "feat/AUTH-42", "",
                                            "stat", "diff", "")
        assert result == "Add OAuth login"

    def test_stdin_fallback_when_no_llm_no_editor(self, monkeypatch, capsys):
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        with patch.object(ga, "call_llm", return_value=None), \
             patch("sys.stdin.read", return_value="My manual message"):
            result = ga.generate_commit_msg("", "main", "", "stat", "diff", "")
        assert result == "My manual message"

    def test_editor_path_uses_ticket_prefix(self, monkeypatch, tmp_path):
        monkeypatch.setenv("EDITOR", "true")  # 'true' command: opens and exits 0
        # Simulate: editor writes nothing meaningful, so we pre-populate the file
        edited_content = "[AUTH-42] Add SSO login\n"
        with patch.object(ga, "call_llm", return_value=None), \
             patch("subprocess.run") as mock_sub, \
             patch("builtins.open", create=True) as mock_open, \
             patch("tempfile.NamedTemporaryFile") as mock_tmp, \
             patch("os.unlink"):
            # Setup tempfile mock
            mock_file = MagicMock()
            mock_file.__enter__ = MagicMock(return_value=mock_file)
            mock_file.__exit__ = MagicMock(return_value=False)
            mock_file.name = str(tmp_path / "msg.txt")
            mock_tmp.return_value = mock_file
            # Setup open mock for reading back
            mock_read = MagicMock()
            mock_read.__enter__ = MagicMock(return_value=mock_read)
            mock_read.__exit__ = MagicMock(return_value=False)
            mock_read.read.return_value = edited_content
            mock_open.return_value = mock_read
            mock_sub.return_value = MagicMock(returncode=0)

            result = ga.generate_commit_msg("AUTH-42", "feat/AUTH-42", "",
                                            "stat", "diff", "")
        assert "AUTH-42" in result


# ===========================================================================
# generate_pr_body fallback (no LLM)
# ===========================================================================
class TestGeneratePrBody:
    def test_uses_llm_result_when_available(self):
        with patch.object(ga, "call_llm", return_value="## Summary\n- Does thing"):
            result = ga.generate_pr_body("AUTH-42", "[AUTH-42](url)", "msg", "stat", "")
        assert result == "## Summary\n- Does thing"

    def test_fallback_includes_ticket_section(self):
        with patch.object(ga, "call_llm", return_value=None):
            result = ga.generate_pr_body(
                "AUTH-42", "[AUTH-42](https://example.com)",
                "Add SSO\n\n- Used httpOnly cookies", "stat", ""
            )
        assert "## Ticket" in result
        assert "[AUTH-42](https://example.com)" in result

    def test_fallback_omits_ticket_section_when_empty(self):
        with patch.object(ga, "call_llm", return_value=None):
            result = ga.generate_pr_body("", "", "Add SSO", "stat", "")
        assert "## Ticket" not in result

    def test_fallback_includes_context_section(self):
        with patch.object(ga, "call_llm", return_value=None):
            result = ga.generate_pr_body("", "", "Add SSO", "stat", "Design decision: use JWTs")
        assert "## Context" in result
        assert "use JWTs" in result
