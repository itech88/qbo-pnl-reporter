"""
COGS analytics layer.

Inputs:  raw DataFrame from fetcher.fetch_all() — columns: year, month, income, cogs
Outputs: three enriched DataFrames consumed by report.py

  mom_df   — month-by-month COGS % for the current year
  yoy_df   — same-month comparison across the 3 available years
  flags_df — months where COGS % deviates beyond threshold from 3-yr avg
"""

import os
from datetime import datetime

import pandas as pd

_THRESHOLD = float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))  # default 5 pp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cogs_pct(df: pd.DataFrame) -> pd.Series:
    """COGS as a fraction of income (0–1). Months with zero income → NaN."""
    return df["cogs"] / df["income"].replace(0, float("nan"))


def _month_abbr(month_num: int) -> str:
    return datetime(2000, month_num, 1).strftime("%b")


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def mom_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """
    Month-over-month COGS % for the current calendar year.

    Returns columns: month, month_name, income, cogs, cogs_pct
    Rows with zero income are retained but cogs_pct is NaN.
    """
    current_year = datetime.now().year
    cur = df[df["year"] == current_year].copy()
    cur = cur.sort_values("month").reset_index(drop=True)
    cur["cogs_pct"] = _cogs_pct(cur)
    cur["month_name"] = cur["month"].apply(_month_abbr)
    return cur[["month", "month_name", "income", "cogs", "cogs_pct"]]


def yoy_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """
    Year-over-year same-month comparison across all years in the DataFrame.

    Returns a pivot where rows are months (1–12) and columns are years,
    with cogs_pct values. Also includes income and cogs columns per year
    for use in the summary table.

    Actual return shape: columns = month, month_name, then per-year triples:
      income_{year}, cogs_{year}, cogs_pct_{year}
    """
    enriched = df.copy()
    enriched["cogs_pct"] = _cogs_pct(enriched)

    years = sorted(enriched["year"].unique())
    months = range(1, 13)

    rows = []
    for m in months:
        row: dict = {"month": m, "month_name": _month_abbr(m)}
        for y in years:
            subset = enriched[(enriched["year"] == y) & (enriched["month"] == m)]
            if subset.empty:
                row[f"income_{y}"] = None
                row[f"cogs_{y}"] = None
                row[f"cogs_pct_{y}"] = None
            else:
                row[f"income_{y}"] = subset.iloc[0]["income"]
                row[f"cogs_{y}"] = subset.iloc[0]["cogs"]
                row[f"cogs_pct_{y}"] = subset.iloc[0]["cogs_pct"]
        rows.append(row)

    return pd.DataFrame(rows)


def flag_anomalies(df: pd.DataFrame, threshold: float = _THRESHOLD) -> pd.DataFrame:
    """
    Flag months where the current year's COGS % deviates from the 3-year
    monthly average by more than `threshold` (expressed as a fraction, e.g. 0.05 = 5 pp).

    Returns rows only for flagged months, with columns:
      month, month_name, cogs_pct_current, avg_3yr, deviation, direction
    """
    enriched = df.copy()
    enriched["cogs_pct"] = _cogs_pct(enriched)

    current_year = datetime.now().year
    current = enriched[enriched["year"] == current_year].set_index("month")
    historical = enriched[enriched["year"] != current_year]

    avg_by_month = (
        historical.groupby("month")["cogs_pct"]
        .mean()
        .rename("avg_3yr")
    )

    flags = []
    for month, avg in avg_by_month.items():
        if pd.isna(avg):
            continue
        if month not in current.index:
            continue
        cur_pct = current.loc[month, "cogs_pct"]
        if pd.isna(cur_pct):
            continue
        deviation = cur_pct - avg
        if abs(deviation) >= threshold:
            flags.append({
                "month": month,
                "month_name": _month_abbr(month),
                "cogs_pct_current": cur_pct,
                "avg_3yr": avg,
                "deviation": deviation,
                "direction": "HIGH" if deviation > 0 else "LOW",
            })

    return pd.DataFrame(
        flags,
        columns=["month", "month_name", "cogs_pct_current", "avg_3yr", "deviation", "direction"],
    )


def run_all(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Convenience wrapper — returns (mom_df, yoy_df, flags_df).
    Pass the raw DataFrame from fetcher.fetch_all().
    """
    return mom_analysis(df), yoy_analysis(df), flag_anomalies(df)


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from fetcher import fetch_all

    print("Fetching data…")
    raw = fetch_all()

    mom, yoy, flags = run_all(raw)

    print("\n── Month-over-month (current year) ──")
    print(mom.to_string(index=False))

    print("\n── Year-over-year (all years) ──")
    print(yoy.to_string(index=False))

    print(f"\n── Anomalies (threshold ±{_THRESHOLD:.0%}) ──")
    if flags.empty:
        print("No anomalies detected.")
    else:
        print(flags.to_string(index=False))
