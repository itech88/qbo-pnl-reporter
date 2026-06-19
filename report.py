"""
Report builder.

Renders the Jinja2 HTML email template with an embedded matplotlib chart.

Public API:
    build_report(mom_df, yoy_df, flags_df, report_config) -> tuple[str, bytes]
"""

import base64
import io
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
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
    prefix = "-$" if value < 0 else "$"
    return f"{prefix}{abs(value):,.2f}"


def _fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return f"{value * 100:.1f}%"


def _jinja_env() -> Environment:
    env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)
    env.filters["currency"]      = _fmt_currency
    env.filters["pct"]           = _fmt_pct
    env.filters["format_number"] = lambda v: f"{int(v):,}"
    return env


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def _build_chart(
    mom_df: pd.DataFrame,
    yoy_df: pd.DataFrame,
    report_config: dict,
) -> bytes:
    """Render chart. Returns raw PNG bytes."""
    metric     = report_config.get("metric", "ratio")
    label      = report_config.get("name", "Value")
    use_pct    = metric in ("ratio", "both")
    plot_col   = "value_pct" if use_pct else "value"
    y_label    = f"{label} % of Income" if use_pct else f"{label} ($)"

    active  = mom_df[mom_df["income"] > 0].copy()
    months  = active["month"].tolist()
    labels  = active["month_name"].tolist()
    bar_vals = (active[plot_col].fillna(0) * (100 if use_pct else 1)).tolist()

    fig, ax = plt.subplots(figsize=(9, 4), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    bars = ax.bar(months, bar_vals, color="#2563eb", alpha=0.85,
                  label=str(_CURRENT_YEAR), zorder=3, width=0.6)

    # Colour negative bars differently
    for bar, val in zip(bars, bar_vals):
        if val < 0:
            bar.set_color("#dc2626")

    # Prior-year lines
    prior_years = sorted(
        c.replace("value_pct_", "").replace("value_", "")
        for c in yoy_df.columns
        if (c.startswith("value_pct_") if use_pct else c.startswith("value_"))
        and not c.endswith(str(_CURRENT_YEAR))
        and "_pct_" not in c.replace("value_pct_", "X")  # avoid double-match
    )
    # Cleaner: just derive prior years from yoy_df columns
    yr_cols = [c for c in yoy_df.columns if c.startswith("value_pct_" if use_pct else "value_")
               and not c.startswith("value_pct_" if not use_pct else "X")]
    prior_years = sorted(
        c.split("_")[-1] for c in yr_cols if c.split("_")[-1] != str(_CURRENT_YEAR)
    )

    palette = ["#94a3b8", "#64748b"]
    for i, yr in enumerate(prior_years):
        col = f"value_pct_{yr}" if use_pct else f"value_{yr}"
        if col not in yoy_df.columns:
            continue
        subset = yoy_df[yoy_df["month"].isin(months)]
        pcts = (subset[col] * (100 if use_pct else 1)).tolist()
        ax.plot(
            subset["month"].tolist(), pcts,
            color=palette[i % len(palette)], linewidth=1.5,
            marker="o", markersize=4, label=yr, zorder=4, linestyle="--",
        )

    ax.axhline(0, color="#64748b", linewidth=0.8, linestyle="-")
    ax.set_xticks(months)
    ax.set_xticklabels(labels, fontsize=9)

    if use_pct:
        ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f"{y:.0f}%"))
    else:
        ax.yaxis.set_major_formatter(
            mticker.FuncFormatter(lambda y, _: f"${y:,.0f}" if y >= 0 else f"-${abs(y):,.0f}")
        )

    ax.tick_params(axis="y", labelsize=9)
    ax.set_ylabel(y_label, fontsize=9)
    ax.set_title(
        f"{label} — {_CURRENT_YEAR} vs Prior Years",
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
    return buf.read()


# ---------------------------------------------------------------------------
# Template context builders
# ---------------------------------------------------------------------------

def _mom_rows(
    mom_df: pd.DataFrame,
    flags_df: pd.DataFrame,
    metric: str,
) -> list[dict]:
    flagged_high = set(flags_df[flags_df["direction"] == "HIGH"]["month"].tolist()) if not flags_df.empty else set()
    flagged_low  = set(flags_df[flags_df["direction"] == "LOW"]["month"].tolist())  if not flags_df.empty else set()

    rows = []
    for _, r in mom_df[mom_df["income"] > 0].iterrows():
        m = int(r["month"])
        if m in flagged_high:
            color = "#dc2626"
        elif m in flagged_low:
            color = "#16a34a"
        else:
            color = "#1e293b"

        # For negative value_pct (e.g. net operating loss), always red
        if not pd.isna(r.get("value_pct")) and r["value_pct"] < 0:
            color = "#dc2626"

        rows.append({
            "month_name": r["month_name"],
            "income":     r["income"],
            "value":      r["value"],
            "value_pct":  r["value_pct"],
            "pct_color":  color,
        })
    return rows


def _yoy_rows(
    yoy_df: pd.DataFrame,
    years: list[str],
    metric: str,
) -> list[dict]:
    use_pct = metric in ("ratio", "both")
    pct_cols = [f"value_pct_{y}" for y in years]
    val_cols = [f"value_{y}"     for y in years]

    check_cols = pct_cols if use_pct else val_cols
    has_data = (
        yoy_df[check_cols].notna().any(axis=1) &
        (yoy_df[check_cols].fillna(0) != 0).any(axis=1)
    )
    subset = yoy_df[has_data]

    rows = []
    for _, r in subset.iterrows():
        row: dict = {"month_name": r["month_name"]}
        for y in years:
            row[f"pct_{y}"] = _fmt_pct(r.get(f"value_pct_{y}"))
            row[f"val_{y}"] = _fmt_currency(r.get(f"value_{y}"))
        rows.append(row)
    return rows


def _summary_stats(mom_df: pd.DataFrame) -> dict:
    active = mom_df[mom_df["income"] > 0]
    if active.empty:
        return {"avg_value_pct": None, "ytd_income": 0.0, "ytd_value": 0.0}
    return {
        "avg_value_pct": active["value_pct"].mean(),
        "ytd_income":    active["income"].sum(),
        "ytd_value":     active["value"].sum(),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_report(
    mom_df: pd.DataFrame,
    yoy_df: pd.DataFrame,
    flags_df: pd.DataFrame,
    report_config: dict,
) -> tuple[str, bytes]:
    """
    Render the HTML email and chart for the given report config.
    Returns (html, chart_png_bytes).
    """
    metric = report_config.get("metric", "ratio")
    years  = sorted(
        c.replace("value_pct_", "")
        for c in yoy_df.columns
        if c.startswith("value_pct_")
    )

    chart_png = _build_chart(mom_df, yoy_df, report_config)

    # Partial = the latest month with income is the current calendar month, i.e.
    # the figures are month-to-date and will change as the month completes.
    now = datetime.now()
    _active = mom_df[mom_df["income"] > 0]
    _report_month = int(_active["month"].max()) if not _active.empty else None
    partial = _report_month is not None and _report_month == now.month

    context = {
        "report_name":   report_config["name"],
        "current_year":  _CURRENT_YEAR,
        "report_date":   datetime.now().strftime("%B %d, %Y"),
        "partial":       partial,
        "partial_month": now.strftime("%B %Y"),
        "metric":        metric,
        "show_income":   metric in ("ratio", "both"),
        "show_pct":      metric in ("ratio", "both"),
        "value_label":   report_config["name"],
        "mom_rows":      _mom_rows(mom_df, flags_df, metric),
        "yoy_rows":      _yoy_rows(yoy_df, years, metric),
        "years":         years,
        "flags":         flags_df.to_dict("records") if not flags_df.empty else [],
        "stats":         _summary_stats(mom_df),
    }

    template = _jinja_env().get_template("report.html")
    return template.render(**context), chart_png


# ---------------------------------------------------------------------------
# Vendor COGS report
# ---------------------------------------------------------------------------

_VENDOR_PALETTE = [
    "#2563eb", "#16a34a", "#ca8a04", "#dc2626", "#7c3aed",
    "#0891b2", "#db2777", "#94a3b8",
]


def _vendor_chart(matrix: dict, report_name: str) -> bytes:
    """Stacked bar chart — COGS spend per vendor across the current year's months."""
    months   = matrix["months"]
    vendors  = matrix["vendors"]
    series   = matrix["series"]
    labels   = [datetime(2000, m, 1).strftime("%b") for m in months]

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    bottoms = [0.0] * len(months)
    for i, v in enumerate(vendors):
        vals  = series.get(v, [0.0] * len(months))
        color = "#cbd5e1" if v == "Other" else _VENDOR_PALETTE[i % len(_VENDOR_PALETTE)]
        ax.bar(range(len(months)), vals, bottom=bottoms, label=v,
               color=color, width=0.6, zorder=3)
        bottoms = [b + x for b, x in zip(bottoms, vals)]

    ax.set_xticks(range(len(months)))
    ax.set_xticklabels(labels, fontsize=9)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(lambda y, _: f"${y:,.0f}"))
    ax.tick_params(axis="y", labelsize=9)
    ax.set_ylabel("COGS Spend", fontsize=9)
    ax.set_title(f"{report_name} by Month", fontsize=11, fontweight="bold", pad=12)
    ax.legend(fontsize=7.5, framealpha=0.8, ncol=2, loc="upper left")
    ax.grid(axis="y", linestyle="--", alpha=0.5, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_vendor_report(
    df,
    report_config: dict,
    report_period: tuple[int, int] | None = None,
) -> tuple[str, bytes]:
    """
    Render the COGS-by-vendor report.
    `df` is the long DataFrame from vendor_fetcher (year, month, vendor, amount).
    `report_period` (year, month), when given, pins the breakdown to the same
    reporting month the metric reports use, so the vendor view tracks them instead
    of lagging to the last month with posted detail. Defaults to the latest month
    with data.
    """
    from vendor_analytics import (
        current_month_breakdown, monthly_share_matrix, vendor_yoy,
    )

    breakdown = current_month_breakdown(df, report_period)
    matrix    = monthly_share_matrix(df, top_n=6)
    yoy       = vendor_yoy(df, report_period)

    now = datetime.now()
    partial = breakdown["year"] == now.year and breakdown["month"] == now.month

    chart_png = _vendor_chart(matrix, report_config["name"])

    # Pre-format breakdown rows
    breakdown_rows = [
        {
            "vendor": v["vendor"],
            "amount": _fmt_currency(v["amount"]),
            "share":  f"{v['share'] * 100:.1f}%",
            "bar_w":  round(v["share"] * 100, 1),
        }
        for v in breakdown["vendors"]
    ]

    # Pre-format YoY rows
    yoy_rows = []
    for r in yoy["rows"]:
        cells = {"vendor": r["vendor"]}
        for y in yoy["years"]:
            cells[f"y_{y}"] = _fmt_currency(r["by_year"].get(y, 0.0))
        yoy_rows.append(cells)

    context = {
        "report_name":    report_config["name"],
        "report_date":    datetime.now().strftime("%B %d, %Y"),
        "current_year":   _CURRENT_YEAR,
        "partial":        partial,
        "partial_month":  now.strftime("%B %Y"),
        "period_label":   f"{breakdown['month_name']} {breakdown['year']}" if breakdown["year"] else "—",
        "total":          _fmt_currency(breakdown["total"]),
        "vendor_count":   len(breakdown["vendors"]),
        "breakdown_rows": breakdown_rows,
        "yoy_years":      yoy["years"],
        "yoy_month":      yoy["month_name"],
        "yoy_rows":       yoy_rows,
    }

    template = _jinja_env().get_template("vendor_report.html")
    return template.render(**context), chart_png


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

def _scorecard_chart(metrics: list[dict]) -> bytes:
    """Horizontal diverging bar chart — deviation from 3-yr average per metric."""
    names      = [m["name"] for m in metrics]
    deviations = [m["deviation"] or 0.0 for m in metrics]
    colors     = [m["bar_color"] for m in metrics]

    fig, ax = plt.subplots(figsize=(8, max(3, len(names) * 0.55)), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    y_pos = range(len(names))
    ax.barh(list(y_pos), deviations, color=colors, alpha=0.85, height=0.55)
    ax.axvline(0, color="#475569", linewidth=1.0, linestyle="-")

    ax.set_yticks(list(y_pos))
    ax.set_yticklabels(names, fontsize=8)
    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"{x*100:+.1f}pp" if abs(x) < 1 else f"{x*100:+.0f}%")
    )
    ax.tick_params(axis="x", labelsize=8)
    ax.set_xlabel("Deviation from 3-Year Monthly Average", fontsize=8)
    ax.set_title("This Month vs 3-Year Average", fontsize=10, fontweight="bold", pad=10)
    ax.grid(axis="x", linestyle="--", alpha=0.4, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    ax.invert_yaxis()

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_scorecard(
    metrics: list[dict],
    scorecard_config: dict,
) -> tuple[str, bytes]:
    """
    Render the monthly business dashboard scorecard.

    Each item in `metrics` is the output of analytics.current_month_stats()
    augmented with 'name' and 'higher_is_better' from the report config.
    """
    threshold = float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))

    for m in metrics:
        dev = m.get("deviation")
        hib = m.get("higher_is_better", False)

        if dev is None or m.get("avg_3yr") is None:
            m["status"]    = "GREY"
            m["status_bg"] = "#f1f5f9"
            m["status_fg"] = "#64748b"
            m["bar_color"] = "#94a3b8"
            continue

        # Flip sign for metrics where higher is better so RED always = bad
        signed = dev if not hib else -dev
        if abs(dev) < threshold:
            m["status"]    = "ON TRACK"
            m["status_bg"] = "#dcfce7"
            m["status_fg"] = "#166534"
            m["bar_color"] = "#16a34a"
        elif abs(dev) < threshold * 2:
            m["status"]    = "WATCH"
            m["status_bg"] = "#fef9c3"
            m["status_fg"] = "#854d0e"
            m["bar_color"] = "#ca8a04"
        else:
            m["status"]    = "ACTION"
            m["status_bg"] = "#fee2e2"
            m["status_fg"] = "#991b1b"
            m["bar_color"] = "#dc2626" if signed > 0 else "#2563eb"

        m["deviation_fmt"] = (
            f"{dev * 100:+.1f}pp" if m["use_pct"] else f"{dev:+,.0f}"
        )

    chart_png = _scorecard_chart(metrics)

    # Derive the reported month from the first metric that has data
    now = datetime.now()
    report_month_name = next(
        (m.get("report_month_name", "") for m in metrics if m.get("report_month_name")),
        now.strftime("%B"),
    )
    _rm = next((m.get("report_month") for m in metrics if m.get("report_month")), None)
    partial = _rm is not None and _rm == now.month

    context = {
        "report_date":       now.strftime("%B %d, %Y"),
        "current_year":      _CURRENT_YEAR,
        "report_month_name": report_month_name,
        "partial":           partial,
        "partial_month":     now.strftime("%B %Y"),
        "metrics":           metrics,
        "threshold_pct":     f"{threshold * 100:.0f}",
    }

    template = _jinja_env().get_template("scorecard.html")
    return template.render(**context), chart_png


