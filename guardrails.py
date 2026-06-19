"""
Pre-send data-quality guardrails — stateless reconciliation + sanity checks.

No storage, no system of record. We never compare against a saved prior pull;
instead we reconcile each fetch against QBO's *own* authoritative totals and the
arithmetic identities of a P&L, entirely within a single pull:

  • Accounting identities (cross-foot):
        Income − COGS              = Gross Profit
        Gross Profit − OpEx        = Net Operating Income
  • Cross-report tie:
        Σ(per-vendor COGS detail)  ≈ COGS summary total
        (the ProfitAndLossDetail endpoint must agree with ProfitAndLoss)
  • Per-report plausibility bands (a value that is wildly off its income base
    almost always means a parsing/extraction break, not real data).

A check that trips does not crash the run — the scheduler holds only the affected
report back from delivery, sends the rest, and alerts the operator. All functions
here are pure and side-effect free so they unit-test without any network or state.

Report-name constants match the `name` field in reports/*.yaml.
"""

import math
import os

import pandas as pd

# Report names that participate in reconciliation (must match reports/*.yaml).
INCOME       = "income"   # not a report — the shared income base in each DataFrame
COGS         = "COGS"
GROSS_PROFIT = "Gross Profit"
NOI          = "Net Operating Income"
OPEX         = "Total Operating Expense Ratio"
VENDOR       = "COGS by Vendor"


def tolerance() -> float:
    """Absolute dollar tolerance for reconciliation, configurable via env.

    Tolerant of an unset *or empty* RECON_TOLERANCE (CI passes secrets through as
    empty strings when they don't exist), falling back to $1.00.
    """
    return float(os.getenv("RECON_TOLERANCE") or "1.00")


# ---------------------------------------------------------------------------
# Per-report sanity
# ---------------------------------------------------------------------------

def report_sanity(
    df: pd.DataFrame,
    year: int,
    month: int,
    metric: str = "ratio",
    partial: bool = False,
) -> list[str]:
    """
    Plausibility checks on a single report's reporting-month row.

    Returns a list of human-readable failure reasons (empty list = passed). Catches
    the failure modes that mean "the data is wrong," not "the business had a bad
    month": missing row, non-finite value, or a value that is an implausible
    multiple of income (a hallmark of a broken extraction).

    The ratio band is skipped when `partial` (the reporting month is the current,
    incomplete calendar month): a few days of income against a near-full month of
    posted expenses produces large-but-legitimate month-to-date ratios. Structural
    checks (missing row, non-finite) still apply, as do the identity and vendor
    reconciliations, which don't depend on the month being complete.
    """
    row = df[(df["year"] == year) & (df["month"] == month)]
    if row.empty:
        return [f"no data row for {year}-{month:02d}"]

    income = float(row.iloc[0]["income"])
    value  = float(row.iloc[0]["value"])
    reasons: list[str] = []

    if not math.isfinite(value):
        reasons.append(f"value is non-finite ({value})")
    if income < 0:
        reasons.append(f"income is negative ({income:,.2f})")

    # A ratio metric whose value is an absurd fraction of income (outside
    # -200%..200%) is almost certainly a parsing error — but only for a *complete*
    # month; mid-month, an extreme ratio is just month-to-date and is labelled, not held.
    if not partial and metric in ("ratio", "both") and income > 0 and math.isfinite(value):
        pct = value / income
        if not (-2.0 <= pct <= 2.0):
            reasons.append(
                f"value is {pct * 100:,.0f}% of income (outside the -200%..200% sanity band)"
            )

    return reasons


# ---------------------------------------------------------------------------
# Cross-foot reconciliation against QBO's own totals
# ---------------------------------------------------------------------------

