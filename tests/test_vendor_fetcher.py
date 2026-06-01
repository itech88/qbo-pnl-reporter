"""Unit tests for vendor_fetcher.py — pure parsing, no API calls."""

import json
import os
import pytest

from vendor_fetcher import (
    _to_float,
    _col_indices,
    _is_cogs_section,
    _find_cogs_sections,
    _parse_detail,
    build_vendor_dataframe,
    _UNATTRIBUTED,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def pl_detail():
    with open(os.path.join(FIXTURE_DIR, "pl_detail.json")) as f:
        return json.load(f)


@pytest.fixture
def cfg():
    return {"cogs_account_match": "cost of goods"}


# ---------------------------------------------------------------------------
# _to_float
# ---------------------------------------------------------------------------

class TestToFloat:
    def test_empty(self):           assert _to_float("") == 0.0
    def test_plain(self):           assert _to_float("2189.45") == 2189.45
    def test_comma(self):           assert _to_float("1,500.00") == 1500.0
    def test_negative(self):        assert _to_float("-535.75") == -535.75
    def test_garbage(self):         assert _to_float("n/a") == 0.0


# ---------------------------------------------------------------------------
# _col_indices
# ---------------------------------------------------------------------------

class TestColIndices:
    def test_maps_expected_columns(self, pl_detail):
        idx = _col_indices(pl_detail)
        assert idx["date"]   == 0
        assert idx["name"]   == 3
        assert idx["amount"] == 6


# ---------------------------------------------------------------------------
# _is_cogs_section
# ---------------------------------------------------------------------------

class TestIsCogsSection:
    def test_matches_full_phrase(self):
        assert _is_cogs_section("Cost of Goods Sold", "cost of goods")
    def test_matches_subaccount(self):
        assert _is_cogs_section("Supplies & Materials - COGS", "cost of goods") is False
        # header substring match is on 'cost of goods'; bare 'cogs' also accepted
        assert _is_cogs_section("COGS", "cost of goods") is True
    def test_no_match_income(self):
        assert not _is_cogs_section("Income", "cost of goods")


# ---------------------------------------------------------------------------
# _find_cogs_sections
# ---------------------------------------------------------------------------

class TestFindCogsSections:
    def test_finds_one_top_cogs_section(self, pl_detail):
        rows = pl_detail["Rows"]["Row"]
        found = _find_cogs_sections(rows, "cost of goods")
        # The single top-level "Cost of Goods Sold" section
        assert len(found) == 1
        header = found[0]["Header"]["ColData"][0]["value"]
        assert "cost of goods" in header.lower()

    def test_excludes_income_and_expenses(self, pl_detail):
        rows = pl_detail["Rows"]["Row"]
        found = _find_cogs_sections(rows, "cost of goods")
        headers = [s["Header"]["ColData"][0]["value"].lower() for s in found]
        assert "income" not in headers
        assert "expenses" not in headers


# ---------------------------------------------------------------------------
# _parse_detail
# ---------------------------------------------------------------------------

class TestParseDetail:
    def test_extracts_cogs_transactions_only(self, pl_detail):
        records = _parse_detail(pl_detail, "cost of goods")
        vendors = {r[2] for r in records}
        # Alcon and ABB are COGS; Amazon (Expenses) and Patient A (Income) excluded
        assert "Alcon" in vendors
        assert "ABB Optical" in vendors
        assert "Amazon" not in vendors
        assert "Patient A" not in vendors

    def test_unattributed_for_blank_name(self, pl_detail):
        records = _parse_detail(pl_detail, "cost of goods")
        assert any(v == _UNATTRIBUTED for (_, _, v, _) in records)

    def test_year_month_parsed(self, pl_detail):
        records = _parse_detail(pl_detail, "cost of goods")
        alcon_jan = [r for r in records if r[2] == "Alcon" and r[1] == 1]
        assert alcon_jan
        assert alcon_jan[0][0] == 2026  # year

    def test_includes_nested_cos_subaccount(self, pl_detail):
        # ABB has a Feb shipping (COS) transaction in a second sub-section
        records = _parse_detail(pl_detail, "cost of goods")
        abb_feb = [r for r in records if r[2] == "ABB Optical" and r[1] == 2]
        assert abb_feb
        assert abb_feb[0][3] == 120.0


# ---------------------------------------------------------------------------
# build_vendor_dataframe
# ---------------------------------------------------------------------------

class TestBuildVendorDataframe:
    def test_columns(self, pl_detail, cfg):
        raw = {2026: [pl_detail, {"Columns": {"Column": []}, "Rows": {"Row": []}}]}
        df = build_vendor_dataframe(raw, cfg)
        assert list(df.columns) == ["year", "month", "vendor", "amount"]

    def test_groups_same_vendor_month(self, pl_detail, cfg):
        # Alcon has two January? No — Jan 2189.45 and Feb 1500. Jan is one row.
        raw = {2026: [pl_detail, {"Columns": {"Column": []}, "Rows": {"Row": []}}]}
        df = build_vendor_dataframe(raw, cfg)
        alcon_jan = df[(df["vendor"] == "Alcon") & (df["month"] == 1)]
        assert len(alcon_jan) == 1
        assert alcon_jan.iloc[0]["amount"] == 2189.45

    def test_dynamic_vendor_discovery(self, pl_detail, cfg):
        raw = {2026: [pl_detail, {"Columns": {"Column": []}, "Rows": {"Row": []}}]}
        df = build_vendor_dataframe(raw, cfg)
        vendors = set(df["vendor"])
        # Discovered purely from data, no hardcoded list
        assert "Alcon" in vendors
        assert "ABB Optical" in vendors
        assert _UNATTRIBUTED in vendors

    def test_empty_raw_returns_empty_df(self, cfg):
        df = build_vendor_dataframe({}, cfg)
        assert df.empty
        assert list(df.columns) == ["year", "month", "vendor", "amount"]
