"""Unit tests for mailer.send_failure_alert — no real SMTP."""

from unittest.mock import patch, MagicMock

from mailer import send_failure_alert

_SMTP_ENV = {
    "SMTP_HOST":     "smtp.example.com",
    "SMTP_PORT":     "587",
    "SMTP_USER":     "bot@example.com",
    "SMTP_PASSWORD": "pw",
    "EMAIL_FROM":    "bot@example.com",
    "EMAIL_TO":      "owner@example.com",
}


class TestSendFailureAlert:
    def test_noop_when_smtp_unconfigured(self):
        env = {k: "" for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM")}
        with patch.dict("os.environ", env, clear=False), \
             patch("smtplib.SMTP") as smtp:
            assert send_failure_alert("subj", "body") is False
            smtp.assert_not_called()

    def test_sends_plaintext_via_smtp(self):
        with patch.dict("os.environ", _SMTP_ENV, clear=False), \
             patch("smtplib.SMTP") as smtp_cls:
            server = MagicMock()
            smtp_cls.return_value.__enter__.return_value = server
            assert send_failure_alert("Build failed", "details here") is True
            server.login.assert_called_once_with("bot@example.com", "pw")
            server.sendmail.assert_called_once()
            # recipient + body propagated
            args = server.sendmail.call_args.args
            assert args[0] == "bot@example.com"
            assert args[1] == ["owner@example.com"]
            assert "details here" in args[2]
            assert "Build failed" in args[2]

    def test_respects_email_to_override(self):
        with patch.dict("os.environ", _SMTP_ENV, clear=False), \
             patch("smtplib.SMTP") as smtp_cls:
            server = MagicMock()
            smtp_cls.return_value.__enter__.return_value = server
            send_failure_alert("s", "b", email_to="custom@example.com")
            assert server.sendmail.call_args.args[1] == ["custom@example.com"]

    def test_never_raises_on_smtp_error(self):
        with patch.dict("os.environ", _SMTP_ENV, clear=False), \
             patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            # Best-effort: returns False, does not raise
            assert send_failure_alert("s", "b") is False
