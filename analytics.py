"""
COGS analytics layer — generalized for any P&L line item or section.

Inputs:  DataFrame from fetcher — columns: year, month, income, value
Outputs: three DataFrames consumed by report.py

  mom_df   — month-by-month for the current year
  yoy_df   — same-month comparison across all available years
  flags_df — months where value deviates beyond threshold from 3-yr average

metric parameter controls anomaly detection logic:
  'ratio'    — flag on value_pct deviation (percentage points)
  'absolute' — flag on relative value deviation (% change from 3-yr mean)
  'both'     — flag on value_pct deviation (same as ratio)
"""

import os
from datetime import datetime

import pandas as pd

_THRESHOLD = float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _value_pct(df: pd.DataFrame) -> pd.Series:
    """value as a fraction of income. Zero income → NaN."""
    return df["value"] / df["income"].replace(0, float("nan"))


def _month_abbr(month_num: int) -> str:
    return datetime(2000, month_num, 1).strftime("%b")


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def mom_analysis(df: pd.DataFrame, metric: str = "ratio") -> pd.DataFrame:
    """
    Month-by-month breakdown for the current calendar year.
    Returns columns: month, month_name, income, value, value_pct
    value_pct is always computed; template decides whether to display it.
    """
    current_year = datetime.now().year
    cur = df[df["year"] == current_year].copy()
    cur = cur.sort_values("month").reset_index(drop=True)
    cur["value_pct"]   = _value_pct(cur)
    cur["month_name"]  = cur["month"].apply(_month_abbr)
    return cur[["month", "month_name", "income", "value", "value_pct"]]


def yoy_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """
    Year-over-year same-month comparison.
    Returns one row per month (1–12) with per-year triples:
      income_{year}, value_{year}, value_pct_{year}
    """
    enriched = df.copy()
    enriched["value_pct"] = _value_pct(enriched)
    years = sorted(enriched["year"].unique())

    rows = []
    for m in range(1, 13):
        row: dict = {"month": m, "month_name": _month_abbr(m)}
        for y in years:
            subset = enriched[(enriched["year"] == y) & (enriched["month"] == m)]
            if subset.empty:
                row[f"income_{y}"]    = None
                row[f"value_{y}"]     = None
                row[f"value_pct_{y}"] = None
            else:
                row[f"income_{y}"]    = subset.iloc[0]["income"]
                row[f"value_{y}"]     = subset.iloc[0]["value"]
                row[f"value_pct_{y}"] = subset.iloc[0]["value_pct"]
        rows.append(row)

    return pd.DataFrame(rows)


def flag_anomalies(
    df: pd.DataFrame,
    metric: str = "ratio",
    threshold: float = _THRESHOLD,
) -> pd.DataFrame:
    """
    Flag months where the current year deviates from the 3-year monthly average.

    For 'ratio' / 'both': flags on value_pct deviation (percentage points).
    For 'absolute': flags on relative deviation of value from 3-yr mean.
    """
    enriched = df.copy()
    enriched["value_pct"] = _value_pct(enriched)

    current_year = datetime.now().year
    current      = enriched[enriched["year"] == current_year].set_index("month")
    historical   = enriched[enriched["year"] != current_year]

    use_pct = metric in ("ratio", "both")
    stat_col = "value_pct" if use_pct else "value"

    avg_by_month = (
        historical.groupby("month")[stat_col]
        .mean()
        .rename("avg_3yr")
    )

    flags = []
    for month, avg in avg_by_month.items():
        if pd.isna(avg) or month not in current.index:
            continue
        cur_val = current.loc[month, stat_col]
        if pd.isna(cur_val):
            continue

        if use_pct:
            deviation = cur_val - avg
            flag_condition = abs(deviation) >= threshold
        else:
            # Relative deviation: how far from the 3-yr mean as a fraction
            deviation = (cur_val - avg) / avg if avg != 0 else 0.0
            flag_condition = abs(deviation) >= threshold

        if flag_condition:
            flags.append({
                "month":           month,
                "month_name":      _month_abbr(month),
                "value_current":   current.loc[month, "value"],
                "pct_current":     current.loc[month, "value_pct"],
                "avg_3yr":         avg,
                "deviation":       deviation,
                "direction":       "HIGH" if deviation > 0 else "LOW",
            })

    return pd.DataFrame(
        flags,
        columns=["month", "month_name", "value_current", "pct_current",
                 "avg_3yr", "deviation", "direction"],
    )


def current_month_stats(
    df: pd.DataFrame,
    metric: str = "ratio",
    threshold: float = _THRESHOLD,
) -> dict | None:
    """
    Extract key stats for the most recently completed month with income data.
    Used by the scorecard.

    On the 1st of the month the current month has no data yet, so this
    correctly falls back to the prior month — the just-completed period
    the owner actually wants to review.
    Returns None if no income data exists for the current year.
    """
    today        = datetime.now()
    current_year = today.year

    enriched = df.copy()
    enriched["value_pct"] = _value_pct(enriched)

    # Most recent month in the current year that has actual income
    active = enriched[(enriched["year"] == current_year) & (enriched["income"] > 0)]
    if active.empty:
        return None

    report_month = int(active["month"].max())
    cur_row      = active[active["month"] == report_month].iloc[0]

    use_pct    = metric in ("ratio", "both")
    stat_col   = "value_pct" if use_pct else "value"
    historical = enriched[enriched["year"] != current_year]
    avg_3yr    = historical[historical["month"] == report_month][stat_col].mean()

    cur_stat  = cur_row[stat_col]
    deviation = float(cur_stat - avg_3yr) if not pd.isna(avg_3yr) else None

    return {
        "report_month":      report_month,
        "report_month_name": _month_abbr(report_month),
        "current_abs":       float(cur_row["value"]),
        "current_pct":       float(cur_row["value_pct"]) if not pd.isna(cur_row["value_pct"]) else None,
        "primary":           float(cur_stat),
        "avg_3yr":           float(avg_3yr) if not pd.isna(avg_3yr) else None,
        "deviation":         deviation,
        "use_pct":           use_pct,
    }


def run_all(
    df: pd.DataFrame,
    metric: str = "ratio",
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Returns (mom_df, yoy_df, flags_df)."""
    return mom_analysis(df, metric), yoy_analysis(df), flag_anomalies(df, metric)


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml, glob, os as _os
    from fetcher import fetch_raw_all, build_dataframe

    raw = fetch_raw_all()
    configs_dir = _os.path.join(_os.path.dirname(__file__), "reports")

    for path in sorted(glob.glob(_os.path.join(configs_dir, "*.yaml"))):
        with open(path) as f:
            cfg = yaml.safe_load(f)

        df = build_dataframe(raw, cfg)
        mom, yoy, flags = run_all(df, cfg["metric"])

        print(f"\n{'━'*60}")
        print(f"  {cfg['name']}  (metric={cfg['metric']})")
        print(f"{'━'*60}")
        print(mom[mom["income"] > 0].to_string(index=False))
        if not flags.empty:
            print(f"\n  Anomalies:")
            print(flags.to_string(index=False))
