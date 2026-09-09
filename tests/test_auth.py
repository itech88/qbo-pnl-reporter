"""Unit tests for auth.py helpers — no network calls, no token exchange."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from auth import (
    _is_token_expired,
    _is_wrong_cluster,
    _retry_delay,
    _persist_to_github_secrets,
    _parse_pat_expiry,
    github_pat_expiry,
    refresh_tokens,
    QBOSession,
)


_WRONG_CLUSTER_BODY = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<IntuitResponse xmlns="http://schema.intuit.com/finance/v3">'
    '<Fault type="AuthenticationFault"><Error code="130">'
    '<Message>Accessing Wrong Cluster</Message>'
    '<Detail>WrongClusterError: -11017-You have accessed the wrong server.</Detail>'
    '</Error></Fault></IntuitResponse>'
)


class TestIsWrongCluster:
    def _resp(self, status, text=""):
        r = MagicMock()
        r.status_code = status
        r.text = text
        return r

    def test_detects_wrong_cluster_401(self):
        assert _is_wrong_cluster(self._resp(401, _WRONG_CLUSTER_BODY)) is True

    def test_detects_by_code_130(self):
        assert _is_wrong_cluster(self._resp(401, '<Error code="130">x</Error>')) is True

    def test_plain_401_is_not_wrong_cluster(self):
        # a genuine auth failure must still trigger the refresh path
        assert _is_wrong_cluster(self._resp(401, "token expired")) is False

    def test_non_401_is_not_wrong_cluster(self):
        assert _is_wrong_cluster(self._resp(200, _WRONG_CLUSTER_BODY)) is False


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
            assert "--body" not in argv       # not the argv-exposing form
            assert "--body-file" not in argv  # unsupported on older runner gh
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


class TestParsePatExpiry:
    """Tolerant parsing of the github-authentication-token-expiration header."""

    def test_utc_suffix_form(self):
        assert _parse_pat_expiry("2026-06-30 23:59:59 UTC") == \
            datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

    def test_numeric_offset_form(self):
        assert _parse_pat_expiry("2026-06-30 23:59:59 +0000") == \
            datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

    def test_negative_offset_normalizes_to_utc(self):
        # 23:59:59 -0700 == 06:59:59 next day UTC
        assert _parse_pat_expiry("2026-06-30 23:59:59 -0700") == \
            datetime(2026, 7, 1, 6, 59, 59, tzinfo=timezone.utc)

    def test_iso_form(self):
        assert _parse_pat_expiry("2026-06-30T23:59:59+00:00") == \
            datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

    def test_garbage_returns_none(self):
        assert _parse_pat_expiry("not-a-date") is None

    def test_empty_returns_none(self):
        assert _parse_pat_expiry("") is None


class TestGithubPatExpiry:
    """Reads the PAT expiry from GitHub's response header — CI-gated, best-effort."""

    def test_no_pat_skips_request(self):
        with patch.dict("os.environ", {"GH_PAT": ""}, clear=False), \
             patch("auth.requests.get") as get:
            assert github_pat_expiry() is None
            get.assert_not_called()

    def test_reads_expiry_header(self):
        resp = MagicMock(status_code=200,
                         headers={"github-authentication-token-expiration": "2026-06-30 23:59:59 UTC"})
        with patch.dict("os.environ", {"GH_PAT": "pat123"}, clear=False), \
             patch("auth.requests.get", return_value=resp):
            assert github_pat_expiry() == datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)

    def test_missing_header_returns_none(self):
        resp = MagicMock(status_code=200, headers={})
        with patch.dict("os.environ", {"GH_PAT": "pat123"}, clear=False), \
             patch("auth.requests.get", return_value=resp):
            assert github_pat_expiry() is None

    def test_non_200_returns_none(self):
        resp = MagicMock(status_code=401, headers={})
        with patch.dict("os.environ", {"GH_PAT": "pat123"}, clear=False), \
             patch("auth.requests.get", return_value=resp):
            assert github_pat_expiry() is None

    def test_request_exception_returns_none(self):
        import requests
        with patch.dict("os.environ", {"GH_PAT": "pat123"}, clear=False), \
             patch("auth.requests.get", side_effect=requests.RequestException("boom")):
            assert github_pat_expiry() is None

    def test_pat_travels_only_in_auth_header(self):
        resp = MagicMock(status_code=200,
                         headers={"github-authentication-token-expiration": "2026-06-30 23:59:59 UTC"})
        with patch.dict("os.environ", {"GH_PAT": "supersecret"}, clear=False), \
             patch("auth.requests.get", return_value=resp) as get:
            github_pat_expiry()
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer supersecret"


_REFRESH_ENV = {
    "QBO_CLIENT_ID":     "cid",
    "QBO_CLIENT_SECRET": "secret",
    "QBO_REFRESH_TOKEN": "refresh-old",
}


