"""
QuickBooks Online P&L data fetcher.

Pulls ProfitAndLoss reports for the current year and the 2 prior years.
Two requests per year (Jan–Jun, Jul–Dec) per Intuit best practice.

Public API:
    fetch_raw_all()                     -> {year: [h1_json, h2_json]}
    build_dataframe(raw_by_year, config) -> DataFrame: year, month, income, value
    fetch_all(config)                   -> convenience wrapper
"""

import os
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

from auth import get_session
from logger import get_logger

load_dotenv()

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SANDBOX_BASE    = "https://sandbox-quickbooks.api.intuit.com"
_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"

_INCOME_GROUP   = os.getenv("QBO_INCOME_GROUP", "Income")
_COGS_GROUP     = os.getenv("QBO_COGS_GROUP",   "COGS")
_INCOME_HEADERS = {"income", "total income"}
_COGS_HEADERS   = {"cost of goods sold", "total cost of goods sold", "cogs"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_url() -> str:
    env = os.getenv("QBO_ENVIRONMENT", "sandbox").lower()
    return _SANDBOX_BASE if env == "sandbox" else _PRODUCTION_BASE


def _realm_id() -> str:
    rid = os.getenv("QBO_REALM_ID", "")
    if not rid or rid == "your_realm_id_here":
        raise RuntimeError("QBO_REALM_ID is not set in .env")
    return rid


def _report_url() -> str:
    return f"{_base_url()}/v3/company/{_realm_id()}/reports/ProfitAndLoss"


def _to_float(value: str) -> float:
    if not value or not value.strip():
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def _parse_column_map(columns: list[dict]) -> dict[int, int]:
    """Return {col_index: month_number} for money columns with parseable month titles."""
    col_map: dict[int, int] = {}
    for i, col in enumerate(columns):
        if col.get("ColType") != "Money":
            continue
        try:
            dt = datetime.strptime(col.get("ColTitle", ""), "%b %Y")
            col_map[i] = dt.month
        except ValueError:
            pass
    return col_map


def _section_matches(row: dict, group: str, header_texts: set[str]) -> bool:
    if row.get("type") != "Section":
        return False
    if row.get("group", "").lower() == group.lower():
        return True
    header_val = (
        row.get("Header", {})
        .get("ColData", [{}])[0]
        .get("value", "")
        .strip()
        .lower()
    )
    return header_val in header_texts


def _extract_totals(
    rows: list[dict],
    group: str,
    header_texts: set[str],
    col_map: dict[int, int],
) -> dict[int, float]:
    """Read a Section's Summary ColData values (section-level totals)."""
    for row in rows:
        if not _section_matches(row, group, header_texts):
            continue
        col_data = row.get("Summary", {}).get("ColData", [])
        return {
            month: _to_float(col_data[idx].get("value", ""))
            for idx, month in col_map.items()
            if idx < len(col_data)
        }
    return {}


def _extract_section_by_group(
    rows: list[dict],
    group: str,
    col_map: dict[int, int],
) -> dict[int, float]:
    """Read a Section Summary matched by group field alone (computed sections)."""
    for row in rows:
        if row.get("type") == "Section" and row.get("group", "") == group:
            col_data = row.get("Summary", {}).get("ColData", [])
            result = {
                month: _to_float(col_data[idx].get("value", ""))
                for idx, month in col_map.items()
                if idx < len(col_data)
            }
            if result:
                return result
    return {}


def _find_line_item(
    rows: list[dict],
    account_name: str,
    col_map: dict[int, int],
) -> dict[int, float]:
    """Recursively search Data rows for an account matching account_name."""
    target = account_name.lower().strip()
    for row in rows:
        if row.get("type") == "Data":
            name = row.get("ColData", [{}])[0].get("value", "").strip().lower()
            if name == target:
                col_data = row.get("ColData", [])
                result = {
                    month: _to_float(col_data[idx].get("value", ""))
                    for idx, month in col_map.items()
                    if idx < len(col_data)
                }
                if not result:
                    log.warning("Line item '%s' found but no monthly values extracted.", account_name)
                return result
        elif row.get("type") == "Section":
            inner = row.get("Rows", {}).get("Row", [])
            result = _find_line_item(inner, account_name, col_map)
            if result:
                return result
    return {}


def _extract_income(data: dict, col_map: dict[int, int]) -> dict[int, float]:
    rows = data.get("Rows", {}).get("Row", [])
    return _extract_totals(rows, _INCOME_GROUP, _INCOME_HEADERS, col_map)


def _extract_value(
    data: dict,
    col_map: dict[int, int],
    report_config: dict,
) -> dict[int, float]:
    """Dispatch to the correct extractor based on report_config.extraction.type."""
    extraction = report_config["extraction"]
    rows = data.get("Rows", {}).get("Row", [])
    etype = extraction["type"]

    if etype == "section_summary":
        group = extraction["group"]
        if group == _COGS_GROUP:
            return _extract_totals(rows, _COGS_GROUP, _COGS_HEADERS, col_map)
        elif group == _INCOME_GROUP:
            return _extract_totals(rows, _INCOME_GROUP, _INCOME_HEADERS, col_map)
        else:
            result = _extract_section_by_group(rows, group, col_map)
            if not result:
                log.warning("Section group '%s' not found in P&L response.", group)
            return result

    elif etype == "line_item":
        account_name = extraction["account_name"]
        result = _find_line_item(rows, account_name, col_map)
        if not result:
            log.warning(
                "Line item '%s' not found in P&L response — "
                "check the account name matches the QBO chart of accounts exactly.",
                account_name,
            )
        return result

    raise ValueError(f"Unknown extraction type: {etype!r}")


# ---------------------------------------------------------------------------
# Raw data fetch (fetch once, reuse across all report configs)
# ---------------------------------------------------------------------------

def fetch_raw_all() -> dict[int, list[dict]]:
    """
    Fetch raw P&L JSON for the current year and 2 prior years.
    Returns {year: [h1_response_json, h2_response_json]}.
    Makes 6 API calls total regardless of how many report configs are run.
    """
    session = get_session()
    current_year = datetime.now().year
    raw: dict[int, list[dict]] = {}

    for year in range(current_year - 2, current_year + 1):
        log.info("Fetching year %d…", year)
        halves = [
            (f"{year}-01-01", f"{year}-06-30"),
            (f"{year}-07-01", f"{year}-12-31"),
        ]
        year_responses = []
        for start, end in halves:
            params = {
                "start_date": start,
                "end_date": end,
                "summarize_column_by": "Month",
                "accounting_method": "Accrual",
            }
            log.info(
                "API request — report=ProfitAndLoss start=%s end=%s "
                "summarize_by=Month accounting=Accrual", start, end,
            )
            response = session.get(_report_url(), params=params, timeout=20)
            response.raise_for_status()
            year_responses.append(response.json())
        raw[year] = year_responses

    return raw


# ---------------------------------------------------------------------------
# DataFrame builder
# ---------------------------------------------------------------------------

def build_dataframe(
    raw_by_year: dict[int, list[dict]],
    report_config: dict,
) -> pd.DataFrame:
    """
    Extract income and target value from cached raw data for a given report config.
    Returns a DataFrame with columns: year, month, income, value.
    """
    rows = []
    for year, responses in sorted(raw_by_year.items()):
        monthly_income: dict[int, float] = {}
        monthly_value: dict[int, float] = {}

        for data in responses:
            columns = data.get("Columns", {}).get("Column", [])
            col_map = _parse_column_map(columns)
            monthly_income.update(_extract_income(data, col_map))
            monthly_value.update(_extract_value(data, col_map, report_config))

        for month in range(1, 13):
            rows.append({
                "year":   year,
                "month":  month,
                "income": monthly_income.get(month, 0.0),
                "value":  monthly_value.get(month, 0.0),
            })

    df = pd.DataFrame(rows, columns=["year", "month", "income", "value"])
    log.debug(
        "Built DataFrame for '%s': %d rows", report_config.get("name", "?"), len(df)
    )
    return df


# ---------------------------------------------------------------------------
# Convenience wrapper
# ---------------------------------------------------------------------------

def fetch_all(report_config: dict) -> pd.DataFrame:
    """Fetch raw data and build a DataFrame in one call."""
    return build_dataframe(fetch_raw_all(), report_config)


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml, glob, os as _os
    configs_dir = _os.path.join(_os.path.dirname(__file__), "reports")
    configs = []
    for path in sorted(glob.glob(_os.path.join(configs_dir, "*.yaml"))):
        with open(path) as f:
            configs.append(yaml.safe_load(f))

    print("Fetching raw P&L data (6 API calls)…")
    raw = fetch_raw_all()

    for cfg in configs:
        df = build_dataframe(raw, cfg)
        active = df[df["income"] > 0]
        print(f"\n── {cfg['name']} ──")
        print(active[["year", "month", "income", "value"]].to_string(index=False))
