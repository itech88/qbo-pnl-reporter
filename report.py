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
    env.filters["currency"] = _fmt_currency
    env.filters["pct"]      = _fmt_pct
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

    context = {
        "report_name":   report_config["name"],
        "current_year":  _CURRENT_YEAR,
        "report_date":   datetime.now().strftime("%B %d, %Y"),
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
