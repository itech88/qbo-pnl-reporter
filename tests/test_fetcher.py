"""Unit tests for fetcher.py — all pure parsing functions, no API calls."""

import json
import os
import pytest

from fetcher import (
    _to_float,
    _parse_column_map,
    _section_matches,
    _extract_totals,
    _parse_response,
)

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def pl_h1():
    with open(os.path.join(FIXTURE_DIR, "pl_h1.json")) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# _to_float
# ---------------------------------------------------------------------------

class TestToFloat:
    def test_empty_string(self):
        assert _to_float("") == 0.0

    def test_whitespace(self):
        assert _to_float("   ") == 0.0

    def test_valid_amount(self):
        assert _to_float("42000.00") == 42000.0

    def test_amount_with_comma(self):
        assert _to_float("1,234.56") == 1234.56

    def test_zero(self):
        assert _to_float("0.00") == 0.0

    def test_large_amount(self):
        assert _to_float("268,500.00") == 268500.0


# ---------------------------------------------------------------------------
# _parse_column_map
# ---------------------------------------------------------------------------

class TestParseColumnMap:
    def test_standard_six_month_range(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        assert col_map == {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}

    def test_skips_account_column(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        assert 0 not in col_map

    def test_skips_total_column(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        col_map = _parse_column_map(columns)
        # Total column is index 7 — must not appear
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
    def _income_section(self):
        return {
            "type": "Section",
            "group": "Income",
            "Header": {"ColData": [{"value": "Income"}]},
        }

    def _cogs_section(self):
        return {
            "type": "Section",
            "group": "COGS",
            "Header": {"ColData": [{"value": "Cost of Goods Sold"}]},
        }

    def test_matches_income_by_group(self):
        from fetcher import _INCOME_HEADERS
        assert _section_matches(self._income_section(), "Income", _INCOME_HEADERS)

    def test_matches_cogs_by_group(self):
        from fetcher import _COGS_HEADERS
        assert _section_matches(self._cogs_section(), "COGS", _COGS_HEADERS)

    def test_matches_cogs_by_header_text_fallback(self):
        row = {"type": "Section", "group": "", "Header": {"ColData": [{"value": "Cost of Goods Sold"}]}}
        from fetcher import _COGS_HEADERS
        assert _section_matches(row, "COGS", _COGS_HEADERS)

    def test_no_match_wrong_group(self):
        from fetcher import _INCOME_HEADERS
        assert not _section_matches(self._cogs_section(), "Income", _INCOME_HEADERS)

    def test_no_match_non_section_type(self):
        row = {"type": "Data", "group": "Income", "Header": {"ColData": [{"value": "Income"}]}}
        from fetcher import _INCOME_HEADERS
        assert not _section_matches(row, "Income", _INCOME_HEADERS)

    def test_no_match_wrong_header_text(self):
        row = {"type": "Section", "group": "", "Header": {"ColData": [{"value": "Expenses"}]}}
        from fetcher import _INCOME_HEADERS
        assert not _section_matches(row, "Income", _INCOME_HEADERS)


# ---------------------------------------------------------------------------
# _extract_totals
# ---------------------------------------------------------------------------

class TestExtractTotals:
    def test_extracts_income_totals(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        rows = pl_h1["Rows"]["Row"]
        col_map = _parse_column_map(columns)
        from fetcher import _INCOME_HEADERS
        result = _extract_totals(rows, "Income", _INCOME_HEADERS, col_map)
        assert result[1] == 42000.0
        assert result[6] == 48500.0

    def test_extracts_cogs_totals(self, pl_h1):
        columns = pl_h1["Columns"]["Column"]
        rows = pl_h1["Rows"]["Row"]
        col_map = _parse_column_map(columns)
        from fetcher import _COGS_HEADERS
        result = _extract_totals(rows, "COGS", _COGS_HEADERS, col_map)
        assert result[1] == 12600.0
        assert result[6] == 14550.0

    def test_returns_empty_when_section_missing(self, pl_h1):
        rows = pl_h1["Rows"]["Row"]
        col_map = {1: 1, 2: 2}
        result = _extract_totals(rows, "Payroll", {"payroll"}, col_map)
        assert result == {}


# ---------------------------------------------------------------------------
# _parse_response (integration of the above)
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_full_parse_returns_six_months(self, pl_h1):
        result = _parse_response(pl_h1)
        assert set(result.keys()) == {1, 2, 3, 4, 5, 6}

    def test_income_values_correct(self, pl_h1):
        result = _parse_response(pl_h1)
        assert result[1]["income"] == 42000.0
        assert result[3]["income"] == 41000.0

    def test_cogs_values_correct(self, pl_h1):
        result = _parse_response(pl_h1)
        assert result[1]["cogs"] == 12600.0
        assert result[6]["cogs"] == 14550.0

    def test_cogs_pct_jan_is_30_percent(self, pl_h1):
        result = _parse_response(pl_h1)
        pct = result[1]["cogs"] / result[1]["income"]
        assert round(pct, 4) == round(12600 / 42000, 4)

    def test_empty_response_returns_empty_dict(self):
        empty = {"Columns": {"Column": []}, "Rows": {"Row": []}}
        assert _parse_response(empty) == {}

    def test_missing_cogs_section_returns_zero(self):
        data = {
            "Columns": {"Column": [
                {"ColTitle": "", "ColType": "Account"},
                {"ColTitle": "Jan 2026", "ColType": "Money"},
                {"ColTitle": "Total", "ColType": "Money"},
            ]},
            "Rows": {"Row": [{
                "type": "Section",
                "group": "Income",
                "Header": {"ColData": [{"value": "Income"}]},
                "Summary": {"ColData": [{"value": "Total Income"}, {"value": "50000.00"}, {"value": "50000.00"}]},
            }]}
        }
        result = _parse_response(data)
        assert result[1]["income"] == 50000.0
        assert result[1]["cogs"] == 0.0
