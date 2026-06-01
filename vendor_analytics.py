"""
Vendor COGS analytics.

Inputs:  long DataFrame from vendor_fetcher — columns: year, month, vendor, amount
Outputs: structures consumed by the vendor report renderer.

Key views:
  current_month_breakdown() — each vendor's $ and % share for the most recent
                              month that has COGS data
  monthly_share_matrix()    — vendor × month grid for the current year (chart)
  vendor_yoy()              — same-month spend per vendor across the 3 years
"""

from datetime import datetime

import pandas as pd


def _month_abbr(m: int) -> str:
    return datetime(2000, m, 1).strftime("%b")


def latest_period(df: pd.DataFrame) -> tuple[int, int] | None:
    """Return (year, month) of the most recent period with any COGS spend."""
    if df.empty:
        return None
    cur_year = int(df["year"].max())
    year_df = df[df["year"] == cur_year]
    if year_df.empty:
        return None
    return cur_year, int(year_df["month"].max())


def current_month_breakdown(df: pd.DataFrame) -> dict:
    """
    Vendor breakdown for the most recent month with data.
    Returns {year, month, month_name, total, vendors: [{vendor, amount, share}]}.
    """
    period = latest_period(df)
    if period is None:
        return {"year": None, "month": None, "month_name": "", "total": 0.0, "vendors": []}

    year, month = period
    snap = df[(df["year"] == year) & (df["month"] == month)].copy()
    total = float(snap["amount"].sum())

    vendors = []
    for _, r in snap.sort_values("amount", ascending=False).iterrows():
        amt = float(r["amount"])
        vendors.append({
            "vendor": r["vendor"],
            "amount": amt,
            "share":  (amt / total) if total else 0.0,
        })

    return {
        "year":       year,
        "month":      month,
        "month_name": _month_abbr(month),
        "total":      total,
        "vendors":    vendors,
    }


def monthly_share_matrix(df: pd.DataFrame, top_n: int = 6) -> dict:
    """
    Build a vendor × month matrix of COGS spend for the current year.
    Vendors beyond the top_n (by YTD spend) are folded into 'Other'.

    Returns {months: [ints], vendors: [names], series: {vendor: [amounts per month]}}.
    """
    period = latest_period(df)
    if period is None:
        return {"months": [], "vendors": [], "series": {}}

    year = period[0]
    cur = df[df["year"] == year]
    if cur.empty:
        return {"months": [], "vendors": [], "series": {}}

    months = sorted(cur["month"].unique().tolist())

    # Rank vendors by YTD spend
    ytd = cur.groupby("vendor")["amount"].sum().sort_values(ascending=False)
    top_vendors = ytd.head(top_n).index.tolist()
    has_other = len(ytd) > top_n

    series: dict[str, list[float]] = {v: [0.0] * len(months) for v in top_vendors}
    if has_other:
        series["Other"] = [0.0] * len(months)

    month_pos = {m: i for i, m in enumerate(months)}
    for _, r in cur.iterrows():
        v = r["vendor"] if r["vendor"] in top_vendors else "Other"
        if v not in series:
            continue
        series[v][month_pos[int(r["month"])]] += float(r["amount"])

    vendors = top_vendors + (["Other"] if has_other else [])
    return {"months": months, "vendors": vendors, "series": series}


def vendor_yoy(df: pd.DataFrame) -> dict:
    """
    Same-month year-over-year spend per vendor for the most recent month.
    Returns {month_name, years: [ints], rows: [{vendor, by_year: {year: amount}}]}.
    """
    period = latest_period(df)
    if period is None:
        return {"month_name": "", "years": [], "rows": []}

    _, month = period
    years = sorted(df["year"].unique().tolist())
    same_month = df[df["month"] == month]

    # Union of vendors that appear in this month across any year
    vendors = (
        same_month.groupby("vendor")["amount"].sum()
        .sort_values(ascending=False).index.tolist()
    )

    rows = []
    for v in vendors:
        by_year = {}
        for y in years:
            cell = same_month[(same_month["year"] == y) & (same_month["vendor"] == v)]
            by_year[y] = float(cell["amount"].sum()) if not cell.empty else 0.0
        rows.append({"vendor": v, "by_year": by_year})

    return {"month_name": _month_abbr(month), "years": years, "rows": rows}
