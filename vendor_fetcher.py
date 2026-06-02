"""
Vendor-level COGS fetcher.

Uses the ProfitAndLossDetail report (not the summary P&L) because only the
detail report breaks each transaction out by vendor under its expense account.

Dynamic by design: we collect every transaction that posts to a Cost of Goods
Sold account and group by vendor name. Whatever vendors exist in production at
run time appear automatically — no hardcoded vendor list. New suppliers show up;
discontinued ones drop off.

Public API:
    fetch_vendor_raw_all()                  -> {year: [h1_json, h2_json]}
    build_vendor_dataframe(raw, cfg)        -> DataFrame: year, month, vendor, amount
"""

import os
import re
from datetime import datetime

import pandas as pd
from dotenv import load_dotenv

from auth import get_session
from fetcher import _base_url, _realm_id
from logger import get_logger

load_dotenv()
log = get_logger(__name__)

# Substring (case-insensitive) that identifies a Cost of Goods Sold section
# header in the ProfitAndLossDetail response. Overridable per report config.
_DEFAULT_COGS_MATCH = "cost of goods"

# Label for transactions posted to COGS with neither a vendor nor a usable memo.
_UNATTRIBUTED = "Unattributed"

# US state abbreviations and corporate suffixes stripped from bank-feed memos
# when deriving a vendor name (memo fallback).
_STATE_ABBR = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_CORP_SUFFIXES = {
    "INC", "LLC", "LTD", "CO", "CORP", "COMPANY", "USA", "US", "LP", "LLP",
}


def _smart_title(text: str) -> str:
    """Title-case a label while preserving short all-caps acronyms (J&J, LKC)."""
    def cap(word: str) -> str:
        core = word.strip(".")
        if len(core) <= 3 and core.isupper() and any(c.isalpha() for c in core):
            return word                      # keep acronyms: J&J, LKC
        return "-".join(p.capitalize() for p in word.split("-"))
    return " ".join(cap(w) for w in text.split())


def _clean_memo_vendor(memo: str) -> str:
    """
    Derive a vendor name from a bank-feed memo/descriptor.

    Credit-card COGS charges import with the merchant in the memo but no Payee
    assigned. This normalizes descriptors like
        'KAZAK-MARS, INC.'                  -> 'Kazak-Mars'
        'LUXOTTICA USA'                     -> 'Luxottica'
        'Alden Optical Laborato XXX-XX2270 Ny' -> 'Alden Optical Laborato'
    Returns '' if nothing usable remains.
    """
    if not memo or not memo.strip():
        return ""

    tokens = memo.replace("*", " ").replace(",", " ").split()
    cleaned: list[str] = []
    for tok in tokens:
        if "XX" in tok.upper():           # card-masking fragments (XXX-XXX2270)
            continue
        if re.fullmatch(r"[#\d\-]+", tok):  # bare transaction/id numbers
            continue
        cleaned.append(tok)

    # Strip a trailing state abbreviation, then any corporate suffix tokens.
    while cleaned and cleaned[-1].upper().strip(".") in _STATE_ABBR:
        cleaned.pop()
    cleaned = [t for t in cleaned if t.upper().strip(".") not in _CORP_SUFFIXES]

    return _smart_title(" ".join(cleaned)) if cleaned else ""


def _report_url() -> str:
    return f"{_base_url()}/v3/company/{_realm_id()}/reports/ProfitAndLossDetail"


def _to_float(value: str) -> float:
    if not value or not str(value).strip():
        return 0.0
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0.0


def _col_indices(data: dict) -> dict[str, int]:
    """Map the columns we care about (by ColType) to their index."""
    idx: dict[str, int] = {}
    for i, col in enumerate(data.get("Columns", {}).get("Column", [])):
        ctype = col.get("ColType", "")
        if ctype == "tx_date":
            idx["date"] = i
        elif ctype == "name":
            idx["name"] = i
        elif ctype == "memo":
            idx["memo"] = i
        elif ctype == "subt_nat_amount":
            idx["amount"] = i
    return idx


def _is_cogs_section(header_text: str, cogs_match: str) -> bool:
    h = header_text.lower()
    return cogs_match in h or h.strip() == "cogs"


def _collect_data_rows(section: dict) -> list[list[str]]:
    """Recursively collect all Data-row ColData value lists under a section."""
    out: list[list[str]] = []
    for row in section.get("Rows", {}).get("Row", []):
        if row.get("type") == "Data":
            out.append([c.get("value", "") for c in row.get("ColData", [])])
        elif row.get("type") == "Section":
            out.extend(_collect_data_rows(row))
    return out


def _find_cogs_sections(rows: list[dict], cogs_match: str) -> list[dict]:
    """Recursively find every section whose header marks it as COGS."""
    found: list[dict] = []
    for row in rows:
        if row.get("type") != "Section":
            continue
        header = row.get("Header", {}).get("ColData", [{}])[0].get("value", "")
        if _is_cogs_section(header, cogs_match):
            found.append(row)
        else:
            # Only descend into non-COGS sections; a COGS section's own
            # children are collected wholesale by _collect_data_rows.
            found.extend(_find_cogs_sections(row.get("Rows", {}).get("Row", []), cogs_match))
    return found


