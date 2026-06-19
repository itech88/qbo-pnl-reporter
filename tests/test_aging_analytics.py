"""Unit tests for aging_analytics.py — pure views, no network."""

from datetime import date

import pandas as pd
import pytest

from aging_analytics import (
    aging_summary,
    party_breakdown,
    oldest_items,
    days_outstanding,
)


def _buckets_df():
    rows = [
        ("VSP",              "Current",     1000.0),
        ("VSP",              "1 - 30",       500.0),
        ("EyeMed",           "Current",      200.0),
        ("EyeMed",           "31 - 60",      300.0),
        ("EyeMed",           "91 and over",  800.0),
        ("Patient Balances", "Current",      100.0),
        ("Patient Balances", "91 and over",  100.0),
    ]
    df = pd.DataFrame(rows, columns=["party", "bucket", "amount"])
    df.attrs["bucket_order"] = ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over"]
    return df


def _detail_df():
    return pd.DataFrame([
        {"party": "EyeMed", "doc_num": "2002", "txn_date": date(2026, 2, 1),
         "due_date": date(2026, 2, 1), "open_balance": 800.0, "days_overdue": 137},
        {"party": "VSP", "doc_num": "1001", "txn_date": date(2026, 6, 10),
         "due_date": date(2026, 6, 10), "open_balance": 1000.0, "days_overdue": 8},
        {"party": "Patient Balances", "doc_num": "3002", "txn_date": date(2026, 1, 15),
         "due_date": date(2026, 1, 15), "open_balance": 100.0, "days_overdue": 154},
        {"party": "Paid", "doc_num": "9999", "txn_date": date(2026, 1, 1),
         "due_date": date(2026, 1, 1), "open_balance": 0.0, "days_overdue": 168},
    ])


# ---------------------------------------------------------------------------
# aging_summary
# ---------------------------------------------------------------------------

class TestAgingSummary:
    def test_totals(self):
        s = aging_summary(_buckets_df())
        assert s["total"] == 3000.0
        assert s["current"] == 1300.0
        assert s["overdue"] == 1700.0

    def test_overdue_pct(self):
        s = aging_summary(_buckets_df())
        assert round(s["overdue_pct"], 4) == round(1700.0 / 3000.0, 4)

    def test_buckets_follow_column_order(self):
        s = aging_summary(_buckets_df())
        assert [b["label"] for b in s["buckets"]] == \
            ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over"]
        assert s["buckets"][0]["overdue"] is False
        assert s["buckets"][-1]["overdue"] is True

    def test_empty(self):
        df = pd.DataFrame(columns=["party", "bucket", "amount"])
        s = aging_summary(df)
        assert s["total"] == 0.0 and s["buckets"] == []


# ---------------------------------------------------------------------------
# party_breakdown
# ---------------------------------------------------------------------------

class TestPartyBreakdown:
    def test_sorted_by_total_desc(self):
        b = party_breakdown(_buckets_df())
        totals = [r["total"] for r in b["rows"]]
        assert totals == sorted(totals, reverse=True)
        assert b["rows"][0]["party"] == "VSP"   # 1500 is the largest

    def test_shares_sum_to_one(self):
        b = party_breakdown(_buckets_df())
        assert round(sum(r["share"] for r in b["rows"]), 4) == 1.0

    def test_other_folding(self):
        b = party_breakdown(_buckets_df(), top_n=2)
        assert b["rows"][-1]["party"] == "Other"
        # Grand total is preserved across the fold
        assert round(sum(r["total"] for r in b["rows"]), 2) == 3000.0


# ---------------------------------------------------------------------------
# oldest_items
# ---------------------------------------------------------------------------

class TestOldestItems:
    def test_orders_oldest_first(self):
        items = oldest_items(_detail_df())
        assert items[0]["doc_num"] == "3002"   # 154 days
        assert items[1]["doc_num"] == "2002"   # 137 days

    def test_excludes_zero_open_balance(self):
        items = oldest_items(_detail_df())
        assert all(it["doc_num"] != "9999" for it in items)

    def test_top_n_limits(self):
        assert len(oldest_items(_detail_df(), top_n=1)) == 1

    def test_empty(self):
        assert oldest_items(pd.DataFrame(
            columns=["party", "doc_num", "txn_date", "due_date",
                     "open_balance", "days_overdue"])) == []


# ---------------------------------------------------------------------------
# days_outstanding
# ---------------------------------------------------------------------------

class TestDaysOutstanding:
    def test_computes(self):
        # $3000 outstanding at $100/day revenue → 30 days
        assert days_outstanding(3000.0, 100.0) == 30.0

    def test_none_when_no_rate(self):
        assert days_outstanding(3000.0, None) is None
        assert days_outstanding(3000.0, 0.0) is None
