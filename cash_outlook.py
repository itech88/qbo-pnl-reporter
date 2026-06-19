"""
Cash Outlook — a near-term cash *position* snapshot (deliberately not a forecast).

Composes already-fetched, already-reconciled inputs from the accrual Balance Sheet:
  • cash on hand          — sum of Bank-type accounts
  • A/R aging summary     — money owed to the practice (from aging_analytics.aging_summary)
  • Total Current Liabilities — money the practice owes (credit cards, A/P, short-term
                            loans, …), the Balance Sheet's own current-liabilities line

and presents: cash now, receivables (current vs overdue), current liabilities owed, and
the net position = cash + A/R − current liabilities. We base "owed" on Total Current
Liabilities rather than A/P alone because this client (and others) pays vendors by credit
card and never enters bills — so A/P is $0 while real short-term debt sits in credit-card
accounts. Current liabilities captures all of it (and naturally includes A/P for clients
who do enter bills). The A/R side keeps its "current" vs "overdue" split. Honest,
data-grounded, and consistent with the project's reconcile-before-send bar.

Public API:
    fetch_balance_sheet()                 -> raw json
    extract_balance_sheet(bs_json)        -> {"cash", "ar", "ap", "current_liabilities"}
                                             (None for any value not found)
    build_outlook(ar_summary, cash, current_liabilities) -> dict for the template
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
    """
    Fetch the Balance Sheet as of today (or `as_of`).

    Forces **Accrual** basis: A/R and A/P only exist on an accrual balance sheet (a
    cash-basis one has no receivables/payables), and the aging reports we reconcile
    against it are inherently accrual.
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    session = get_session()
    log.info("API request — report=BalanceSheet report_date=%s accounting=Accrual", as_of)
    r = session.get(
        _bs_url(),
        params={"report_date": as_of, "accounting_method": "Accrual"},
        timeout=30,
    )
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
    Pull cash on hand, A/R, A/P, and Total Current Liabilities from the Balance Sheet.

    Cash = the 'Total Bank Accounts' line; A/R / A/P = the receivable / payable lines;
    current_liabilities = the 'Total Current Liabilities' line (the figure Cash Outlook
    treats as "owed"). `ap` is retained for clients who enter bills, but is legitimately
    None for credit-card-only clients. Returns None for any value not found so callers
    can degrade gracefully.
    """
    pairs = list(_iter_labeled_amounts(bs_json.get("Rows", {}).get("Row", [])))

    def find(*needles: str):
        for label, val in pairs:
            low = label.lower()
            if any(n in low for n in needles):
                return val
        return None

    result = {
        "cash": find("total bank accounts", "bank accounts"),
        "ar":   find("accounts receivable", "a/r"),
        "ap":   find("accounts payable", "a/p"),
        "current_liabilities": find("total current liabilities"),
    }
    # Cash Outlook needs cash, A/R, and current liabilities — if any is missing, log the
    # labels we did see so the needles can be tuned to this realm's chart of accounts
    # (names only, not balances). A/P is intentionally excluded here: it's legitimately
    # absent for credit-card-only clients and must not trip a false alarm.
    if any(result[k] is None for k in ("cash", "ar", "current_liabilities")):
        log.warning("Balance Sheet lines not all found (cash=%s ar=%s current_liabilities=%s). "
                    "Labels seen: %s", result["cash"], result["ar"],
                    result["current_liabilities"], [lbl for lbl, _ in pairs])
    return result


def build_outlook(ar_summary: dict, cash: float | None,
                  current_liabilities: float | None) -> dict:
    """
    Compose the cash position from the A/R aging summary, cash on hand, and the Balance
    Sheet's Total Current Liabilities.

    Net position = cash + A/R − current liabilities (None if cash is unavailable, so the
    template can show '—' instead of a misleading number). The A/R side keeps its
    current/overdue split; current liabilities is a single figure (no aging breakdown).
    """
    ar_total = float(ar_summary.get("total", 0.0))
    owed = float(current_liabilities or 0.0)
    net = (cash + ar_total - owed) if cash is not None else None

    return {
        "cash":         cash,
        "ar_total":     ar_total,
        "ar_current":   float(ar_summary.get("current", 0.0)),
        "ar_overdue":   float(ar_summary.get("overdue", 0.0)),
        "current_liabilities": owed,
        "net_position": net,
    }