def _resolve_vendor(name: str, memo: str, memo_fallback: bool,
                    aliases: dict[str, str]) -> str:
    """
    Determine the vendor label for a COGS transaction.
    Priority: explicit Payee/Vendor name → memo-derived name (if enabled) →
    Unattributed. An alias map (lowercased keys) canonicalizes the result so
    bill-based and memo-based spellings of the same vendor merge.
    """
    vendor = name.strip()
    if not vendor and memo_fallback:
        vendor = _clean_memo_vendor(memo)
    vendor = vendor or _UNATTRIBUTED
    return aliases.get(vendor.lower(), vendor)


def _parse_detail(data: dict, report_config: dict) -> list[tuple[int, int, str, float]]:
    """
    Parse one ProfitAndLossDetail response.
    Returns a list of (year, month, vendor, amount) tuples for COGS transactions.

    When a transaction has no Payee/Vendor (common for credit-card-fed charges),
    the vendor is derived from the memo descriptor if `memo_fallback` is enabled
    (default True). An optional `aliases` map canonicalizes vendor names.
    """
    cogs_match    = report_config.get("cogs_account_match", _DEFAULT_COGS_MATCH).lower()
    memo_fallback = report_config.get("memo_fallback", True)
    aliases       = {k.lower(): v for k, v in (report_config.get("aliases") or {}).items()}

    idx = _col_indices(data)
    if "date" not in idx or "amount" not in idx:
        log.warning("ProfitAndLossDetail missing expected columns — got %s", idx)
        return []

    name_idx = idx.get("name")
    memo_idx = idx.get("memo")
    rows = data.get("Rows", {}).get("Row", [])
    sections = _find_cogs_sections(rows, cogs_match)

    records: list[tuple[int, int, str, float]] = []
    for sec in sections:
        for cd in _collect_data_rows(sec):
            if len(cd) <= idx["amount"] or len(cd) <= idx["date"]:
                continue
            try:
                dt = datetime.strptime(cd[idx["date"]], "%Y-%m-%d")
            except (ValueError, TypeError):
                continue
            name = cd[name_idx] if name_idx is not None and name_idx < len(cd) else ""
            memo = cd[memo_idx] if memo_idx is not None and memo_idx < len(cd) else ""
            vendor = _resolve_vendor(name, memo, memo_fallback, aliases)
            records.append((dt.year, dt.month, vendor, _to_float(cd[idx["amount"]])))
    return records


# ---------------------------------------------------------------------------
# Raw fetch
# ---------------------------------------------------------------------------

def fetch_vendor_raw_all() -> dict[int, list[dict]]:
    """
    Fetch ProfitAndLossDetail for the current year and 2 prior years.
    Two half-year calls per year. Returns {year: [h1_json, h2_json]}.
    """
    session = get_session()
    current_year = datetime.now().year
    raw: dict[int, list[dict]] = {}

    for year in range(current_year - 2, current_year + 1):
        log.info("Fetching vendor detail for year %d…", year)
        halves = [
            (f"{year}-01-01", f"{year}-06-30"),
            (f"{year}-07-01", f"{year}-12-31"),
        ]
        year_responses = []
        for start, end in halves:
            log.info("API request — report=ProfitAndLossDetail start=%s end=%s", start, end)
            r = session.get(
                _report_url(),
                params={"start_date": start, "end_date": end},
                timeout=40,
            )
            r.raise_for_status()
            year_responses.append(r.json())
        raw[year] = year_responses

    return raw


# ---------------------------------------------------------------------------
# DataFrame builder
# ---------------------------------------------------------------------------

def build_vendor_dataframe(
    raw_by_year: dict[int, list[dict]],
    report_config: dict,
) -> pd.DataFrame:
    """
    Build a long-form DataFrame of monthly COGS spend per vendor.
    Columns: year, month, vendor, amount

    Dynamically includes every vendor that posted to a COGS account in any
    period — the vendor set is rebuilt from the data on every run.
    """
    records: list[tuple[int, int, str, float]] = []
    for responses in raw_by_year.values():
        for data in responses:
            records.extend(_parse_detail(data, report_config))

    if not records:
        return pd.DataFrame(columns=["year", "month", "vendor", "amount"])

    df = pd.DataFrame(records, columns=["year", "month", "vendor", "amount"])
    # Collapse multiple transactions for the same vendor/month into one total.
    df = (
        df.groupby(["year", "month", "vendor"], as_index=False)["amount"]
        .sum()
        .sort_values(["year", "month", "amount"], ascending=[True, True, False])
        .reset_index(drop=True)
    )
    log.debug("Vendor DataFrame: %d rows, %d unique vendors",
              len(df), df["vendor"].nunique())
    return df


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cfg = {"cogs_account_match": "cost of goods"}
    print("Fetching vendor COGS detail…")
    raw = fetch_vendor_raw_all()
    df = build_vendor_dataframe(raw, cfg)
    if df.empty:
        print("No vendor COGS data returned.")
    else:
        latest_year = df["year"].max()
        latest = df[df["year"] == latest_year]
        latest_month = latest["month"].max()
        snap = latest[latest["month"] == latest_month].copy()
        total = snap["amount"].sum()
        snap["share"] = (snap["amount"] / total * 100).round(1)
        print(f"\nMost recent month: {latest_year}-{latest_month:02d}  "
              f"(total COGS ${total:,.2f})")
        print(snap[["vendor", "amount", "share"]].to_string(index=False))
