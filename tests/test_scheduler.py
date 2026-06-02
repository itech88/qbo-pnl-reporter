"""Unit tests for scheduler.py PAT-expiry alerting — no network, mocked email."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import scheduler


def _expiry_in(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


class TestCheckPatExpiry:
    """Self-issued reminder before the un-rotatable GH_PAT lapses."""

    def test_noop_when_expiry_unknown(self):
        # github_pat_expiry returns None locally / on error → no email
        with patch("auth.github_pat_expiry", return_value=None), \
             patch("mailer.send_failure_alert") as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_not_called()

    def test_no_alert_when_healthy(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(90)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert") as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_not_called()

    def test_alerts_within_window(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(10)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()
            subject, body = alert.call_args.args[:2]
            assert "expires in" in subject.lower()
            assert "fine-grained" in body.lower()  # body carries the rotation steps

    def test_alerts_when_expired(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(-2)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()
            subject, _ = alert.call_args.args[:2]
            assert "expired" in subject.lower()

    def test_custom_threshold_widens_window(self):
        # 40 days out is healthy at the default 30 but warns at 45
        with patch("auth.github_pat_expiry", return_value=_expiry_in(40)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "45"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()

    def test_never_raises(self):
        # An exception anywhere in the check must not propagate into the run
        with patch("auth.github_pat_expiry", side_effect=RuntimeError("boom")), \
             patch("mailer.send_failure_alert"):
            scheduler._check_pat_expiry("run123")  # must not raise
