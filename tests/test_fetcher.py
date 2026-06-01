"""Unit tests for fetcher.py — all pure parsing functions, no API calls."""

import json
import os
import pytest

from fetcher import (
    _to_float,
    _parse_column_map,
    _section_matches,
    _extract_totals,
    _find_line_item,
    _extract_value,
    build_dataframe,
    _INCOME_HEADERS,
    _COGS_HEADERS,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def pl_h1():
    with open(os.path.join(FIXTURE_DIR, "pl_h1.json")) as f:
        return json.load(f)


@pytest.fixture
def pl_h1_with_line_items():
    """Fixture extended with expense line items for line-item extraction tests."""
    with open(os.path.join(FIXTURE_DIR, "pl_h1.json")) as f:
        data = json.load(f)
    # Inject a realistic Expenses section with nested line items
    data["Rows"]["Row"].append({
        "type": "Section",
        "group": "Expenses",
        "Header": {"ColData": [{"value": "Expenses"}]},
        "Rows": {
            "Row": [
                {
                    "type": "Data",
                    "ColData": [
                        {"value": "Home Office Supplies"},
                        {"value": "36.75"}, {"value": "0.00"}, {"value": "45.00"},
                        {"value": "0.00"}, {"value": "22.50"}, {"value": "0.00"},
                        {"value": "104.25"},
                    ],
                },
                {
                    "type": "Section",
                    "group": "",
                    "Header": {"ColData": [{"value": "Payroll expenses"}]},
                    "Rows": {
                        "Row": [
                            {
                                "type": "Data",
                                "ColData": [
                                    {"value": "Payroll Expense Home Office Reimbursement"},
                                    {"value": "1834.42"}, {"value": "1891.96"}, {"value": "2698.66"},
                                    {"value": "1750.00"}, {"value": "1900.00"}, {"value": "1850.00"},
                                    {"value": "11924.04"},
                                ],
                            }
                        ]
                    },
                    "Summary": {"ColData": [{"value": "Total Payroll expenses"},
                                            {"value": "1834.42"}, {"value": "1891.96"}, {"value": "2698.66"},
                                            {"value": "1750.00"}, {"value": "1900.00"}, {"value": "1850.00"},
                                            {"value": "11924.04"}]},
                },
            ]
        },
        "Summary": {"ColData": [{"value": "Total Expenses"},
                                 {"value": "1871.17"}, {"value": "1891.96"}, {"value": "2743.66"},
                                 {"value": "1750.00"}, {"value": "1922.50"}, {"value": "1850.00"},
                                 {"value": "12028.29"}]},
    })
    return data


@pytest.fixture
def cogs_config():
    return {"extraction": {"type": "section_summary", "group": "COGS"}}


@pytest.fixture
def line_item_config():
    return {"extraction": {"type": "line_item", "account_name": "Home Office Supplies"}}


@pytest.fixture
def payroll_config():
    return {
        "extraction": {
            "type": "line_item",
            "account_name": "Payroll Expense Home Office Reimbursement",
        }
    }


# ---------------------------------------------------------------------------
# _to_float
# ---------------------------------------------------------------------------

class TestToFloat:
    def test_empty_string(self):       assert _to_float("") == 0.0
    def test_whitespace(self):         assert _to_float("   ") == 0.0
    def test_valid_amount(self):       assert _to_float("42000.00") == 42000.0
    def test_amount_with_comma(self):  assert _to_float("1,234.56") == 1234.56
    def test_zero(self):               assert _to_float("0.00") == 0.0
    def test_large_amount(self):       assert _to_float("268,500.00") == 268500.0
    def test_negative(self):           assert _to_float("-5000.00") == -5000.0


# ---------------------------------------------------------------------------
# _parse_column_map
# ---------------------------------------------------------------------------

class TestParseColumnMap:
    def test_standard_six_month_range(self, pl_h1):
        col_map = _parse_column_map(pl_h1["Columns"]["Column"])
        assert col_map == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}

    def test_skips_account_column(self, pl_h1):
        col_map = _parse_column_map(pl_h1["Columns"]["Column"])
        assert 0 not in col_map

    def test_skips_total_column(self, pl_h1):
        col_map = _parse_column_map(pl_h1["Columns"]["Column"])
        assert 7 not in col_map

    def test_empty_columns(self):
        assert _parse_column_map([]) == {}

    def test_only_account_and_total(self):
        columns = [
            {"ColTitle": "",      "ColType": "Account"},
            {"ColTitle": "Total", "ColType": "Money"},
        ]
        assert _parse_column_map(columns) == {}


# ---------------------------------------------------------------------------
# _section_matches
# ---------------------------------------------------------------------------

