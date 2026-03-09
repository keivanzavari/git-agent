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

    def test_bitbucket_server_detected_via_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_SERVER_URL", "https://bitbucket.mycompany.com")
        assert ga.detect_platform("git@bitbucket.mycompany.com:PROJECT/repo.git") == "bitbucket"

    def test_bitbucket_server_https_detected_via_env(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_SERVER_URL", "https://bitbucket.mycompany.com")
        assert ga.detect_platform("https://bitbucket.mycompany.com/scm/PROJECT/repo.git") == "bitbucket"

    def test_non_matching_server_url_stays_unknown(self, monkeypatch):
        monkeypatch.setenv("BITBUCKET_SERVER_URL", "https://bitbucket.mycompany.com")
        assert ga.detect_platform("https://other-host.com/org/repo.git") == "unknown"

    def test_no_env_var_non_bitbucket_stays_unknown(self, monkeypatch):
        monkeypatch.delenv("BITBUCKET_SERVER_URL", raising=False)
        assert ga.detect_platform("https://bitbucket.mycompany.com/scm/P/r.git") == "unknown"


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
# parse_bitbucket_server_path
# ===========================================================================
class TestParseBitbucketServerPath:
    @pytest.mark.parametrize("url,expected_project,expected_repo", [
        # SCP-like SSH
        ("git@bitbucket.mycompany.com:PROJECT/my-repo.git", "PROJECT", "my-repo"),
        # SSH URI with port
        ("ssh://git@bitbucket.mycompany.com:7999/PROJECT/my-repo.git", "PROJECT", "my-repo"),
        # HTTPS with scm/ prefix (standard Bitbucket Server)
        ("https://bitbucket.mycompany.com/scm/PROJECT/my-repo.git", "PROJECT", "my-repo"),
        # HTTPS without scm/ prefix
        ("https://bitbucket.mycompany.com/PROJECT/my-repo.git", "PROJECT", "my-repo"),
        # Uppercase SCM prefix
        ("https://bitbucket.mycompany.com/SCM/MYPROJ/repo.git", "MYPROJ", "repo"),
    ])
    def test_extracts_project_and_repo(self, url, expected_project, expected_repo):
        project, repo = ga.parse_bitbucket_server_path(url)
        assert project == expected_project
        assert repo == expected_repo

    def test_returns_empty_strings_for_unrecognised_url(self):
        project, repo = ga.parse_bitbucket_server_path("not-a-url")
        assert project == ""
        assert repo == ""


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
    # ── bb-cli (preferred) ──────────────────────────────────────────────────

    def test_prefers_bb_over_bkt(self):
        """When bb is available it is used and bkt is never called."""
        def cmd_exists(name):
            return name == "bb"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "remote_url", return_value="git@bitbucket.mycompany.com:PROJ/repo.git"), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://bb.example.com/projects/PROJ/repos/repo/pull-requests/1\n")
            url = ga.create_bitbucket_pr("Title", "Body", "feature/x", "main", False)

        assert url == "https://bb.example.com/projects/PROJ/repos/repo/pull-requests/1"
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bb"

    def test_bb_cmd_passes_title_description_source_target(self):
        def cmd_exists(name):
            return name == "bb"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:PROJ/repo.git"), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("My PR", "Some body", "feature/x", "main", False)

        cmd = mock_run.call_args[0][0]
        assert "--title" in cmd and cmd[cmd.index("--title") + 1] == "My PR"
        assert "--description" in cmd and cmd[cmd.index("--description") + 1] == "Some body"
        assert "--source" in cmd and cmd[cmd.index("--source") + 1] == "feature/x"
        assert "--target" in cmd and cmd[cmd.index("--target") + 1] == "main"
        assert "--draft" not in cmd

    def test_bb_cmd_includes_project_and_repo(self):
        def cmd_exists(name):
            return name == "bb"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "remote_url", return_value="https://bb.myco.com/scm/MYPROJ/myrepo.git"), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("T", "B", "feat", "main", False)

        cmd = mock_run.call_args[0][0]
        assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "MYPROJ"
        assert "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "myrepo"

    def test_bb_cmd_adds_draft_flag(self):
        def cmd_exists(name):
            return name == "bb"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:P/r.git"), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("T", "B", "feat", "main", True)

        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd

    # ── bkt fallback ────────────────────────────────────────────────────────

    def test_falls_back_to_bkt_when_bb_absent(self):
        def cmd_exists(name):
            return name == "bkt"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://bitbucket.org/org/repo/pull-requests/1\n")
            url = ga.create_bitbucket_pr("Title", "", "feature/x", "main", False)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "bkt"
        assert url == "https://bitbucket.org/org/repo/pull-requests/1"

    def test_bkt_draft_warns_and_creates_anyway(self):
        def cmd_exists(name):
            return name == "bkt"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "run") as mock_run, \
             patch.object(ga, "warn") as mock_warn:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("Title", "", "feature/x", "main", True)

        warn_messages = [c[0][0].lower() for c in mock_warn.call_args_list]
        assert any("draft" in m for m in warn_messages)
        mock_run.assert_called_once()

    def test_bkt_pr_body_warns_and_creates_anyway(self):
        def cmd_exists(name):
            return name == "bkt"

        with patch.object(ga, "_cmd_exists", side_effect=cmd_exists), \
             patch.object(ga, "run") as mock_run, \
             patch.object(ga, "warn") as mock_warn:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.create_bitbucket_pr("Title", "Some body text", "feature/x", "main", False)

        warn_messages = [c[0][0].lower() for c in mock_warn.call_args_list]
        assert any("description" in m for m in warn_messages)
        mock_run.assert_called_once()

    def test_dies_without_bb_or_bkt(self):
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


