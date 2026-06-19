"""Unit tests for cash_outlook.py — balance-sheet extraction + outlook compose."""

import json
import os

import pytest

from cash_outlook import extract_balance_sheet, build_outlook

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def balance_sheet():
    with open(os.path.join(FIXTURE_DIR, "balance_sheet.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# extract_balance_sheet
# ---------------------------------------------------------------------------

class TestExtractBalanceSheet:
    def test_cash(self, balance_sheet):
        assert extract_balance_sheet(balance_sheet)["cash"] == 25000.0

    def test_ar(self, balance_sheet):
        assert extract_balance_sheet(balance_sheet)["ar"] == 3000.0

    def test_ap(self, balance_sheet):
        # A/P line is retained for clients who enter bills.
        assert extract_balance_sheet(balance_sheet)["ap"] == 1800.0

    def test_current_liabilities(self, balance_sheet):
        # The figure Cash Outlook treats as "owed" (credit cards + A/P + …).
        assert extract_balance_sheet(balance_sheet)["current_liabilities"] == 2500.0

    def test_missing_returns_none(self):
        assert extract_balance_sheet({"Rows": {"Row": []}}) == \
            {"cash": None, "ar": None, "ap": None, "current_liabilities": None}


# ---------------------------------------------------------------------------
# build_outlook
# ---------------------------------------------------------------------------

def _ar():
    return {"total": 3000.0, "current": 1300.0, "overdue": 1700.0}


class TestBuildOutlook:
    def test_net_position(self):
        o = build_outlook(_ar(), 25000.0, 2500.0)
        assert o["net_position"] == 25000.0 + 3000.0 - 2500.0

    def test_passthrough(self):
        o = build_outlook(_ar(), 25000.0, 2500.0)
        assert o["ar_current"] == 1300.0
        assert o["ar_overdue"] == 1700.0
        assert o["current_liabilities"] == 2500.0

    def test_net_none_when_cash_unavailable(self):
        o = build_outlook(_ar(), None, 2500.0)
        assert o["net_position"] is None
        assert o["cash"] is None

    def test_current_liabilities_none_defaults_zero(self):
        o = build_outlook(_ar(), 25000.0, None)
        assert o["current_liabilities"] == 0.0
        assert o["net_position"] == 25000.0 + 3000.0
