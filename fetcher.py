"""
QuickBooks Online P&L data fetcher.

Pulls ProfitAndLoss reports for the current year and the 2 prior years.
Two requests per year (Jan–Jun, Jul–Dec) per Intuit best practice.
Returns a flat pandas DataFrame: year, month, income, cogs.
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

_SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
_PRODUCTION_BASE = "https://quickbooks.api.intuit.com"

# QBO group identifiers in the P&L JSON payload.
# If a sandbox account uses different labels, set QBO_INCOME_GROUP / QBO_COGS_GROUP in .env.
_INCOME_GROUP = os.getenv("QBO_INCOME_GROUP", "Income")
_COGS_GROUP = os.getenv("QBO_COGS_GROUP", "COGS")

# Fallback: match section by its header text when group field is absent.
_INCOME_HEADERS = {"income", "total income"}
_COGS_HEADERS = {"cost of goods sold", "total cost of goods sold", "cogs"}


# ---------------------------------------------------------------------------
# Internal helpers
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
    """Convert a QBO ColData value string to float; empty or missing → 0.0."""
    if not value or not value.strip():
        return 0.0
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return 0.0


def _parse_column_map(columns: list[dict]) -> dict[int, int]:
    """
    Return {col_index: month_number} for every money column whose title
    parses as "Mon YYYY" (e.g. "Jan 2026"). Skips index 0 (label column)
    and the trailing "Total" column.
    """
    col_map: dict[int, int] = {}
    for i, col in enumerate(columns):
        if col.get("ColType") != "Money":
            continue
        try:
            dt = datetime.strptime(col.get("ColTitle", ""), "%b %Y")
            col_map[i] = dt.month
        except ValueError:
            pass  # "Total" and other non-month columns
    return col_map


def _section_matches(row: dict, group: str, header_texts: set[str]) -> bool:
    """True if the row is a Section matching the expected group or header text."""
    if row.get("type") != "Section":
        return False
    if row.get("group", "").lower() == group.lower():
        return True
    # Fallback: check the first ColData value in the section Header.
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
    """
    Walk top-level rows looking for the matching Section.
    Returns {month_number: amount} from its Summary ColData.
    """
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


def _parse_response(data: dict) -> dict[int, dict[str, float]]:
    """
    Parse one P&L API response.
    Returns {month_number: {"income": float, "cogs": float}}.
    """
    columns = data.get("Columns", {}).get("Column", [])
    col_map = _parse_column_map(columns)

    rows = data.get("Rows", {}).get("Row", [])
    income = _extract_totals(rows, _INCOME_GROUP, _INCOME_HEADERS, col_map)
    cogs = _extract_totals(rows, _COGS_GROUP, _COGS_HEADERS, col_map)

    return {
        month: {"income": income.get(month, 0.0), "cogs": cogs.get(month, 0.0)}
        for month in col_map.values()
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_year(year: int) -> pd.DataFrame:
    """
    Fetch the full-year P&L for `year` using two half-year requests.
    Returns a DataFrame with columns: year, month, income, cogs.
    """
    session = get_session()
    halves = [
        (f"{year}-01-01", f"{year}-06-30"),
        (f"{year}-07-01", f"{year}-12-31"),
    ]

    records: dict[int, dict[str, float]] = {}
    for start, end in halves:
        params = {
            "start_date": start,
            "end_date": end,
            "summarize_column_by": "Month",
            "accounting_method": "Accrual",
        }
        log.info(
            "API request — report=ProfitAndLoss start=%s end=%s summarize_by=Month accounting=Accrual",
            start, end,
        )
        response = session.get(_report_url(), params=params, timeout=20)
        response.raise_for_status()
        parsed = _parse_response(response.json())
        log.debug("Parsed %d month(s) from %s → %s", len(parsed), start, end)
        records.update(parsed)

    return pd.DataFrame(
        [
            {"year": year, "month": m, "income": v["income"], "cogs": v["cogs"]}
            for m, v in sorted(records.items())
        ],
        columns=["year", "month", "income", "cogs"],
    )


def fetch_all() -> pd.DataFrame:
    """
    Fetch and return a DataFrame covering the current year and 2 prior years.
    Columns: year (int), month (int 1–12), income (float), cogs (float).
    """
    current_year = datetime.now().year
    frames = []
    for year in range(current_year - 2, current_year + 1):
        log.info("Fetching year %d…", year)
        frames.append(fetch_year(year))
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# CLI smoke-test — run `python fetcher.py` to print the raw DataFrame
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Pulling P&L data from QuickBooks Online…")
    df = fetch_all()
    if df.empty:
        print("No data returned — check QBO_REALM_ID and token validity.")
    else:
        df["cogs_pct"] = (df["cogs"] / df["income"].replace(0, float("nan")) * 100).round(1)
        print(df.to_string(index=False))
