"""Unit tests for auth.py helpers — no network calls, no token exchange."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from auth import _is_token_expired


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
