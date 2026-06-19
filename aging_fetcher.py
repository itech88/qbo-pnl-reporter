"""
Accounts-Receivable / Accounts-Payable aging fetcher.

Pulls QBO's aging reports "as of today" — a point-in-time *snapshot*, unlike the
flow-based P&L fetchers (no half-year loop, no multi-year history). The `side`
("receivable" | "payable") selects the endpoint pair; everything else is shared:

  summary  → AgedReceivables / AgedPayables       — buckets per party + grand total
  detail   → AgedReceivableDetail / AgedPayableDetail — open documents per party

The summary is QBO's *authoritative* bucketed total; the detail is the open-item
worklist. The scheduler reconciles Σ(detail open balances) against the summary grand
total before sending (guardrails.reconcile_aging), so a parse drift is *held*, never
emailed. "Party" = the payer (A/R) or creditor (A/P); names are canonicalized through
an optional `aliases` map, exactly like the COGS-by-Vendor report.

Public API:
    fetch_aging_raw(side)              -> {"summary": json, "detail": json}
    build_aging_dataframe(raw, cfg)    -> (buckets_df, detail_df)

NOTE: QBO column ColType strings on the *detail* reports vary by company/config; the
parsers below map them tolerantly (ColType aliases + ColTitle fallback). Confirm the
real shape with a production dry-run (see docs/regression-testing.md) before first send
— the reconciliation guardrail is the backstop until then.
"""

import os
import re
from datetime import datetime, date

import pandas as pd
from dotenv import load_dotenv

from auth import get_session
from fetcher import _base_url, _realm_id
from logger import get_logger

load_dotenv()
log = get_logger(__name__)

# side -> (summary report name, detail report name)
_ENDPOINTS = {
    "receivable": ("AgedReceivables", "AgedReceivableDetail"),
    "payable":    ("AgedPayables",    "AgedPayableDetail"),
}

# Label for documents with no resolvable customer/vendor.
_UNASSIGNED = "Unassigned"

# Row labels in the summary that are grand-total lines, not parties.
_TOTAL_LABELS = {"total", "totals", "grand total"}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _to_float(value) -> float:
    if value is None or not str(value).strip():
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def _parse_date(value: str) -> date | None:
    if not value or not str(value).strip():
        return None
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _report_url(report_name: str) -> str:
    return f"{_base_url()}/v3/company/{_realm_id()}/reports/{report_name}"


def _resolve_party(name: str, aliases: dict[str, str]) -> str:
    party = (name or "").strip()
    if not party or party.lower() in _TOTAL_LABELS:
        party = _UNASSIGNED if not party else party
    return aliases.get(party.lower(), party)


