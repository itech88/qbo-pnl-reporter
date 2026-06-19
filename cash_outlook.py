"""
Cash Outlook — a near-term cash *position* snapshot (deliberately not a forecast).

Composes three already-fetched, already-reconciled inputs:
  • cash on hand      — from the Balance Sheet (sum of Bank-type accounts)
  • A/R aging summary — money owed to the practice (from aging_analytics.aging_summary)
  • A/P aging summary — money the practice owes

and presents: cash now, receivables (current vs overdue), payables (current vs
overdue), and the net position = cash + A/R − A/P. We split each side into "current"
(not yet overdue) vs "overdue" rather than inventing dated cash-flow predictions —
honest, data-grounded, and consistent with the project's reconcile-before-send bar.

Public API:
    fetch_balance_sheet()                 -> raw json
    extract_balance_sheet(bs_json)        -> {"cash", "ar", "ap"}  (None if not found)
    build_outlook(ar_summary, ap_summary, cash) -> dict for the template
"""

from datetime import datetime

from dotenv import load_dotenv

from auth import get_session
from fetcher import _base_url, _realm_id
from logger import get_logger

load_dotenv()
log = get_logger(__name__)


def _to_float(value) -> float:
    if value is None or not str(value).strip():
        return 0.0
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def _bs_url() -> str:
    return f"{_base_url()}/v3/company/{_realm_id()}/reports/BalanceSheet"


def fetch_balance_sheet(as_of: str | None = None) -> dict:
    """Fetch the Balance Sheet as of today (or `as_of`)."""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    session = get_session()
    log.info("API request — report=BalanceSheet report_date=%s", as_of)
    r = session.get(_bs_url(), params={"report_date": as_of}, timeout=30)
    r.raise_for_status()
    return r.json()


def _iter_labeled_amounts(rows: list[dict]):
    """Yield (label, amount) for every Data row and every Section Summary, in order."""
    for row in rows:
        if row.get("type") == "Data":
            cd = row.get("ColData", [])
            if cd:
                yield cd[0].get("value", "").strip(), _to_float(cd[-1].get("value", ""))
        summary = row.get("Summary", {}).get("ColData", [])
        if summary:
            yield summary[0].get("value", "").strip(), _to_float(summary[-1].get("value", ""))
        if "Rows" in row:
            yield from _iter_labeled_amounts(row.get("Rows", {}).get("Row", []))


def extract_balance_sheet(bs_json: dict) -> dict:
    """
    Pull cash on hand, A/R, and A/P from the Balance Sheet.

    Cash = the 'Total Bank Accounts' line; A/R / A/P = the receivable / payable lines.
    Returns None for any value not found so callers can degrade gracefully.
    """
    pairs = list(_iter_labeled_amounts(bs_json.get("Rows", {}).get("Row", [])))

    def find(*needles: str):
        for label, val in pairs:
            low = label.lower()
            if any(n in low for n in needles):
                return val
        return None

    return {
        "cash": find("total bank accounts", "bank accounts"),
        "ar":   find("accounts receivable", "a/r"),
        "ap":   find("accounts payable", "a/p"),
    }


def build_outlook(ar_summary: dict, ap_summary: dict, cash: float | None) -> dict:
    """
    Compose the cash position from the A/R and A/P aging summaries plus cash on hand.

    Net position = cash + A/R − A/P (None if cash is unavailable, so the template
    can show '—' instead of a misleading number).
    """
    ar_total = float(ar_summary.get("total", 0.0))
    ap_total = float(ap_summary.get("total", 0.0))
    net = (cash + ar_total - ap_total) if cash is not None else None

    return {
        "cash":        cash,
        "ar_total":    ar_total,
        "ar_current":  float(ar_summary.get("current", 0.0)),
        "ar_overdue":  float(ar_summary.get("overdue", 0.0)),
        "ap_total":    ap_total,
        "ap_current":  float(ap_summary.get("current", 0.0)),
        "ap_overdue":  float(ap_summary.get("overdue", 0.0)),
        "net_position": net,
    }
