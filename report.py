"""
Report builder.

Renders the Jinja2 HTML email template with an embedded matplotlib chart.

Public API:
    build_report(mom_df, yoy_df, flags_df) -> str   # complete HTML string
"""

import base64
import io
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # non-interactive backend — safe for cron/CI
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_CURRENT_YEAR = datetime.now().year


# ---------------------------------------------------------------------------
# Jinja2 filters
# ---------------------------------------------------------------------------

def _fmt_currency(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"${value:,.2f}"


def _fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value * 100:.1f}%"


def _jinja_env() -> Environment:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    env.filters["currency"] = _fmt_currency
    env.filters["pct"] = _fmt_pct
    return env


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def _build_chart(mom_df: pd.DataFrame, yoy_df: pd.DataFrame) -> str:
    """Render COGS % bar chart with prior-year line overlays. Returns base64 PNG."""
    active = mom_df[mom_df["income"] > 0].copy()
    months = active["month"].tolist()
    labels = active["month_name"].tolist()

    fig, ax = plt.subplots(figsize=(9, 4), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    # Current year bars
    bar_pcts = (active["cogs_pct"].fillna(0) * 100).tolist()
    ax.bar(months, bar_pcts, color="#2563eb", alpha=0.85,
           label=str(_CURRENT_YEAR), zorder=3, width=0.6)

    # Prior year lines
    prior_years = sorted(
        c.replace("cogs_pct_", "")
        for c in yoy_df.columns
        if c.startswith("cogs_pct_") and not c.endswith(str(_CURRENT_YEAR))
    )
    palette = ["#94a3b8", "#64748b"]
    for i, yr in enumerate(prior_years):
        col = f"cogs_pct_{yr}"
        if col not in yoy_df.columns:
            continue
        subset = yoy_df[yoy_df["month"].isin(months)]
        pcts = (subset[col] * 100).tolist()
        ax.plot(
            subset["month"].tolist(), pcts,
            color=palette[i % len(palette)], linewidth=1.5,
            marker="o", markersize=4, label=yr, zorder=4, linestyle="--",
        )

    ax.set_xticks(months)
    ax.set_xticklabels(labels, fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    ax.tick_params(axis="y", labelsize=9)
    ax.set_ylabel("COGS % of Income", fontsize=9)
    ax.set_title(
        f"COGS % of Income — {_CURRENT_YEAR} vs Prior Years",
        fontsize=11, fontweight="bold", pad=12,
    )
    ax.legend(fontsize=8, framealpha=0.7)
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("utf-8")


# ---------------------------------------------------------------------------
# Template context builders
# ---------------------------------------------------------------------------

def _mom_rows(mom_df: pd.DataFrame, flags_df: pd.DataFrame) -> list[dict]:
    """Build row dicts for the MoM table, skipping months with no income."""
    flagged_high = set(flags_df[flags_df["direction"] == "HIGH"]["month"].tolist()) if not flags_df.empty else set()
    flagged_low  = set(flags_df[flags_df["direction"] == "LOW"]["month"].tolist())  if not flags_df.empty else set()

    rows = []
    for _, r in mom_df[mom_df["income"] > 0].iterrows():
        m = int(r["month"])
        if m in flagged_high:
            color = "#dc2626"   # red
        elif m in flagged_low:
            color = "#16a34a"   # green
        else:
            color = "#1e293b"   # neutral

        rows.append({
            "month_name":   r["month_name"],
            "income":       r["income"],
            "cogs":         r["cogs"],
            "cogs_pct":     r["cogs_pct"],
            "pct_color":    color,
        })
    return rows


def _yoy_rows(yoy_df: pd.DataFrame, years: list[str]) -> list[dict]:
    """Build row dicts for the YoY table, skipping months with no data in any year."""
    pct_cols = [f"cogs_pct_{y}" for y in years]
    # Keep months that have at least one non-null, non-zero value across all years
    has_data = yoy_df[pct_cols].notna().any(axis=1) & (yoy_df[pct_cols].fillna(0) != 0).any(axis=1)
    subset = yoy_df[has_data]

    rows = []
    for _, r in subset.iterrows():
        row = {"month_name": r["month_name"]}
        for y in years:
            row[f"pct_{y}"] = _fmt_pct(r.get(f"cogs_pct_{y}"))
        rows.append(row)
    return rows


def _summary_stats(mom_df: pd.DataFrame) -> dict:
    """Compute headline numbers shown in the email subheader."""
    active = mom_df[mom_df["income"] > 0]
    if active.empty:
        return {"avg_cogs_pct": None, "ytd_income": 0.0, "ytd_cogs": 0.0}
    return {
        "avg_cogs_pct": active["cogs_pct"].mean(),
        "ytd_income":   active["income"].sum(),
        "ytd_cogs":     active["cogs"].sum(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_report(
    mom_df: pd.DataFrame,
    yoy_df: pd.DataFrame,
    flags_df: pd.DataFrame,
) -> str:
    """Render and return the full HTML email string."""
    years = sorted(
        c.replace("cogs_pct_", "")
        for c in yoy_df.columns
        if c.startswith("cogs_pct_")
    )

    context = {
        "current_year":  _CURRENT_YEAR,
        "report_date":   datetime.now().strftime("%B %d, %Y"),
        "chart_b64":     _build_chart(mom_df, yoy_df),
        "mom_rows":      _mom_rows(mom_df, flags_df),
        "yoy_rows":      _yoy_rows(yoy_df, years),
        "years":         years,
        "flags":         flags_df.to_dict("records") if not flags_df.empty else [],
        "stats":         _summary_stats(mom_df),
    }

    template = _jinja_env().get_template("report.html")
    return template.render(**context)


# ---------------------------------------------------------------------------
# CLI — writes report.html to disk for browser preview
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from fetcher import fetch_all
    from analytics import run_all

    print("Fetching data…")
    raw = fetch_all()
    mom, yoy, flags = run_all(raw)

    html = build_report(mom, yoy, flags)
    out_path = os.path.join(os.path.dirname(__file__), "preview_report.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"Report written to {out_path} — open in a browser to preview.")
