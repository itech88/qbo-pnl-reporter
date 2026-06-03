"""Unit tests for mailer — send_report routing + send_failure_alert, no real SMTP."""

from unittest.mock import patch, MagicMock

import pytest

from mailer import send_failure_alert, send_report

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


class TestSendReport:
    """Provider routing + the inline-chart MIME build."""

    def _env(self, provider):
        return {**_SMTP_ENV, "EMAIL_PROVIDER": provider}

    def test_smtp_backend_sends_multipart_with_inline_chart(self):
        # Exercises the real _send_smtp + _build_mime path end to end.
        with patch.dict("os.environ", self._env("smtp"), clear=False), \
             patch("smtplib.SMTP") as smtp_cls:
            server = MagicMock()
            smtp_cls.return_value.__enter__.return_value = server
            send_report("<html>body</html>", b"\x89PNGfake",
                        subject="COGS Report May", email_to="to@example.com")
            server.sendmail.assert_called_once()
            args = server.sendmail.call_args.args
            assert args[0] == "bot@example.com"     # EMAIL_FROM
            assert args[1] == ["to@example.com"]    # recipient
            sent = args[2]
            assert "Subject: COGS Report May" in sent
            assert "monthly_chart" in sent          # chart embedded as inline CID
            assert "multipart/related" in sent

    def test_routes_to_sendgrid(self):
        backend = MagicMock()
        with patch.dict("os.environ", self._env("sendgrid"), clear=False), \
             patch.dict("mailer._BACKENDS", {"sendgrid": backend}):
            send_report("<html>", b"PNG", subject="S", email_to="x@example.com")
        backend.assert_called_once_with("S", "<html>", b"PNG", "x@example.com")

    def test_routes_to_ses(self):
        backend = MagicMock()
        with patch.dict("os.environ", self._env("ses"), clear=False), \
             patch.dict("mailer._BACKENDS", {"ses": backend}):
            send_report("<html>", b"PNG", subject="S", email_to="x@example.com")
        backend.assert_called_once_with("S", "<html>", b"PNG", "x@example.com")

    def test_unknown_provider_raises(self):
        with patch.dict("os.environ", self._env("carrierpigeon"), clear=False):
            with pytest.raises(ValueError):
                send_report("<html>", b"PNG")

    def test_email_to_defaults_to_env(self):
        backend = MagicMock()
        with patch.dict("os.environ", self._env("smtp"), clear=False), \
             patch.dict("mailer._BACKENDS", {"smtp": backend}):
            send_report("<html>", b"PNG", subject="S")   # no email_to override
        assert backend.call_args.args[3] == "owner@example.com"   # from EMAIL_TO

    def test_default_subject_when_none(self):
        backend = MagicMock()
        with patch.dict("os.environ", self._env("smtp"), clear=False), \
             patch.dict("mailer._BACKENDS", {"smtp": backend}):
            send_report("<html>", b"PNG")   # no subject
        assert backend.call_args.args[0].startswith("COGS Report")
