"""Tests for the aging self-heal — rebuilding buckets when the summary comes back empty.

The condition under test is the real one from 2026-08-16 and 2026-09-09: the
AgedReceivables summary returns no per-party rows, while the detail endpoint and
the Balance Sheet both agree there is money outstanding.
"""

import pandas as pd
import pytest

from aging_analytics import aging_summary
from aging_fetcher import (
    DERIVED_BUCKET_ORDER,
    bucket_for_days,
    derive_buckets_from_detail,
)
from scheduler import _heal_aging_from_detail

_DETAIL_COLS = ["party", "doc_num", "txn_date", "due_date", "open_balance", "days_overdue"]


def _detail(rows):
    """rows: list of (party, open_balance, days_overdue)."""
    return pd.DataFrame(
        [(p, "1", None, None, bal, days) for p, bal, days in rows],
        columns=_DETAIL_COLS,
    )


def _empty_buckets():
    """What the summary parser produced on the days this defect fired."""
    df = pd.DataFrame(columns=["party", "bucket", "amount"])
    df.attrs["bucket_order"] = []
    return df


class TestBucketForDays:
    @pytest.mark.parametrize("days,expected", [
        (None,  "Current"),
        (float("nan"), "Current"),   # what None becomes after a DataFrame round-trip
        (-10,   "Current"),
        (0,     "Current"),
        (1,     "1 - 30"),
        (30,    "1 - 30"),
        (31,    "31 - 60"),
        (60,    "31 - 60"),
        (61,    "61 - 90"),
        (90,    "61 - 90"),
        (91,    "91 and over"),
        (5000,  "91 and over"),
    ])
    def test_ladder_boundaries(self, days, expected):
        assert bucket_for_days(days) == expected

    def test_undated_documents_are_current_not_ancient(self):
        """Ageing an undated document into the oldest bucket would overstate risk."""
        assert bucket_for_days(None) == "Current"


class TestDeriveBucketsFromDetail:
    def test_rebuilds_the_aug_16_case(self):
        out = derive_buckets_from_detail(_detail([("Acme", 50.0, None)]))
        assert float(out["amount"].sum()) == 50.0
        assert aging_summary(out)["total"] == 50.0

    def test_sums_multiple_documents_per_party_and_bucket(self):
        out = derive_buckets_from_detail(_detail([
            ("Acme", 10.0, 5), ("Acme", 15.0, 12), ("Beta", 7.0, 100),
        ]))
        acme = out[(out["party"] == "Acme") & (out["bucket"] == "1 - 30")]
        assert float(acme["amount"].iloc[0]) == 25.0
        assert float(out["amount"].sum()) == 32.0

    def test_spreads_documents_across_buckets(self):
        out = derive_buckets_from_detail(_detail([
            ("Acme", 1.0, None), ("Acme", 2.0, 10),
            ("Acme", 4.0, 45), ("Acme", 8.0, 200),
        ]))
        got = dict(zip(out["bucket"], out["amount"]))
        assert got == {"Current": 1.0, "1 - 30": 2.0, "31 - 60": 4.0, "91 and over": 8.0}

    def test_carries_the_full_ladder_as_bucket_order(self):
        out = derive_buckets_from_detail(_detail([("Acme", 50.0, None)]))
        assert out.attrs["bucket_order"] == DERIVED_BUCKET_ORDER

    def test_empty_detail_yields_an_empty_frame_with_the_right_columns(self):
        out = derive_buckets_from_detail(_detail([]))
        assert out.empty
        assert list(out.columns) == ["party", "bucket", "amount"]

    def test_none_detail_does_not_crash(self):
        assert derive_buckets_from_detail(None).empty

    def test_current_and_overdue_split_survives_the_rebuild(self):
        summ = aging_summary(derive_buckets_from_detail(_detail([
            ("Acme", 30.0, None),   # current
            ("Beta", 70.0, 45),     # overdue
        ])))
        assert summ["current"] == 30.0
        assert summ["overdue"] == 70.0
        assert summ["overdue_pct"] == pytest.approx(0.7)


class TestHealAgingFromDetail:
    """The repair must be conservative: only when the Balance Sheet vouches for the detail."""

    def test_heals_the_real_aug_16_condition(self):
        ddf = _detail([("Acme", 50.0, None)])
        out = _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=50.0,
                                      detail_total=50.0)
        assert out is not None
        _, summ = out
        assert summ["total"] == 50.0

    def test_declines_when_the_detail_disagrees_with_the_balance_sheet(self):
        """Swapping one unverified number for another is not a repair."""
        ddf = _detail([("Acme", 999.0, None)])
        assert _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=50.0,
                                       detail_total=999.0) is None

    def test_declines_without_a_balance_sheet_anchor(self):
        ddf = _detail([("Acme", 50.0, None)])
        assert _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=None,
                                       detail_total=50.0) is None

    def test_declines_without_detail(self):
        assert _heal_aging_from_detail(_detail([]), summary_total=0.0, bs_total=50.0,
                                       detail_total=None) is None

    def test_declines_when_the_summary_was_already_correct(self):
        """No repair is attempted when there is nothing wrong."""
        ddf = _detail([("Acme", 50.0, None)])
        assert _heal_aging_from_detail(ddf, summary_total=50.0, bs_total=50.0,
                                       detail_total=50.0) is None

    def test_declines_when_the_rebuild_would_not_reconcile(self):
        """Self-healing must satisfy the guardrail, never bypass it.

        The detail total ties to the Balance Sheet, but the rows carry no open
        balance, so the rebuild produces nothing and the hold must stand.
        """
        ddf = _detail([("Acme", 0.0, None)])
        assert _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=50.0,
                                       detail_total=50.0) is None

    def test_tolerance_scales_with_the_balance(self):
        """A cent of rounding between two QBO endpoints is not a defect."""
        ddf = _detail([("Acme", 10000.0, None)])
        out = _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=10000.50,
                                      detail_total=10000.0)
        assert out is not None

    def test_a_healed_summary_reconciles_against_the_balance_sheet(self):
        from guardrails import reconcile_balance_sheet
        ddf = _detail([("Acme", 20.0, 5), ("Beta", 30.0, 95)])
        out = _heal_aging_from_detail(ddf, summary_total=0.0, bs_total=50.0,
                                      detail_total=50.0)
        assert out is not None
        _, summ = out
        assert reconcile_balance_sheet(50.0, summ["total"], label="A/R") == []
