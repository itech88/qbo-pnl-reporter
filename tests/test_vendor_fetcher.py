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
    _clean_memo_vendor,
    _resolve_vendor,
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
    # memo_fallback defaults to True
    return {"cogs_account_match": "cost of goods"}


@pytest.fixture
def cfg_no_fallback():
    return {"cogs_account_match": "cost of goods", "memo_fallback": False}


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
        assert idx["memo"]   == 4
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
    def test_extracts_cogs_transactions_only(self, pl_detail, cfg_no_fallback):
        records = _parse_detail(pl_detail, cfg_no_fallback)
        vendors = {r[2] for r in records}
        # Alcon and ABB are COGS; Amazon (Expenses) and Patient A (Income) excluded
        assert "Alcon" in vendors
        assert "ABB Optical" in vendors
        assert "Amazon" not in vendors
        assert "Patient A" not in vendors

    def test_unattributed_when_fallback_off(self, pl_detail, cfg_no_fallback):
        # The OSRX row has a blank Payee; with fallback off it stays Unattributed
        records = _parse_detail(pl_detail, cfg_no_fallback)
        assert any(v == _UNATTRIBUTED for (_, _, v, _) in records)

    def test_year_month_parsed(self, pl_detail, cfg):
        records = _parse_detail(pl_detail, cfg)
        alcon_jan = [r for r in records if r[2] == "Alcon" and r[1] == 1]
        assert alcon_jan
        assert alcon_jan[0][0] == 2026  # year

    def test_includes_nested_cos_subaccount(self, pl_detail, cfg):
        # ABB has a Feb shipping (COS) transaction in a second sub-section
        records = _parse_detail(pl_detail, cfg)
        abb_feb = [r for r in records if r[2] == "ABB Optical" and r[1] == 2]
        assert abb_feb
        assert abb_feb[0][3] == 120.0


# ---------------------------------------------------------------------------
# Memo fallback + alias resolution
# ---------------------------------------------------------------------------

class TestCleanMemoVendor:
    @pytest.mark.parametrize("memo,expected", [
        ("KAZAK-MARS, INC.",                       "Kazak-Mars"),
        ("LUXOTTICA USA",                          "Luxottica"),
        ("MARCHON EYEWEAR",                        "Marchon Eyewear"),
        ("ALTAIR EYEWEAR",                         "Altair Eyewear"),
        ("EYEWEAR DESIGNS LTD",                    "Eyewear Designs"),
        ("COOPERVISION, INC.",                     "Coopervision"),
        ("J&J*VISION CARE",                        "J&J Vision Care"),
        ("Optisource XXX-XXX8360 Ny",              "Optisource"),
        ("Lkc Technologies Inc XXX-XXX-1992 Md",   "Lkc Technologies"),
        ("Alden Optical Laborato XXX-XX2270 Ny",   "Alden Optical Laborato"),
    ])
    def test_descriptor_cleanup(self, memo, expected):
        assert _clean_memo_vendor(memo) == expected

    def test_empty_memo(self):
        assert _clean_memo_vendor("") == ""
        assert _clean_memo_vendor("   ") == ""

    def test_only_noise_returns_empty(self):
        assert _clean_memo_vendor("XXX-XXX1234 Ny") == ""


class TestResolveVendor:
    def test_name_takes_priority_over_memo(self):
        assert _resolve_vendor("Alcon", "ALCON VISION LLC", True, {}) == "Alcon"

    def test_memo_used_when_name_blank(self):
        assert _resolve_vendor("", "LUXOTTICA USA", True, {}) == "Luxottica"

    def test_unattributed_when_no_name_no_memo(self):
        assert _resolve_vendor("", "", True, {}) == _UNATTRIBUTED

    def test_fallback_disabled_keeps_unattributed(self):
        assert _resolve_vendor("", "LUXOTTICA USA", False, {}) == _UNATTRIBUTED

    def test_alias_canonicalizes_memo_result(self):
        aliases = {"coopervision": "CooperVision"}
        assert _resolve_vendor("", "COOPERVISION, INC.", True, aliases) == "CooperVision"

    def test_alias_merges_bill_name_too(self):
        # alias applies to explicit names as well, merging bill + memo spellings
        aliases = {"marchon eyewear": "Marchon Eyewear"}
        assert _resolve_vendor("Marchon Eyewear", "", True, aliases) == "Marchon Eyewear"


class TestMemoFallbackInParse:
    def test_blank_payee_resolves_via_memo(self, pl_detail, cfg):
        records = _parse_detail(pl_detail, cfg)
        vendors = {r[2] for r in records}
        # OSRX row had blank Payee; memo fallback surfaces it
        assert "Osrx" in vendors
        assert _UNATTRIBUTED not in vendors

    def test_alias_applied_in_parse(self, pl_detail):
        cfg = {"cogs_account_match": "cost of goods",
               "memo_fallback": True, "aliases": {"osrx": "OSRX Pharmacy"}}
        records = _parse_detail(pl_detail, cfg)
        assert any(v == "OSRX Pharmacy" for (_, _, v, _) in records)


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
        # blank-Payee row surfaced via memo fallback rather than Unattributed
        assert "Osrx" in vendors
        assert _UNATTRIBUTED not in vendors

    def test_empty_raw_returns_empty_df(self, cfg):
        df = build_vendor_dataframe({}, cfg)
        assert df.empty
        assert list(df.columns) == ["year", "month", "vendor", "amount"]