class TestSectionMatches:
    def test_matches_income_by_group(self):
        row = {"type": "Section", "group": "Income", "Header": {"ColData": [{"value": "Income"}]}}
        assert _section_matches(row, "Income", _INCOME_HEADERS)

    def test_matches_cogs_by_group(self):
        row = {"type": "Section", "group": "COGS", "Header": {"ColData": [{"value": "COGS"}]}}
        assert _section_matches(row, "COGS", _COGS_HEADERS)

    def test_matches_by_header_text_fallback(self):
        row = {"type": "Section", "group": "", "Header": {"ColData": [{"value": "Cost of Goods Sold"}]}}
        assert _section_matches(row, "COGS", _COGS_HEADERS)

    def test_no_match_wrong_group(self):
        row = {"type": "Section", "group": "COGS", "Header": {"ColData": [{"value": "COGS"}]}}
        assert not _section_matches(row, "Income", _INCOME_HEADERS)

    def test_no_match_non_section_type(self):
        row = {"type": "Data", "group": "Income", "Header": {"ColData": [{"value": "Income"}]}}
        assert not _section_matches(row, "Income", _INCOME_HEADERS)


# ---------------------------------------------------------------------------
# _extract_totals
# ---------------------------------------------------------------------------

class TestExtractTotals:
    def test_extracts_income_totals(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        rows    = pl_h1["Rows"]["Row"]
        col_map = _parse_column_map(columns)
        result  = _extract_totals(rows, "Income", _INCOME_HEADERS, col_map)
        assert result[1] == 42000.0
        assert result[6] == 48500.0

    def test_extracts_cogs_totals(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        rows    = pl_h1["Rows"]["Row"]
        col_map = _parse_column_map(columns)
        result  = _extract_totals(rows, "COGS", _COGS_HEADERS, col_map)
        assert result[1] == 12600.0
        assert result[6] == 14550.0

    def test_returns_empty_when_missing(self, pl_h1):
        rows    = pl_h1["Rows"]["Row"]
        col_map = {1: 1}
        assert _extract_totals(rows, "Payroll", {"payroll"}, col_map) == {}


# ---------------------------------------------------------------------------
# _find_line_item
# ---------------------------------------------------------------------------

class TestFindLineItem:
    def test_finds_direct_child(self, pl_h1_with_line_items):
        columns = pl_h1_with_line_items["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        rows    = pl_h1_with_line_items["Rows"]["Row"]
        result  = _find_line_item(rows, "Home Office Supplies", col_map)
        assert result[1] == 36.75
        assert result[3] == 45.00

    def test_finds_nested_item(self, pl_h1_with_line_items):
        columns = pl_h1_with_line_items["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        rows    = pl_h1_with_line_items["Rows"]["Row"]
        result  = _find_line_item(rows, "Payroll Expense Home Office Reimbursement", col_map)
        assert result[1] == 1834.42
        assert result[3] == 2698.66

    def test_case_insensitive_match(self, pl_h1_with_line_items):
        columns = pl_h1_with_line_items["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        rows    = pl_h1_with_line_items["Rows"]["Row"]
        result  = _find_line_item(rows, "home office supplies", col_map)
        assert result[1] == 36.75

    def test_returns_empty_when_not_found(self, pl_h1_with_line_items):
        columns = pl_h1_with_line_items["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        rows    = pl_h1_with_line_items["Rows"]["Row"]
        assert _find_line_item(rows, "Nonexistent Account", col_map) == {}


# ---------------------------------------------------------------------------
# _extract_value (dispatch)
# ---------------------------------------------------------------------------

class TestExtractValue:
    def test_section_summary_cogs(self, pl_h1, cogs_config):
        columns = pl_h1["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        result  = _extract_value(pl_h1, col_map, cogs_config)
        assert result[1] == 12600.0

    def test_line_item_home_office(self, pl_h1_with_line_items, line_item_config):
        columns = pl_h1_with_line_items["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        result  = _extract_value(pl_h1_with_line_items, col_map, line_item_config)
        assert result[1] == 36.75

    def test_unknown_extraction_type_raises(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        with pytest.raises(ValueError, match="Unknown extraction type"):
            _extract_value(pl_h1, col_map, {"extraction": {"type": "magic"}})


# ---------------------------------------------------------------------------
# build_dataframe
# ---------------------------------------------------------------------------

class TestBuildDataframe:
    def test_returns_36_rows_for_three_years(self, pl_h1, cogs_config):
        raw = {2024: [pl_h1, pl_h1], 2025: [pl_h1, pl_h1], 2026: [pl_h1, pl_h1]}
        df  = build_dataframe(raw, cogs_config)
        assert len(df) == 36  # 3 years × 12 months

    def test_columns_present(self, pl_h1, cogs_config):
        raw = {2026: [pl_h1, pl_h1]}
        df  = build_dataframe(raw, cogs_config)
        for col in ["year", "month", "income", "value"]:
            assert col in df.columns

    def test_income_values_correct(self, pl_h1, cogs_config):
        raw = {2026: [pl_h1, pl_h1]}
        df  = build_dataframe(raw, cogs_config)
        jan = df[(df["year"] == 2026) & (df["month"] == 1)].iloc[0]
        assert jan["income"] == 42000.0

    def test_value_is_cogs_for_cogs_config(self, pl_h1, cogs_config):
        raw = {2026: [pl_h1, pl_h1]}
        df  = build_dataframe(raw, cogs_config)
        jan = df[(df["year"] == 2026) & (df["month"] == 1)].iloc[0]
        assert jan["value"] == 12600.0
