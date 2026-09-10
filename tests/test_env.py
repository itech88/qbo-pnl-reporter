"""
Unit tests for env.py — the empty-string-tolerant env readers (RB-3).

GitHub Actions injects an empty string for any referenced-but-unset secret. The
naive `float(os.getenv("X", "default"))` idiom raises ValueError on "" — the
default only applies when the var is *absent* — which aborted the whole unattended
run at import. These tests pin the tolerant behaviour so that class of outage
can't come back.
"""

from unittest.mock import patch, MagicMock

import pytest

import analytics
import guardrails
import mailer
from env import env_float, env_int, env_str


class TestEnvStr:
    def test_returns_value_when_set(self):
        with patch.dict("os.environ", {"X": "sendgrid"}, clear=False):
            assert env_str("X", "smtp") == "sendgrid"

    def test_default_when_empty(self):
        # The case CI actually produces for an unset secret.
        with patch.dict("os.environ", {"X": ""}, clear=False):
            assert env_str("X", "smtp") == "smtp"

    def test_default_when_absent(self):
        with patch.dict("os.environ", {}, clear=False):
            assert env_str("QBO_NOT_SET_ANYWHERE", "smtp") == "smtp"


class TestEnvFloat:
    def test_parses_value(self):
        with patch.dict("os.environ", {"X": "0.07"}, clear=False):
            assert env_float("X", 0.05) == pytest.approx(0.07)

    def test_default_when_empty(self):
        # float("") would raise ValueError — this is the RB-3 crash.
        with patch.dict("os.environ", {"X": ""}, clear=False):
            assert env_float("X", 0.05) == pytest.approx(0.05)

    def test_default_when_absent(self):
        assert env_float("QBO_NOT_SET_ANYWHERE", 0.05) == pytest.approx(0.05)

    def test_garbage_still_raises(self):
        # A typo'd value is a real misconfiguration and must fail loudly rather
        # than be silently swallowed by the default.
        with patch.dict("os.environ", {"X": "not-a-number"}, clear=False):
            with pytest.raises(ValueError):
                env_float("X", 0.05)


class TestEnvInt:
    def test_parses_value(self):
        with patch.dict("os.environ", {"X": "465"}, clear=False):
            assert env_int("X", 587) == 465

    def test_default_when_empty(self):
        # int("") would raise ValueError.
        with patch.dict("os.environ", {"X": ""}, clear=False):
            assert env_int("X", 587) == 587

    def test_default_when_absent(self):
        assert env_int("QBO_NOT_SET_ANYWHERE", 587) == 587


class TestEmptySecretsDoNotCrashCallSites:
    """The real call sites CI can hand an empty string to. Each of these raised
    ValueError before RB-3 and took the whole run down with it."""

    _EMPTY = {
        "COGS_VARIANCE_THRESHOLD":  "",
        "RECON_TOLERANCE":          "",
        "SMTP_PORT":                "",
        "GH_PAT_EXPIRY_WARN_DAYS":  "",
        "EMAIL_PROVIDER":           "",
    }

    def test_variance_threshold_falls_back(self):
        with patch.dict("os.environ", self._EMPTY, clear=False):
            assert analytics.variance_threshold() == pytest.approx(0.05)

    def test_recon_tolerance_falls_back(self):
        with patch.dict("os.environ", self._EMPTY, clear=False):
            assert guardrails.tolerance() == pytest.approx(1.00)

    def test_flag_anomalies_resolves_threshold_at_call_time(self):
        import pandas as pd
        df = pd.DataFrame(
            [{"year": 2025, "month": m, "income": 45000, "value": 13500} for m in range(1, 13)],
            columns=["year", "month", "income", "value"],
        )
        with patch.dict("os.environ", self._EMPTY, clear=False):
            analytics.flag_anomalies(df)   # must not raise

    def test_threshold_is_read_at_call_time_not_import_time(self):
        # Changing the env after import must be picked up — proof the value is not
        # frozen into a module global or a function default at import.
        with patch.dict("os.environ", {"COGS_VARIANCE_THRESHOLD": "0.25"}, clear=False):
            assert analytics.variance_threshold() == pytest.approx(0.25)
        with patch.dict("os.environ", {"COGS_VARIANCE_THRESHOLD": "0.10"}, clear=False):
            assert analytics.variance_threshold() == pytest.approx(0.10)

    def test_empty_email_provider_resolves_to_smtp(self):
        # Previously: "" sailed past the getenv default → "Unknown EMAIL_PROVIDER ''".
        # Full fake SMTP env + patched smtplib.SMTP so no real connection is made
        # (patching _send_smtp would miss — _BACKENDS binds it at import).
        env = {
            "EMAIL_PROVIDER": "",       # the empty value CI would inject
            "SMTP_HOST": "smtp.example.com", "SMTP_PORT": "587",
            "SMTP_USER": "bot@example.com", "SMTP_PASSWORD": "pw",
            "EMAIL_FROM": "bot@example.com", "EMAIL_TO": "owner@example.com",
        }
        with patch.dict("os.environ", env, clear=False), \
             patch("smtplib.SMTP") as smtp_cls:
            server = MagicMock()
            smtp_cls.return_value.__enter__.return_value = server
            mailer.send_report("<html>", b"PNG", subject="s", email_to="a@b.com")
        # The SMTP backend ran (provider resolved to "smtp", no ValueError raised).
        smtp_cls.assert_called_once()
        server.sendmail.assert_called_once()
