"""Unit tests for aging_fetcher.py — pure parsing, no API calls."""

import json
import os
from datetime import date

import pytest

from aging_fetcher import (
    _to_float,
    _parse_date,
    _resolve_party,
    _summary_columns,
    _parse_summary,
    _detail_col_indices,
    build_aging_dataframe,
    _UNASSIGNED,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
AS_OF = date(2026, 6, 18)


@pytest.fixture
def ar_summary():
    with open(os.path.join(FIXTURE_DIR, "aged_receivable_summary.json")) as f:
        return json.load(f)


@pytest.fixture
def ar_detail():
    with open(os.path.join(FIXTURE_DIR, "aged_receivable_detail.json")) as f:
        return json.load(f)


@pytest.fixture
def raw(ar_summary, ar_detail):
    return {"summary": ar_summary, "detail": ar_detail}


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------

class TestScalars:
    def test_to_float_plain(self):     assert _to_float("1,500.00") == 1500.0
    def test_to_float_empty(self):     assert _to_float("") == 0.0
    def test_to_float_dollar(self):    assert _to_float("$3,000") == 3000.0
    def test_to_float_garbage(self):   assert _to_float("n/a") == 0.0
    def test_parse_date(self):         assert _parse_date("2026-05-20") == date(2026, 5, 20)
    def test_parse_date_blank(self):   assert _parse_date("") is None
    def test_parse_date_bad(self):     assert _parse_date("not-a-date") is None


class TestResolveParty:
    def test_passthrough(self):
        assert _resolve_party("VSP", {}) == "VSP"

    def test_blank_is_unassigned(self):
        assert _resolve_party("", {}) == _UNASSIGNED

    def test_alias_canonicalizes(self):
        assert _resolve_party("VSP", {"vsp": "VSP Vision"}) == "VSP Vision"


# ---------------------------------------------------------------------------
# Summary parsing
# ---------------------------------------------------------------------------

class TestSummary:
    def test_columns_identify_buckets_and_total(self, ar_summary):
        buckets, total_idx = _summary_columns(ar_summary)
        assert list(buckets.values()) == ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over"]
        assert total_idx == 6

    def test_parse_records_and_order(self, ar_summary):
        records, order = _parse_summary(ar_summary, {})
        assert order == ["Current", "1 - 30", "31 - 60", "61 - 90", "91 and over"]
        # grand-total Summary row is excluded; three payers × five buckets
        parties = {r[0] for r in records}
        assert parties == {"VSP", "EyeMed", "Patient Balances"}
        assert len(records) == 15

    def test_total_row_excluded(self, ar_summary):
        records, _ = _parse_summary(ar_summary, {})
        assert all(r[0].lower() != "total" for r in records)


# ---------------------------------------------------------------------------
# Detail column mapping
# ---------------------------------------------------------------------------

class TestDetailColumns:
    def test_maps_expected_columns(self, ar_detail):
        idx = _detail_col_indices(ar_detail)
        assert idx["date"] == 0
        assert idx["doc_num"] == 2
        assert idx["due_date"] == 3
        assert idx["amount"] == 4
        assert idx["open_balance"] == 5


# ---------------------------------------------------------------------------
# build_aging_dataframe — the integration of summary + detail
# ---------------------------------------------------------------------------

class TestBuildDataframe:
    def test_buckets_tie_to_grand_total(self, raw):
        bdf, _ = build_aging_dataframe(raw, {"name": "A/R Aging"}, as_of=AS_OF)
        assert round(bdf["amount"].sum(), 2) == 3000.00

    def test_bucket_order_in_attrs(self, raw):
        bdf, _ = build_aging_dataframe(raw, {"name": "A/R Aging"}, as_of=AS_OF)
        assert bdf.attrs["bucket_order"][0] == "Current"
        assert bdf.attrs["bucket_order"][-1] == "91 and over"

    def test_detail_open_balances_tie(self, raw):
        # The reconciliation guardrail relies on this: Σ(detail) == summary total.
        _, ddf = build_aging_dataframe(raw, {"name": "A/R Aging"}, as_of=AS_OF)
        assert round(ddf["open_balance"].sum(), 2) == 3000.00

    def test_party_from_section_header(self, raw):
        _, ddf = build_aging_dataframe(raw, {"name": "A/R Aging"}, as_of=AS_OF)
        assert set(ddf["party"]) == {"VSP", "EyeMed", "Patient Balances"}

    def test_days_overdue_computed(self, raw):
        _, ddf = build_aging_dataframe(raw, {"name": "A/R Aging"}, as_of=AS_OF)
        # Invoice 2002 due 2026-02-01, as-of 2026-06-18 → 137 days
        row = ddf[ddf["doc_num"] == "2002"].iloc[0]
        assert row["days_overdue"] == (AS_OF - date(2026, 2, 1)).days

    def test_aliases_merge_parties(self, raw):
        cfg = {"name": "A/R Aging", "aliases": {"vsp": "VSP Vision Plan"}}
        bdf, ddf = build_aging_dataframe(raw, cfg, as_of=AS_OF)
        assert "VSP Vision Plan" in set(bdf["party"])
        assert "VSP" not in set(bdf["party"])
        assert "VSP Vision Plan" in set(ddf["party"])
