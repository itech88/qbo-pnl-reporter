"""
Unit tests for report.py — the rendering layer (HTML + matplotlib charts).

No network. Charts render through matplotlib's headless Agg backend (set in
report.py). Inputs are produced the same way the pipeline produces them — via
analytics.run_all on synthetic DataFrames — so these exercise the real contract.
"""

from datetime import datetime

import pandas as pd
import pytest

from analytics import run_all
from report import (
    build_report,
    build_vendor_report,
    build_scorecard,
    _fmt_currency,
    _fmt_pct,
    _CURRENT_YEAR,
)

# PNG file signature — every chart must be a real PNG.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Build fixtures relative to the year report.py captured at import, so the
# "current year" bars always have data regardless of when the suite runs.
_YEARS = [_CURRENT_YEAR - 2, _CURRENT_YEAR - 1, _CURRENT_YEAR]


def _make_df(rows):
    return pd.DataFrame(rows, columns=["year", "month", "income", "value"])


def _assert_valid_png(png: bytes):
    assert isinstance(png, bytes) and png[:8] == _PNG_MAGIC and len(png) > 1000


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def stable_df():
    """Three years, steady ~30% value/income ratio."""
    income = {1: 42000, 2: 45000, 3: 41000, 4: 47000, 5: 45000, 6: 48500,
              7: 44000, 8: 46000, 9: 43000, 10: 48000, 11: 50000, 12: 52000}
    rows = [{"year": y, "month": m, "income": inc, "value": round(inc * 0.30, 2)}
            for y in _YEARS for m, inc in income.items()]
    return _make_df(rows)


@pytest.fixture
def spike_df(stable_df):
    """Current-year May spikes to 50% of income — should flag + colour red."""
    df = stable_df.copy()
    mask = (df["year"] == _CURRENT_YEAR) & (df["month"] == 5)
    df.loc[mask, "value"] = df.loc[mask, "income"] * 0.50
    return df


@pytest.fixture
def negative_df():
    """Net-operating-income style: some months negative."""
    rows = [{"year": y, "month": m, "income": 45000,
             "value": -5000 if m in (1, 2) else 8000}
            for y in _YEARS for m in range(1, 13)]
    return _make_df(rows)


@pytest.fixture
def current_year_only_df():
    """No prior-year history — chart must render with no prior-year lines."""
    rows = [{"year": _CURRENT_YEAR, "month": m, "income": 45000, "value": 13500}
            for m in range(1, 13)]
    return _make_df(rows)


def _vendor_df(n_vendors: int):
    """Long vendor frame (year, month, vendor, amount) for the current month."""
    rows = []
    for y in _YEARS:
        for i in range(n_vendors):
            rows.append({"year": y, "month": 5, "vendor": f"Vendor {i}",
                         "amount": 1000.0 * (i + 1)})
    return pd.DataFrame(rows, columns=["year", "month", "vendor", "amount"])


# ---------------------------------------------------------------------------
# build_report
# ---------------------------------------------------------------------------

class TestBuildReport:
    def test_ratio_report_renders_html_and_png(self, stable_df):
        mom, yoy, flags = run_all(stable_df, "ratio")
        cfg = {"name": "COGS", "metric": "ratio"}
        html, png = build_report(mom, yoy, flags, cfg)
        assert isinstance(html, str) and "COGS" in html
        _assert_valid_png(png)

    def test_both_metric_shows_income_and_pct(self, stable_df):
        mom, yoy, flags = run_all(stable_df, "both")
        html, png = build_report(mom, yoy, flags, {"name": "Gross Profit", "metric": "both"})
        assert "Gross Profit" in html
        _assert_valid_png(png)

    def test_negative_value_rendered_red(self, negative_df):
        mom, yoy, flags = run_all(negative_df, "both")
        html, _ = build_report(mom, yoy, flags, {"name": "Net Operating Income", "metric": "both"})
        # _mom_rows paints negative value_pct rows with the red hex
        assert "#dc2626" in html

    def test_anomaly_month_is_flagged(self, spike_df):
        mom, yoy, flags = run_all(spike_df, "ratio")
        assert not flags.empty  # sanity: the spike produced a flag
        html, _ = build_report(mom, yoy, flags, {"name": "COGS", "metric": "ratio"})
        assert "#dc2626" in html  # flagged-high month coloured red

    def test_missing_prior_years_still_renders(self, current_year_only_df):
        mom, yoy, flags = run_all(current_year_only_df, "ratio")
        html, png = build_report(mom, yoy, flags, {"name": "COGS", "metric": "ratio"})
        assert "COGS" in html
        _assert_valid_png(png)


