"""Unit tests for vendor_analytics.py — synthetic DataFrames, no API."""

import pandas as pd
import pytest

from vendor_analytics import (
    latest_period,
    current_month_breakdown,
    monthly_share_matrix,
    vendor_yoy,
)


def _df(rows):
    return pd.DataFrame(rows, columns=["year", "month", "vendor", "amount"])


@pytest.fixture
def sample():
    return _df([
        {"year": 2026, "month": 1, "vendor": "Alcon",       "amount": 2000.0},
        {"year": 2026, "month": 1, "vendor": "ABB Optical",  "amount": 1000.0},
        {"year": 2026, "month": 1, "vendor": "B-Lite",       "amount": 1000.0},
        {"year": 2026, "month": 2, "vendor": "Alcon",        "amount": 3000.0},
        {"year": 2026, "month": 2, "vendor": "ABB Optical",  "amount": 1000.0},
        {"year": 2025, "month": 2, "vendor": "Alcon",        "amount": 2500.0},
        {"year": 2025, "month": 1, "vendor": "Alcon",        "amount": 1800.0},
    ])


# ---------------------------------------------------------------------------
# latest_period
# ---------------------------------------------------------------------------

class TestLatestPeriod:
    def test_picks_latest_year_month(self, sample):
        assert latest_period(sample) == (2026, 2)

    def test_empty(self):
        assert latest_period(_df([])) is None


# ---------------------------------------------------------------------------
# current_month_breakdown
# ---------------------------------------------------------------------------

class TestCurrentMonthBreakdown:
    def test_uses_latest_month(self, sample):
        b = current_month_breakdown(sample)
        assert b["month"] == 2
        assert b["month_name"] == "Feb"

    def test_total_correct(self, sample):
        b = current_month_breakdown(sample)
        assert b["total"] == 4000.0  # Alcon 3000 + ABB 1000 in Feb

    def test_shares_sum_to_one(self, sample):
        b = current_month_breakdown(sample)
        assert abs(sum(v["share"] for v in b["vendors"]) - 1.0) < 1e-9

    def test_sorted_descending(self, sample):
        b = current_month_breakdown(sample)
        amounts = [v["amount"] for v in b["vendors"]]
        assert amounts == sorted(amounts, reverse=True)
        assert b["vendors"][0]["vendor"] == "Alcon"

    def test_empty_df(self):
        b = current_month_breakdown(_df([]))
        assert b["vendors"] == []
        assert b["total"] == 0.0

    def test_pinned_period_with_data(self, sample):
        # Pin to Jan rather than the latest month (Feb).
        b = current_month_breakdown(sample, period=(2026, 1))
        assert b["month"] == 1
        assert b["month_name"] == "Jan"
        assert b["total"] == 4000.0  # Alcon 2000 + ABB 1000 + B-Lite 1000

    def test_pinned_period_with_no_rows_shows_that_month(self, sample):
        # June (6) has no rows; pinning must still report June, empty, $0 — not
        # fall back to Feb. This is the in-progress reporting-month case.
        b = current_month_breakdown(sample, period=(2026, 6))
        assert b["year"] == 2026
        assert b["month"] == 6
        assert b["month_name"] == "Jun"
        assert b["total"] == 0.0
        assert b["vendors"] == []


# ---------------------------------------------------------------------------
# monthly_share_matrix
# ---------------------------------------------------------------------------

class TestMonthlyShareMatrix:
    def test_months_for_current_year(self, sample):
        m = monthly_share_matrix(sample)
        assert m["months"] == [1, 2]

    def test_series_lengths_match_months(self, sample):
        m = monthly_share_matrix(sample)
        for vendor, vals in m["series"].items():
            assert len(vals) == len(m["months"])

    def test_top_n_folds_into_other(self):
        rows = []
        for v in range(10):
            rows.append({"year": 2026, "month": 1, "vendor": f"V{v}", "amount": 100 - v})
        m = monthly_share_matrix(_df(rows), top_n=3)
        assert "Other" in m["vendors"]
        assert len(m["vendors"]) == 4  # 3 top + Other

    def test_alcon_values_placed_correctly(self, sample):
        m = monthly_share_matrix(sample)
        jan_idx = m["months"].index(1)
        feb_idx = m["months"].index(2)
        assert m["series"]["Alcon"][jan_idx] == 2000.0
        assert m["series"]["Alcon"][feb_idx] == 3000.0


# ---------------------------------------------------------------------------
# vendor_yoy
# ---------------------------------------------------------------------------

class TestVendorYoy:
    def test_compares_same_month_across_years(self, sample):
        y = vendor_yoy(sample)
        assert y["month_name"] == "Feb"
        assert set(y["years"]) == {2025, 2026}

    def test_alcon_feb_values(self, sample):
        y = vendor_yoy(sample)
        alcon = next(r for r in y["rows"] if r["vendor"] == "Alcon")
        assert alcon["by_year"][2026] == 3000.0
        assert alcon["by_year"][2025] == 2500.0

    def test_missing_year_is_zero(self, sample):
        y = vendor_yoy(sample)
        abb = next(r for r in y["rows"] if r["vendor"] == "ABB Optical")
        # ABB has no Feb 2025
        assert abb["by_year"][2025] == 0.0
        assert abb["by_year"][2026] == 1000.0

    def test_empty(self):
        y = vendor_yoy(_df([]))
        assert y["rows"] == []

    def test_honors_pinned_period(self, sample):
        # Pin to Jan rather than the latest month (Feb).
        y = vendor_yoy(sample, period=(2026, 1))
        assert y["month_name"] == "Jan"
        alcon = next(r for r in y["rows"] if r["vendor"] == "Alcon")
        assert alcon["by_year"][2026] == 2000.0
        assert alcon["by_year"][2025] == 1800.0