# ===========================================================================
# get_github_pr_comments
# ===========================================================================
class TestGetGithubPrComments:
    _sample_json = json.dumps({
        "number": 42,
        "comments": [
            {
                "author": {"login": "alice"},
                "body": "Please add tests",
                "createdAt": "2026-03-05T10:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "bob"},
                "body": "Looks good, minor nit",
                "submittedAt": "2026-03-05T11:00:00Z",
                "state": "CHANGES_REQUESTED",
            },
            {
                "author": {"login": "carol"},
                "body": "",
                "submittedAt": "2026-03-05T12:00:00Z",
                "state": "APPROVED",
            },
        ],
    })

    def test_returns_comments_and_reviews(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "capture", return_value=self._sample_json):
            result = ga.get_github_pr_comments()

        assert result["pr_number"] == 42
        authors = {c["author"] for c in result["comments"]}
        assert "alice" in authors
        assert "bob" in authors

    def test_no_pr_returns_empty(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "capture", return_value=""):
            result = ga.get_github_pr_comments()

        assert result == {"pr_number": None, "comments": []}

    def test_empty_review_body_skipped(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "capture", return_value=self._sample_json):
            result = ga.get_github_pr_comments()

        # carol approved with empty body — should not appear
        authors = [c["author"] for c in result["comments"]]
        assert "carol" not in authors

    def test_dies_without_gh_cli(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.get_github_pr_comments()


# ===========================================================================
# get_gitlab_mr_comments
# ===========================================================================
class TestGetGitlabMrComments:
    _sample_json = json.dumps({
        "iid": 7,
        "notes": [
            {
                "author": {"username": "dave"},
                "body": "Fix the typo",
                "created_at": "2026-03-06T09:00:00Z",
            }
        ],
    })

    def test_returns_notes_as_comments(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "capture", return_value=self._sample_json):
            result = ga.get_gitlab_mr_comments()

        assert result["pr_number"] == 7
        assert len(result["comments"]) == 1
        assert result["comments"][0]["author"] == "dave"
        assert result["comments"][0]["body"] == "Fix the typo"

    def test_no_mr_returns_empty(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "capture", return_value=""):
            result = ga.get_gitlab_mr_comments()

        assert result == {"pr_number": None, "comments": []}

    def test_dies_without_glab_cli(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.get_gitlab_mr_comments()


# ===========================================================================
# get_bitbucket_pr_comments
# ===========================================================================
class TestGetBitbucketPrComments:
    _pr_list_json = json.dumps({
        "pullRequests": [
            {"id": 7, "fromBranch": "feature/AUTH-42/sso", "toBranch": "main"},
        ],
        "totalCount": 1,
        "pagination": {"isLastPage": True}
    })

    _comments_json = json.dumps({
        "values": [
            {
                "id": 1,
                "text": "Please add tests",
                "author": {"displayName": "Alice", "name": "alice"},
                "createdDate": 1700000000000,
                "state": "OPEN",
                "deleted": False,
            },
            {
                "id": 2,
                "text": "",          # empty — should be skipped
                "author": {"displayName": "Bob"},
                "createdDate": 1700000001000,
                "state": "OPEN",
                "deleted": False,
            },
            {
                "id": 3,
                "text": "Deleted comment",
                "author": {"displayName": "Carol"},
                "createdDate": 1700000002000,
                "state": "OPEN",
                "deleted": True,    # deleted — should be skipped
            },
        ]
    })

    def test_returns_comments_for_current_branch(self):
        def fake_capture(cmd, **kwargs):
            if "list" in cmd:
                return self._pr_list_json
            if "comments" in cmd:
                return self._comments_json
            return ""

        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "capture", side_effect=fake_capture):
            result = ga.get_bitbucket_pr_comments()

        assert result["pr_number"] == 7
        assert len(result["comments"]) == 1
        assert result["comments"][0]["author"] == "Alice"
        assert result["comments"][0]["body"] == "Please add tests"
        assert result["comments"][0]["state"] == "OPEN"

    def test_empty_and_deleted_comments_are_skipped(self):
        def fake_capture(cmd, **kwargs):
            if "list" in cmd:
                return self._pr_list_json
            if "comments" in cmd:
                return self._comments_json
            return ""

        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "capture", side_effect=fake_capture):
            result = ga.get_bitbucket_pr_comments()

        authors = [c["author"] for c in result["comments"]]
        assert "Bob" not in authors    # empty text
        assert "Carol" not in authors  # deleted

    def test_no_pr_for_branch_returns_empty(self):
        pr_list = json.dumps({"pullRequests": [], "totalCount": 0})
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "capture", return_value=pr_list):
            result = ga.get_bitbucket_pr_comments()

        assert result == {"pr_number": None, "comments": []}

    def test_warns_and_returns_empty_when_bb_not_installed(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             patch.object(ga, "warn") as mock_warn:
            result = ga.get_bitbucket_pr_comments()

        assert result == {"pr_number": None, "comments": []}
        mock_warn.assert_called_once()
        assert "bb" in mock_warn.call_args[0][0].lower()

    def test_pr_list_command_used_before_comments(self):
        captured_cmds = []

        def fake_capture(cmd, **kwargs):
            captured_cmds.append(cmd)
            if "list" in cmd:
                return self._pr_list_json
            if "comments" in cmd:
                return self._comments_json
            return ""

        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "capture", side_effect=fake_capture):
            ga.get_bitbucket_pr_comments()

        assert any("list" in cmd for cmd in captured_cmds)
        assert any("comments" in cmd for cmd in captured_cmds)