def reconcile_identities(
    totals: dict[str, float | None],
) -> list[tuple[str, tuple[str, ...]]]:
    """
    Cross-foot the P&L for one month using the section totals QBO itself returned.

    `totals` maps report-name -> that section's value for the reporting month, plus
    `INCOME`. Only identities whose inputs are all present are checked, so a partial
    run (e.g. a single --report) simply skips what it can't verify.

    Returns a list of (reason, implicated_report_names). The scheduler holds each
    implicated report that is actually being sent.
    """
    tol = tolerance()
    income = totals.get(INCOME)
    cogs   = totals.get(COGS)
    gp     = totals.get(GROSS_PROFIT)
    opex   = totals.get(OPEX)
    noi    = totals.get(NOI)

    findings: list[tuple[str, tuple[str, ...]]] = []

    if None not in (income, cogs, gp):
        diff = (income - cogs) - gp
        if abs(diff) > tol:
            findings.append((
                f"Income − COGS ≠ Gross Profit (off by {diff:+,.2f}: "
                f"{income:,.2f} − {cogs:,.2f} should equal {gp:,.2f})",
                (COGS, GROSS_PROFIT),
            ))

    if None not in (gp, opex, noi):
        diff = (gp - opex) - noi
        if abs(diff) > tol:
            findings.append((
                f"Gross Profit − Operating Expenses ≠ Net Operating Income "
                f"(off by {diff:+,.2f}: {gp:,.2f} − {opex:,.2f} should equal {noi:,.2f})",
                (OPEX, NOI),
            ))

    return findings


def reconcile_vendor(
    cogs_summary: float | None,
    vendor_detail_total: float | None,
) -> list[str]:
    """
    Tie the per-vendor COGS detail to the COGS summary total for the same month.

    Tolerance is the larger of the absolute dollar tolerance or 1% of COGS, since
    the two QBO endpoints can round slightly differently. Returns failure reasons
    (empty = tied, or not enough data to check).
    """
    if cogs_summary is None or vendor_detail_total is None:
        return []

    tol = max(tolerance(), abs(cogs_summary) * 0.01)
    diff = vendor_detail_total - cogs_summary
    if abs(diff) > tol:
        return [
            f"vendor detail total {vendor_detail_total:,.2f} ≠ COGS summary "
            f"{cogs_summary:,.2f} (off by {diff:+,.2f})"
        ]
    return []


# ---------------------------------------------------------------------------
# Aging reconciliation (A/R and A/P)
# ---------------------------------------------------------------------------

def reconcile_aging(
    summary_total: float | None,
    detail_total: float | None,
    label: str = "aging",
) -> list[str]:
    """
    Tie the open-document detail to the aging summary grand total (same snapshot).

    QBO's AgedReceivables/AgedPayables summary buckets and the matching detail report
    are two endpoints over the same data; if Σ(open balances) ≠ the bucketed total,
    something was mis-parsed or mid-sync, so the report is held rather than sent.
    Tolerance is the larger of the absolute dollar tolerance or 1% of the total.
    Returns failure reasons (empty = tied, or not enough data to check).
    """
    if summary_total is None or detail_total is None:
        return []

    tol = max(tolerance(), abs(summary_total) * 0.01)
    diff = detail_total - summary_total
    if abs(diff) > tol:
        return [
            f"{label} detail total {detail_total:,.2f} ≠ aging summary "
            f"{summary_total:,.2f} (off by {diff:+,.2f})"
        ]
    return []


def reconcile_balance_sheet(
    bs_total: float | None,
    aging_total: float | None,
    label: str = "A/R",
) -> list[str]:
    """
    Cross-anchor the aging total against the Balance Sheet's own A/R or A/P line.

    A third independent witness (the Balance Sheet) to the same figure: if it
    disagrees with the aging grand total beyond tolerance, the snapshot is internally
    inconsistent and the dependent report should be held. Skipped when either input
    is missing (e.g. the Balance Sheet line could not be located).
    """
    if bs_total is None or aging_total is None:
        return []

    tol = max(tolerance(), abs(bs_total) * 0.01)
    diff = aging_total - bs_total
    if abs(diff) > tol:
        return [
            f"{label} aging total {aging_total:,.2f} ≠ Balance Sheet {label} "
            f"{bs_total:,.2f} (off by {diff:+,.2f})"
        ]
    return []