class TestRefreshTokens:
    """OAuth refresh — persists rotated tokens, surfaces failures."""

    def test_success_persists_and_returns(self):
        resp = MagicMock(status_code=200, headers={"intuit_tid": "tid-1"})
        resp.json.return_value = {
            "access_token": "new-access", "refresh_token": "new-refresh",
            "expires_in": 3600, "token_type": "bearer",
        }
        with patch.dict("os.environ", _REFRESH_ENV, clear=False), \
             patch("auth.requests.post", return_value=resp) as post, \
             patch("auth.set_key"), \
             patch("auth._persist_to_github_secrets") as persist:
            token = refresh_tokens()
        assert token == "new-access"
        post.assert_called_once()
        # the rotated refresh token is what gets persisted for the next run
        assert persist.call_args.args[1] == "new-refresh"

    def test_non_200_raises(self):
        resp = MagicMock(status_code=400, text="invalid_grant", headers={"intuit_tid": ""})
        with patch.dict("os.environ", _REFRESH_ENV, clear=False), \
             patch("auth.requests.post", return_value=resp):
            with pytest.raises(RuntimeError):
                refresh_tokens()

    def test_missing_refresh_token_raises(self):
        env = {**_REFRESH_ENV, "QBO_REFRESH_TOKEN": ""}
        with patch.dict("os.environ", env, clear=False), \
             patch("auth.requests.post") as post:
            with pytest.raises(RuntimeError):
                refresh_tokens()
            post.assert_not_called()   # never hits the network without a token


def _http(status, tid="", retry_after=None):
    headers = {"intuit_tid": tid}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return MagicMock(status_code=status, headers=headers, text="body")


class TestQBOSession:
    """Transparent token refresh on expiry and on 401."""

    def test_refreshes_when_token_expired(self):
        with patch.dict("os.environ",
                        {"QBO_TOKEN_EXPIRY": "", "QBO_ACCESS_TOKEN": "tok"}, clear=False), \
             patch("auth.refresh_tokens") as refresh, \
             patch("requests.Session.request", return_value=_http(200)):
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 200
        refresh.assert_called_once()

    def test_retries_once_on_401(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        with patch.dict("os.environ",
                        {"QBO_TOKEN_EXPIRY": future, "QBO_ACCESS_TOKEN": "tok"}, clear=False), \
             patch("auth.refresh_tokens") as refresh, \
             patch("requests.Session.request",
                   side_effect=[_http(401), _http(200)]) as super_req:
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 200
        assert super_req.call_count == 2   # original + one retry
        refresh.assert_called_once()       # refreshed on the 401


class TestRetryDelay:
    """Retry-After is honoured when Intuit sends a usable one."""

    def test_delta_seconds_header_wins(self):
        assert _retry_delay(_http(429, retry_after="7"), fallback=2.0) == 7.0

    def test_http_date_header_falls_back(self):
        resp = _http(503, retry_after="Wed, 01 Sep 2027 20:41:29 GMT")
        assert _retry_delay(resp, fallback=2.0) == 2.0

    def test_missing_header_falls_back(self):
        assert _retry_delay(_http(500), fallback=4.0) == 4.0

    def test_negative_header_is_clamped_to_zero(self):
        assert _retry_delay(_http(500, retry_after="-5"), fallback=2.0) == 0.0


class TestQBOSessionTransientRetry:
    """A transient 5xx/429 must not abort the run — the Sept 1 failure mode."""

    def _live_env(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        return {"QBO_TOKEN_EXPIRY": future, "QBO_ACCESS_TOKEN": "tok"}

    def test_retries_500_then_succeeds(self):
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request",
                   side_effect=[_http(500), _http(200)]) as super_req:
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 200
        assert super_req.call_count == 2
        sleep.assert_called_once_with(2.0)

    def test_backoff_is_exponential_across_attempts(self):
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request",
                   side_effect=[_http(503), _http(503), _http(503), _http(200)]):
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 200
        assert [c.args[0] for c in sleep.call_args_list] == [2.0, 4.0, 8.0]

    def test_gives_up_after_max_attempts_and_returns_last_response(self):
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request",
                   return_value=_http(500)) as super_req:
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 500
        assert super_req.call_count == 4      # QBO_MAX_ATTEMPTS default
        assert sleep.call_count == 3          # no sleep after the final attempt

    def test_retry_after_header_overrides_backoff(self):
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request",
                   side_effect=[_http(429, retry_after="11"), _http(200)]):
            QBOSession().request("GET", "https://example.com/x")
        sleep.assert_called_once_with(11.0)

    def test_attempt_count_is_configurable(self):
        env = {**self._live_env(), "QBO_MAX_ATTEMPTS": "2"}
        with patch.dict("os.environ", env, clear=False), \
             patch("auth.time.sleep"), \
             patch("requests.Session.request",
                   return_value=_http(500)) as super_req:
            QBOSession().request("GET", "https://example.com/x")
        assert super_req.call_count == 2

    def test_client_errors_are_not_retried(self):
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request",
                   return_value=_http(400)) as super_req:
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 400
        assert super_req.call_count == 1
        sleep.assert_not_called()

    def test_wrong_cluster_401_is_not_retried_or_refreshed(self):
        resp401 = MagicMock(
            status_code=401, headers={"intuit_tid": ""}, text=_WRONG_CLUSTER_BODY,
        )
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.refresh_tokens") as refresh, \
             patch("auth.time.sleep") as sleep, \
             patch("requests.Session.request", return_value=resp401) as super_req:
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 401
        assert super_req.call_count == 1
        refresh.assert_not_called()
        sleep.assert_not_called()

    def test_token_refresh_happens_at_most_once_per_request(self):
        """A 401 that keeps coming back must not rotate the refresh token twice —
        concurrent/repeated rotation is what strands the chain on invalid_grant."""
        with patch.dict("os.environ", self._live_env(), clear=False), \
             patch("auth.refresh_tokens") as refresh, \
             patch("auth.time.sleep"), \
             patch("requests.Session.request",
                   side_effect=[_http(401), _http(500), _http(200)]):
            resp = QBOSession().request("GET", "https://example.com/x")
        assert resp.status_code == 200
        refresh.assert_called_once()