# ===========================================================================
# update_github_pr
# ===========================================================================
class TestUpdateGithubPr:
    def test_edit_title_and_body(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://github.com/org/repo/pull/1\n")
            ga.update_github_pr(title="New title", body="New body")

        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["gh", "pr", "edit"]
        assert "--title" in cmd and cmd[cmd.index("--title") + 1] == "New title"
        assert "--body"  in cmd and cmd[cmd.index("--body")  + 1] == "New body"

    def test_edit_base_branch(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_github_pr(base="release/v2")

        cmd = mock_run.call_args[0][0]
        assert "--base" in cmd and cmd[cmd.index("--base") + 1] == "release/v2"

    def test_draft_true_calls_ready_undo(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_github_pr(draft=True)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert ["gh", "pr", "ready", "--undo"] in cmds

    def test_draft_false_calls_ready(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_github_pr(draft=False)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert ["gh", "pr", "ready"] in cmds

    def test_draft_none_does_not_call_ready(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_github_pr(title="T", draft=None)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any("ready" in cmd for cmd in cmds)

    def test_no_edit_call_when_only_draft_changes(self):
        """gh pr edit should not be called when only draft is being toggled."""
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_github_pr(draft=False)

        cmds = [c[0][0] for c in mock_run.call_args_list]
        assert not any(cmd[:3] == ["gh", "pr", "edit"] for cmd in cmds)

    def test_dies_without_gh(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.update_github_pr(title="T")


# ===========================================================================
# update_gitlab_mr
# ===========================================================================
class TestUpdateGitlabMr:
    def test_single_call_with_all_fields(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_gitlab_mr(title="T", body="B", base="develop")

        assert mock_run.call_count == 1
        cmd = mock_run.call_args[0][0]
        assert cmd[0:3] == ["glab", "mr", "update"]
        assert "--title"         in cmd and cmd[cmd.index("--title")         + 1] == "T"
        assert "--description"   in cmd and cmd[cmd.index("--description")   + 1] == "B"
        assert "--target-branch" in cmd and cmd[cmd.index("--target-branch") + 1] == "develop"

    def test_draft_true_adds_draft_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_gitlab_mr(title="T", draft=True)

        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd
        assert "--ready" not in cmd

    def test_draft_false_adds_ready_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_gitlab_mr(title="T", draft=False)

        cmd = mock_run.call_args[0][0]
        assert "--ready" in cmd
        assert "--draft" not in cmd

    def test_nothing_to_update_when_no_fields(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "run") as mock_run:
            result = ga.update_gitlab_mr()

        mock_run.assert_not_called()
        assert "Nothing" in result

    def test_dies_without_glab(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.update_gitlab_mr(title="T")


# ===========================================================================
# update_bitbucket_pr
# ===========================================================================
class TestUpdateBitbucketPr:
    _pr_list_json = json.dumps({
        "pullRequests": [
            {"id": 7, "fromBranch": "feature/AUTH-42/sso", "toBranch": "main"},
        ],
        "totalCount": 1,
    })

    def test_finds_pr_by_branch_and_calls_update(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:PROJ/repo.git"), \
             patch.object(ga, "capture", return_value=self._pr_list_json), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="https://bb.myco.com/projects/PROJ/repos/repo/pull-requests/7\n")
            ga.update_bitbucket_pr(title="New title")

        cmd = mock_run.call_args[0][0]
        assert cmd[0:4] == ["bb", "pr", "update", "7"]
        assert "--title" in cmd and cmd[cmd.index("--title") + 1] == "New title"

    def test_passes_project_and_repo(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "remote_url", return_value="https://bb.myco.com/scm/MYPROJ/myrepo.git"), \
             patch.object(ga, "capture", return_value=self._pr_list_json), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_bitbucket_pr(title="T")

        cmd = mock_run.call_args[0][0]
        assert "--project" in cmd and cmd[cmd.index("--project") + 1] == "MYPROJ"
        assert "--repo"    in cmd and cmd[cmd.index("--repo")    + 1] == "myrepo"

    def test_draft_true_passes_draft_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:P/r.git"), \
             patch.object(ga, "capture", return_value=self._pr_list_json), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_bitbucket_pr(draft=True)

        cmd = mock_run.call_args[0][0]
        assert "--draft" in cmd
        assert "--ready" not in cmd

    def test_draft_false_passes_ready_flag(self):
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="feature/AUTH-42/sso"), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:P/r.git"), \
             patch.object(ga, "capture", return_value=self._pr_list_json), \
             patch.object(ga, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(stdout="url\n")
            ga.update_bitbucket_pr(draft=False)

        cmd = mock_run.call_args[0][0]
        assert "--ready" in cmd
        assert "--draft" not in cmd

    def test_dies_when_no_pr_found_for_branch(self):
        empty_list = json.dumps({"pullRequests": [], "totalCount": 0})
        with patch.object(ga, "_cmd_exists", return_value=True), \
             patch.object(ga, "current_branch", return_value="main"), \
             patch.object(ga, "remote_url", return_value="git@bb.myco.com:P/r.git"), \
             patch.object(ga, "capture", return_value=empty_list), \
             pytest.raises(SystemExit):
            ga.update_bitbucket_pr(title="T")

    def test_dies_without_bb(self):
        with patch.object(ga, "_cmd_exists", return_value=False), \
             pytest.raises(SystemExit):
            ga.update_bitbucket_pr(title="T")


# ===========================================================================
# update_pr dispatch
# ===========================================================================
class TestUpdatePrDispatch:
    def test_github_dispatches_to_github_fn(self):
        with patch.object(ga, "remote_url", return_value="git@github.com:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="github"), \
             patch.object(ga, "update_github_pr", return_value="url") as mock_fn:
            result = ga.update_pr(title="T", body="B", base="main", draft=False)

        mock_fn.assert_called_once_with("T", "B", "main", False)
        assert result == "url"

    def test_gitlab_dispatches_to_gitlab_fn(self):
        with patch.object(ga, "remote_url", return_value="git@gitlab.com:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="gitlab"), \
             patch.object(ga, "update_gitlab_mr", return_value="url") as mock_fn:
            result = ga.update_pr(title="T", draft=True)

        mock_fn.assert_called_once_with("T", "", "", True)

    def test_bitbucket_dispatches_to_bitbucket_fn(self):
        with patch.object(ga, "remote_url", return_value="git@bitbucket.org:org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="bitbucket"), \
             patch.object(ga, "update_bitbucket_pr", return_value="url") as mock_fn:
            result = ga.update_pr(body="New body")

        mock_fn.assert_called_once_with("", "New body", "", None)

    def test_unsupported_platform_raises(self):
        with patch.object(ga, "remote_url", return_value="https://codeberg.org/org/repo.git"), \
             patch.object(ga, "detect_platform", return_value="unknown"):
            with pytest.raises(ValueError, match="Unsupported platform"):
                ga.update_pr(title="T")
