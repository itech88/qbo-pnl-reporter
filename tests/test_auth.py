"""Unit tests for auth.py helpers — no network calls, no token exchange."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from auth import _is_token_expired, _persist_to_github_secrets


class TestIsTokenExpired:
    def test_missing_expiry_returns_true(self):
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": ""}, clear=False):
            assert _is_token_expired() is True

    def test_invalid_expiry_string_returns_true(self):
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": "not-a-date"}, clear=False):
            assert _is_token_expired() is True

    def test_past_expiry_returns_true(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": past}, clear=False):
            assert _is_token_expired() is True

    def test_within_five_minute_buffer_returns_true(self):
        # 3 minutes from now — inside the 5-minute safety window
        soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": soon}, clear=False):
            assert _is_token_expired() is True

    def test_future_expiry_returns_false(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": future}, clear=False):
            assert _is_token_expired() is False

    def test_exactly_at_five_minute_boundary_returns_true(self):
        # Exactly 5 minutes out — should be treated as expired
        boundary = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": boundary}, clear=False):
            assert _is_token_expired() is True

    def test_six_minutes_out_returns_false(self):
        fresh = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
        with patch.dict("os.environ", {"QBO_TOKEN_EXPIRY": fresh}, clear=False):
            assert _is_token_expired() is False


class TestPersistToGithubSecrets:
    """Writeback of rotated tokens to GitHub Secrets — CI-gated, stdin-fed."""

    def test_noop_without_pat(self):
        env = {"GH_PAT": "", "GITHUB_REPOSITORY": "owner/repo"}
        with patch.dict("os.environ", env, clear=False), \
             patch("subprocess.run") as run:
            _persist_to_github_secrets("acc", "ref", "2026-06-01T00:00:00+00:00")
            run.assert_not_called()

    def test_noop_without_repo(self):
        env = {"GH_PAT": "pat123", "GITHUB_REPOSITORY": ""}
        with patch.dict("os.environ", env, clear=False), \
             patch("subprocess.run") as run:
            _persist_to_github_secrets("acc", "ref", "2026-06-01T00:00:00+00:00")
            run.assert_not_called()

    def test_sets_three_secrets_via_stdin(self):
        env = {"GH_PAT": "pat123", "GITHUB_REPOSITORY": "owner/repo"}
        with patch.dict("os.environ", env, clear=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            _persist_to_github_secrets("ACC", "REF", "EXP")

        assert run.call_count == 3
        keys_set, values_fed = set(), set()
        for call in run.call_args_list:
            argv = call.args[0]
            assert argv[:3] == ["gh", "secret", "set"]
            keys_set.add(argv[3])
            # value passed via stdin, never in argv
            assert "--body-file" in argv and "-" in argv
            assert "--body" not in argv  # not the argv-exposing form
            values_fed.add(call.kwargs["input"])
            assert call.kwargs["env"]["GH_TOKEN"] == "pat123"
        assert keys_set == {"QBO_ACCESS_TOKEN", "QBO_REFRESH_TOKEN", "QBO_TOKEN_EXPIRY"}
        assert values_fed == {"ACC", "REF", "EXP"}

    def test_token_value_never_in_argv(self):
        env = {"GH_PAT": "pat123", "GITHUB_REPOSITORY": "owner/repo"}
        with patch.dict("os.environ", env, clear=False), \
             patch("subprocess.run", return_value=MagicMock(returncode=0)) as run:
            _persist_to_github_secrets("SECRET_ACCESS", "SECRET_REFRESH", "EXP")
        for call in run.call_args_list:
            assert "SECRET_ACCESS" not in call.args[0]
            assert "SECRET_REFRESH" not in call.args[0]

    def test_gh_missing_is_handled(self):
        env = {"GH_PAT": "pat123", "GITHUB_REPOSITORY": "owner/repo"}
        with patch.dict("os.environ", env, clear=False), \
             patch("subprocess.run", side_effect=FileNotFoundError()):
            # Must not raise
            _persist_to_github_secrets("acc", "ref", "exp")