def _cell(coldata: list[dict], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(coldata):
        return ""
    return coldata[idx].get("value", "") if isinstance(coldata[idx], dict) else ""


# ---------------------------------------------------------------------------
# Summary report (AgedReceivables / AgedPayables) — buckets per party
# ---------------------------------------------------------------------------

def _summary_columns(data: dict) -> tuple[dict[int, str], int | None]:
    """Return ({money_col_index: bucket_label}, total_col_index).

    Bucket columns are the Money columns whose title is not 'Total'; the Total
    column (if present) is returned separately. Bucket order follows column order.
    """
    bucket_cols: dict[int, str] = {}
    total_idx: int | None = None
    for i, col in enumerate(data.get("Columns", {}).get("Column", [])):
        if col.get("ColType") != "Money":
            continue
        title = (col.get("ColTitle") or "").strip()
        if title.lower() == "total":
            total_idx = i
        else:
            bucket_cols[i] = title or f"Bucket {i}"
    return bucket_cols, total_idx


def _collect_summary_rows(rows: list[dict]) -> list[list[dict]]:
    """Recursively collect every Data row's ColData (the per-party rows)."""
    out: list[list[dict]] = []
    for row in rows:
        if row.get("type") == "Data":
            out.append(row.get("ColData", []))
        elif row.get("type") == "Section":
            out.extend(_collect_summary_rows(row.get("Rows", {}).get("Row", [])))
    return out


def _parse_summary(
    data: dict, aliases: dict[str, str]
) -> tuple[list[tuple[str, str, float]], list[str]]:
    """Parse the summary report into (party, bucket, amount) records + bucket order."""
    bucket_cols, _total_idx = _summary_columns(data)
    bucket_order = [bucket_cols[i] for i in sorted(bucket_cols)]
    if not bucket_cols:
        log.warning("Aging summary had no Money/bucket columns — got %s",
                    [c.get("ColTitle") for c in data.get("Columns", {}).get("Column", [])])

    records: list[tuple[str, str, float]] = []
    for cd in _collect_summary_rows(data.get("Rows", {}).get("Row", [])):
        party_raw = _cell(cd, 0).strip()
        if party_raw.lower() in _TOTAL_LABELS:
            continue  # grand-total line, not a party
        party = _resolve_party(party_raw, aliases)
        for idx in sorted(bucket_cols):
            records.append((party, bucket_cols[idx], _to_float(_cell(cd, idx))))
    return records, bucket_order


# ---------------------------------------------------------------------------
# Detail report (AgedReceivableDetail / AgedPayableDetail) — open documents
# ---------------------------------------------------------------------------

def _detail_col_indices(data: dict) -> dict[str, int]:
    """Map detail columns to logical fields by ColType, with ColTitle fallback."""
    idx: dict[str, int] = {}
    for i, col in enumerate(data.get("Columns", {}).get("Column", [])):
        ctype = (col.get("ColType") or "").lower()
        title = (col.get("ColTitle") or "").strip().lower()
        if ctype == "tx_date" or title == "date":
            idx["date"] = i
        elif ctype == "doc_num" or title in ("num", "no.", "number"):
            idx["doc_num"] = i
        elif ctype in ("name", "cust_name", "vend_name", "customer", "vendor") \
                or title in ("customer", "vendor", "name"):
            idx["name"] = i
        elif ctype == "due_date" or title == "due date":
            idx["due_date"] = i
        elif "open_bal" in ctype or title in ("open balance", "open_balance"):
            idx["open_balance"] = i
        elif ctype in ("subt_nat_amount", "amount") or title == "amount":
            idx["amount"] = i
    return idx


def _collect_detail_rows(rows: list[dict], party: str | None) -> list[tuple[str | None, list[dict]]]:
    """Collect (section_party, ColData) for every Data row.

    Detail reports group open documents under a Section per customer/vendor, so the
    *innermost* Section header is the party. A name column on the row itself, when
    present and non-empty, takes precedence (handled by the caller).
    """
    out: list[tuple[str | None, list[dict]]] = []
    for row in rows:
        if row.get("type") == "Data":
            out.append((party, row.get("ColData", [])))
        elif row.get("type") == "Section":
            header = row.get("Header", {}).get("ColData", [{}])[0].get("value", "").strip()
            out.extend(_collect_detail_rows(row.get("Rows", {}).get("Row", []), header or party))
    return out


def _parse_detail(
    data: dict, aliases: dict[str, str], as_of: date
) -> list[tuple[str, str, date | None, date | None, float, int | None]]:
    """Parse detail into (party, doc_num, txn_date, due_date, open_balance, days_overdue)."""
    idx = _detail_col_indices(data)
    if "date" not in idx and "due_date" not in idx:
        log.warning("Aging detail missing date columns — got %s", idx)

    records = []
    for section_party, cd in _collect_detail_rows(data.get("Rows", {}).get("Row", []), None):
        name = _cell(cd, idx.get("name")).strip()
        # Section-grouped reports leave the name column blank on rows; fall back to
        # the enclosing section header.
        party = _resolve_party(name or section_party or "", aliases)

        open_bal = _to_float(_cell(cd, idx.get("open_balance")
                                   if "open_balance" in idx else idx.get("amount")))
        if open_bal == 0.0:
            continue  # closed / fully-paid documents carry no open balance

        txn = _parse_date(_cell(cd, idx.get("date")))
        due = _parse_date(_cell(cd, idx.get("due_date")))
        # Overdue is measured from the due date; without one, fall back to txn date.
        ref = due or txn
        days_overdue = (as_of - ref).days if ref else None
        records.append((party, _cell(cd, idx.get("doc_num")).strip(),
                        txn, due, open_bal, days_overdue))
    return records


# ---------------------------------------------------------------------------
# Raw fetch
# ---------------------------------------------------------------------------

def fetch_aging_raw(side: str, as_of: str | None = None) -> dict[str, dict]:
    """Fetch the summary + detail aging reports for `side` as of today (or `as_of`)."""
    if side not in _ENDPOINTS:
        raise ValueError(f"Unknown aging side {side!r}; choose from {list(_ENDPOINTS)}")
    summary_report, detail_report = _ENDPOINTS[side]
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    session = get_session()

    log.info("API request — report=%s report_date=%s", summary_report, as_of)
    s = session.get(_report_url(summary_report), params={"report_date": as_of}, timeout=30)
    s.raise_for_status()
    log.info("API request — report=%s report_date=%s", detail_report, as_of)
    d = session.get(_report_url(detail_report), params={"report_date": as_of}, timeout=40)
    d.raise_for_status()
    return {"summary": s.json(), "detail": d.json()}


# ---------------------------------------------------------------------------
# DataFrame builder
# ---------------------------------------------------------------------------

def build_aging_dataframe(
    raw: dict[str, dict], report_config: dict, as_of: date | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build (buckets_df, detail_df) from a raw aging pull.

      buckets_df: columns [party, bucket, amount]; bucket order in .attrs["bucket_order"]
      detail_df:  columns [party, doc_num, txn_date, due_date, open_balance, days_overdue]
    """
    aliases = {k.lower(): v for k, v in (report_config.get("aliases") or {}).items()}
    as_of = as_of or datetime.now().date()

    bucket_records, bucket_order = _parse_summary(raw.get("summary", {}), aliases)
    detail_records = _parse_detail(raw.get("detail", {}), aliases, as_of)

    bdf = pd.DataFrame(bucket_records, columns=["party", "bucket", "amount"])
    # Collapse any duplicate party rows (alias merges) into one per bucket.
    if not bdf.empty:
        bdf = bdf.groupby(["party", "bucket"], as_index=False)["amount"].sum()
    bdf.attrs["bucket_order"] = bucket_order

    ddf = pd.DataFrame(
        detail_records,
        columns=["party", "doc_num", "txn_date", "due_date", "open_balance", "days_overdue"],
    )
    log.debug("Aging '%s': %d bucket rows, %d open documents",
              report_config.get("name", "?"), len(bdf), len(ddf))
    return bdf, ddf


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    side = sys.argv[1] if len(sys.argv) > 1 else "receivable"
    print(f"Fetching {side} aging…")
    raw = fetch_aging_raw(side)
    bdf, ddf = build_aging_dataframe(raw, {"name": side})
    print(f"\nBuckets ({bdf.attrs.get('bucket_order')}):")
    print(bdf.to_string(index=False))
    print(f"\nOpen documents: {len(ddf)}")
    if not ddf.empty:
        print(ddf.sort_values("days_overdue", ascending=False).head(10).to_string(index=False))