# ---------------------------------------------------------------------------
# build_vendor_report
# ---------------------------------------------------------------------------

class TestBuildVendorReport:
    def test_renders_vendor_names_and_total(self):
        html, png = build_vendor_report(_vendor_df(3), {"name": "COGS by Vendor"})
        assert "COGS by Vendor" in html
        assert "Vendor 0" in html
        _assert_valid_png(png)

    def test_folds_excess_vendors_into_other(self):
        # 8 vendors > top_n=6 exercises the chart's "Other" folding branch
        # (folding is visual — in the PNG, not the HTML). The breakdown table
        # still lists every vendor individually; assert the report builds cleanly.
        html, png = build_vendor_report(_vendor_df(8), {"name": "COGS by Vendor"})
        assert "Vendor 7" in html  # full breakdown rendered, no vendor dropped
        _assert_valid_png(png)

    def test_empty_df_renders_dash_period(self):
        empty = pd.DataFrame(columns=["year", "month", "vendor", "amount"])
        html, png = build_vendor_report(empty, {"name": "COGS by Vendor"})
        assert "—" in html  # period_label falls back to em dash, no crash
        _assert_valid_png(png)


# ---------------------------------------------------------------------------
# build_scorecard
# ---------------------------------------------------------------------------

def _metric(name, deviation, *, use_pct=True, higher_is_better=False, avg_3yr=0.30):
    return {
        "name": name, "deviation": deviation, "avg_3yr": avg_3yr,
        "use_pct": use_pct, "higher_is_better": higher_is_better,
        "report_month_name": "May", "current_abs": 13500.0,
        "current_pct": 0.30, "primary": 0.30,
    }


class TestBuildScorecard:
    def test_status_thresholds(self):
        # default threshold 0.05 → ON TRACK <0.05, WATCH <0.10, ACTION >=0.10
        metrics = [
            _metric("On-track metric", 0.01),
            _metric("Watch metric",    0.07),
            _metric("Action metric",   0.15),
            _metric("No-data metric",  None),
        ]
        html, png = build_scorecard(metrics, {"name": "Monthly Business Dashboard"})
        assert metrics[0]["status"] == "ON TRACK"
        assert metrics[1]["status"] == "WATCH"
        assert metrics[2]["status"] == "ACTION"
        assert metrics[3]["status"] == "GREY"
        for word in ("ON TRACK", "WATCH", "ACTION"):
            assert word in html
        _assert_valid_png(png)

    def test_higher_is_better_flips_action_colour(self):
        # A big positive deviation is BAD for COGS (red) but GOOD for Gross Profit (blue)
        bad  = _metric("COGS",         0.15, higher_is_better=False)
        good = _metric("Gross Profit", 0.15, higher_is_better=True)
        build_scorecard([bad, good], {"name": "Monthly Business Dashboard"})
        assert bad["bar_color"]  == "#dc2626"   # red = bad
        assert good["bar_color"] == "#2563eb"   # blue = improvement, not red

    def test_deviation_formatted_by_metric_type(self):
        pct = _metric("COGS", 0.123, use_pct=True)
        absolute = _metric("Home Office Supplies", 250.0, use_pct=False, avg_3yr=1000.0)
        build_scorecard([pct, absolute], {"name": "Monthly Business Dashboard"})
        assert pct["deviation_fmt"].endswith("pp")     # percentage points
        assert "pp" not in absolute["deviation_fmt"]   # plain dollar delta


# ---------------------------------------------------------------------------
# Jinja value filters
# ---------------------------------------------------------------------------

class TestFilters:
    def test_currency_none_is_dash(self):
        assert _fmt_currency(None) == "—"
        assert _fmt_currency(float("nan")) == "—"

    def test_currency_negative_prefix(self):
        assert _fmt_currency(-1234.5) == "-$1,234.50"
        assert _fmt_currency(1234.5) == "$1,234.50"

    def test_pct_none_is_dash(self):
        assert _fmt_pct(None) == "—"
        assert _fmt_pct(float("nan")) == "—"

    def test_pct_formats_fraction(self):
        assert _fmt_pct(0.305) == "30.5%"