# ---------------------------------------------------------------------------
# Aging report (A/R and A/P) — point-in-time snapshot, no MoM/YoY
# ---------------------------------------------------------------------------

# Overdue buckets shade from amber to deep red as they age; 'Current' is green.
_CURRENT_COLOR       = "#16a34a"
_AGING_BUCKET_COLORS = ["#ca8a04", "#f97316", "#dc2626", "#991b1b", "#7f1d1d"]

_SIDE_META = {
    "receivable": {
        "party_label": "Payer", "flow_label": "DSO",
        "flow_name": "Days Sales Outstanding", "owed": "owed to you",
        "empty": "No open receivables — nothing outstanding.",
    },
    "payable": {
        "party_label": "Vendor", "flow_label": "DPO",
        "flow_name": "Days Payable Outstanding", "owed": "owed by you",
        "empty": "No open payables — nothing outstanding.",
    },
}


def _is_current_bucket(label: str) -> bool:
    return label.strip().lower() == "current"


def _aging_chart(breakdown: dict, report_name: str) -> bytes:
    """Horizontal stacked bars — outstanding balance per party, segmented by age bucket."""
    rows         = breakdown["rows"]
    bucket_order = breakdown["bucket_order"]
    parties      = [r["party"] for r in rows]

    fig, ax = plt.subplots(figsize=(9, max(3, len(parties) * 0.5)), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    y_pos = list(range(len(parties)))
    lefts = [0.0] * len(parties)
    overdue_i = 0
    for b in bucket_order:
        vals  = [r["buckets"].get(b, 0.0) for r in rows]
        color = _CURRENT_COLOR if _is_current_bucket(b) else \
            _AGING_BUCKET_COLORS[overdue_i % len(_AGING_BUCKET_COLORS)]
        if not _is_current_bucket(b):
            overdue_i += 1
        ax.barh(y_pos, vals, left=lefts, label=b, color=color, height=0.6, zorder=3)
        lefts = [l + v for l, v in zip(lefts, vals)]

    ax.set_yticks(y_pos)
    ax.set_yticklabels(parties, fontsize=8)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.tick_params(axis="x", labelsize=8)
    ax.set_xlabel("Outstanding Balance", fontsize=9)
    ax.set_title(f"{report_name} by Age", fontsize=11, fontweight="bold", pad=12)
    ax.legend(fontsize=7.5, framealpha=0.85, ncol=min(len(bucket_order), 5), loc="lower right")
    ax.grid(axis="x", linestyle="--", alpha=0.5, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_aging_report(
    summary_df,
    detail_df,
    report_config: dict,
    trailing_daily: float | None = None,
    detail_available: bool = True,
) -> tuple[str, bytes]:
    """
    Render an aging report (A/R or A/P). `summary_df`/`detail_df` come from
    aging_fetcher.build_aging_dataframe. `trailing_daily` (daily revenue for A/R,
    daily spend for A/P) drives DSO/DPO when available, else it shows '—'.
    `detail_available=False` (the per-item detail endpoint failed) renders the
    summary-only view with a note instead of the oldest-items worklist.
    Returns (html, chart_png).
    """
    from aging_analytics import (
        aging_summary, party_breakdown, oldest_items, days_outstanding,
    )

    side = report_config.get("side", "receivable")
    meta = _SIDE_META.get(side, _SIDE_META["receivable"])

    summary   = aging_summary(summary_df)
    breakdown = party_breakdown(summary_df, top_n=8)
    items     = oldest_items(detail_df, report_config.get("top_items", 10))
    dxo       = days_outstanding(summary["total"], trailing_daily)

    chart_png = _aging_chart(breakdown, report_config["name"])

    breakdown_rows = []
    for r in breakdown["rows"]:
        cells = []
        for b in breakdown["bucket_order"]:
            amt = r["buckets"].get(b, 0.0)
            cells.append({
                "amount":  _fmt_currency(amt) if amt else "—",
                "overdue": (not _is_current_bucket(b)) and amt > 0,
            })
        breakdown_rows.append({
            "party": r["party"],
            "cells": cells,
            "total": _fmt_currency(r["total"]),
            "share": f"{r['share'] * 100:.1f}%",
            "bar_w": round(r["share"] * 100, 1),
        })

    oldest_rows = [
        {
            "party":        it["party"],
            "doc_num":      it["doc_num"],
            "due_date":     it["due_date"],
            "open_balance": _fmt_currency(it["open_balance"]),
            "days_overdue": it["days_overdue"],
            "is_overdue":   it["days_overdue"] is not None and it["days_overdue"] > 0,
        }
        for it in items
    ]

    context = {
        "report_name":   report_config["name"],
        "report_date":   datetime.now().strftime("%B %d, %Y"),
        "party_label":   meta["party_label"],
        "owed_label":    meta["owed"],
        "flow_label":    meta["flow_label"],
        "flow_name":     meta["flow_name"],
        "empty_label":   meta["empty"],
        "total":         _fmt_currency(summary["total"]),
        "overdue":       _fmt_currency(summary["overdue"]),
        "current":       _fmt_currency(summary["current"]),
        "overdue_pct":   f"{summary['overdue_pct'] * 100:.0f}%",
        "dxo":           f"{dxo:.0f} days" if dxo is not None else "—",
        "bucket_labels": breakdown["bucket_order"],
        "breakdown_rows": breakdown_rows,
        "oldest_rows":   oldest_rows,
        "detail_available": detail_available,
    }

    template = _jinja_env().get_template("aging_report.html")
    return template.render(**context), chart_png


# ---------------------------------------------------------------------------
# Cash Outlook — position snapshot composed from A/R, A/P, and cash on hand
# ---------------------------------------------------------------------------

def _outlook_chart(outlook: dict) -> bytes:
    """Simple position bars: cash on hand, + receivables, − payables, = net."""
    cash = outlook["cash"] or 0.0
    net  = outlook["net_position"]
    if net is None:
        net = cash + outlook["ar_total"] - outlook["ap_total"]

    labels = ["Cash on hand", "+ Receivables", "− Payables", "= Net position"]
    values = [cash, outlook["ar_total"], -outlook["ap_total"], net]
    colors = ["#2563eb", "#16a34a", "#dc2626", "#7c3aed"]

    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=150)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8f9fa")

    y_pos = list(range(len(labels)))
    ax.barh(y_pos, values, color=colors, alpha=0.88, height=0.6, zorder=3)
    ax.axvline(0, color="#475569", linewidth=1.0)
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(lambda x, _: f"${x:,.0f}" if x >= 0 else f"-${abs(x):,.0f}")
    )
    ax.tick_params(axis="x", labelsize=8)
    ax.set_title("Cash Position", fontsize=11, fontweight="bold", pad=12)
    ax.grid(axis="x", linestyle="--", alpha=0.5, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_cash_outlook(outlook: dict, report_config: dict) -> tuple[str, bytes]:
    """Render the Cash Outlook position snapshot. `outlook` from cash_outlook.build_outlook."""
    chart_png = _outlook_chart(outlook)

    context = {
        "report_name":  report_config["name"],
        "report_date":  datetime.now().strftime("%B %d, %Y"),
        "cash":         _fmt_currency(outlook["cash"]) if outlook["cash"] is not None else "—",
        "ar_total":     _fmt_currency(outlook["ar_total"]),
        "ar_current":   _fmt_currency(outlook["ar_current"]),
        "ar_overdue":   _fmt_currency(outlook["ar_overdue"]),
        "ap_total":     _fmt_currency(outlook["ap_total"]),
        "ap_current":   _fmt_currency(outlook["ap_current"]),
        "ap_overdue":   _fmt_currency(outlook["ap_overdue"]),
        "net_position": _fmt_currency(outlook["net_position"]) if outlook["net_position"] is not None else "—",
        "net_negative": outlook["net_position"] is not None and outlook["net_position"] < 0,
    }

    template = _jinja_env().get_template("cash_outlook.html")
    return template.render(**context), chart_png


# ---------------------------------------------------------------------------
# CLI — writes preview_report.html for browser inspection
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import yaml
    from fetcher import fetch_raw_all, build_dataframe
    from analytics import run_all

    with open("reports/cogs.yaml") as f:
        cfg = yaml.safe_load(f)

    print("Fetching data…")
    raw = fetch_raw_all()
    df  = build_dataframe(raw, cfg)
    mom, yoy, flags = run_all(df, cfg["metric"])

    html, chart_png = build_report(mom, yoy, flags, cfg)
    preview = html.replace(
        'src="cid:monthly_chart"',
        f'src="data:image/png;base64,{base64.b64encode(chart_png).decode()}"',
    )
    out_path = os.path.join(os.path.dirname(__file__), "preview_report.html")
    with open(out_path, "w") as f:
        f.write(preview)
    print(f"Preview written to {out_path}")
