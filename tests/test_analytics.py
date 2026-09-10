"""Unit tests for analytics.py — synthetic DataFrames, no API calls."""

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest

from analytics import (
    mom_analysis,
    yoy_analysis,
    flag_anomalies,
    run_all,
    current_month_stats,
)


class _Jan1Datetime(datetime):
    """datetime whose .now() is pinned to Jan 1 (a fresh calendar year), while
    normal construction still works — used to prove the reporting year follows the
    data, not the wall clock."""
    @classmethod
    def now(cls, tz=None):
        return datetime(_CUR_YEAR, 1, 1, 9, 0, 0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_df(rows):
    return pd.DataFrame(rows, columns=["year", "month", "income", "value"])


@pytest.fixture
def three_year_df():
    """Realistic three-year dataset with consistent ~30% value/income ratio."""
    rows = []
    income = {1: 42000, 2: 45000, 3: 41000, 4: 47000, 5: 45000, 6: 48500,
              7: 44000, 8: 46000, 9: 43000, 10: 48000, 11: 50000, 12: 52000}
    for year in [2024, 2025, 2026]:
        for month, inc in income.items():
            rows.append({"year": year, "month": month,
                         "income": inc, "value": round(inc * 0.30, 2)})
    return _make_df(rows)


@pytest.fixture
def spike_df(three_year_df):
    """May 2026 has a value spike to 50% of income."""
    df = three_year_df.copy()
    mask = (df["year"] == 2026) & (df["month"] == 5)
    df.loc[mask, "value"] = df.loc[mask, "income"] * 0.50
    return df


@pytest.fixture
def no_history_df():
    rows = [{"year": 2026, "month": m, "income": 45000, "value": 13500}
            for m in range(1, 13)]
    return _make_df(rows)


@pytest.fixture
def negative_value_df():
    """Net Operating Income scenario — some months negative."""
    rows = []
    for year in [2024, 2025, 2026]:
        for month in range(1, 13):
            value = -5000 if month in (1, 2) else 8000
            rows.append({"year": year, "month": month, "income": 45000, "value": value})
    return _make_df(rows)


# ---------------------------------------------------------------------------
# mom_analysis
# ---------------------------------------------------------------------------

class TestMomAnalysis:
    def test_returns_twelve_rows(self, three_year_df):
        result = mom_analysis(three_year_df)
        assert len(result) == 12

    def test_value_pct_calculated_correctly(self, three_year_df):
        result = mom_analysis(three_year_df)
        assert all(abs(result["value_pct"] - 0.30) < 0.001)

    def test_zero_income_gives_nan(self):
        df = _make_df([{"year": 2026, "month": 1, "income": 0, "value": 0}])
        result = mom_analysis(df)
        assert pd.isna(result.iloc[0]["value_pct"])

    def test_negative_value_pct_preserved(self, negative_value_df):
        result = mom_analysis(negative_value_df)
        jan = result[result["month"] == 1].iloc[0]
        assert jan["value_pct"] < 0

    def test_required_columns(self, three_year_df):
        result = mom_analysis(three_year_df)
        for col in ["month", "month_name", "income", "value", "value_pct"]:
            assert col in result.columns

    def test_month_name_jan(self, three_year_df):
        result = mom_analysis(three_year_df)
        assert result.loc[result["month"] == 1, "month_name"].iloc[0] == "Jan"


# ---------------------------------------------------------------------------
# flag_anomalies — ratio metric
# ---------------------------------------------------------------------------

class TestFlagAnomaliesRatio:
    def test_no_flags_when_stable(self, three_year_df):
        result = flag_anomalies(three_year_df, metric="ratio", threshold=0.05)
        assert result.empty

    def test_flags_high_spike(self, spike_df):
        result = flag_anomalies(spike_df, metric="ratio", threshold=0.05)
        assert not result.empty
        assert result[result["month"] == 5].iloc[0]["direction"] == "HIGH"

    def test_deviation_correct(self, spike_df):
        result = flag_anomalies(spike_df, metric="ratio", threshold=0.05)
        flagged = result[result["month"] == 5].iloc[0]
        assert abs(flagged["deviation"] - 0.20) < 0.01

    def test_no_flags_without_history(self, no_history_df):
        result = flag_anomalies(no_history_df, metric="ratio", threshold=0.05)
        assert result.empty

    def test_output_columns(self, spike_df):
        result = flag_anomalies(spike_df, metric="ratio", threshold=0.05)
        for col in ["month", "month_name", "value_current", "pct_current",
                    "avg_3yr", "deviation", "direction"]:
            assert col in result.columns


# ---------------------------------------------------------------------------
# flag_anomalies — absolute metric
# ---------------------------------------------------------------------------

class TestFlagAnomaliesAbsolute:
    def test_no_flags_when_stable(self, three_year_df):
        result = flag_anomalies(three_year_df, metric="absolute", threshold=0.20)
        assert result.empty

    def test_flags_on_relative_deviation(self):
        rows = []
        for year in [2024, 2025]:
            for month in range(1, 13):
                rows.append({"year": year, "month": month, "income": 45000, "value": 1000})
        # 2026 May is 50% above average (1500 vs 1000)
        for month in range(1, 13):
            value = 1500 if month == 5 else 1000
            rows.append({"year": 2026, "month": month, "income": 45000, "value": value})
        df = _make_df(rows)
        result = flag_anomalies(df, metric="absolute", threshold=0.20)
        assert not result.empty
        assert result[result["month"] == 5].iloc[0]["direction"] == "HIGH"


# ---------------------------------------------------------------------------
# yoy_analysis
# ---------------------------------------------------------------------------

class TestYoyAnalysis:
    def test_has_column_per_year(self, three_year_df):
        result = yoy_analysis(three_year_df)
        for yr in [2024, 2025, 2026]:
            assert f"value_pct_{yr}" in result.columns
            assert f"value_{yr}"     in result.columns

    def test_twelve_rows(self, three_year_df):
        assert len(yoy_analysis(three_year_df)) == 12

    def test_negative_values_preserved(self, negative_value_df):
        result = yoy_analysis(negative_value_df)
        jan_pct = result.loc[result["month"] == 1, "value_pct_2026"].iloc[0]
        assert jan_pct < 0

    def test_values_consistent_with_mom(self, three_year_df):
        yoy = yoy_analysis(three_year_df)
        jan = yoy.loc[yoy["month"] == 1, "value_pct_2026"].iloc[0]
        assert abs(jan - 0.30) < 0.001


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------

class TestRunAll:
    def test_returns_three_dataframes(self, three_year_df):
        mom, yoy, flags = run_all(three_year_df, metric="ratio")
        assert isinstance(mom,   pd.DataFrame)
        assert isinstance(yoy,   pd.DataFrame)
        assert isinstance(flags, pd.DataFrame)

    def test_both_metric_passes_through(self, three_year_df):
        mom, yoy, flags = run_all(three_year_df, metric="both")
        assert "value_pct" in mom.columns


# ---------------------------------------------------------------------------
# current_month_stats — the scorecard's per-metric snapshot
# ---------------------------------------------------------------------------

# Built relative to "now" so the current-year selection works whenever this runs.
_CUR_YEAR = datetime.now().year
_PRIOR_YEARS = [_CUR_YEAR - 2, _CUR_YEAR - 1]


class TestCurrentMonthStats:
    def _df_through_month(self, last_month: int, ratio: float = 0.30):
        """Full prior-year history; current year has income only through last_month."""
        rows = [{"year": y, "month": m, "income": 45000, "value": 45000 * ratio}
                for y in _PRIOR_YEARS for m in range(1, 13)]
        for m in range(1, 13):
            inc = 45000 if m <= last_month else 0
            rows.append({"year": _CUR_YEAR, "month": m, "income": inc, "value": inc * ratio})
        return _make_df(rows)

    def test_picks_latest_month_with_income(self):
        # Income posted through May → the just-completed month is reported,
        # i.e. the prior-month fallback an owner wants on the 1st.
        stats = current_month_stats(self._df_through_month(5))
        assert stats is not None
        assert stats["report_month"] == 5
        assert stats["report_month_name"] == "May"

    def test_reports_prior_year_december_when_current_year_empty(self):
        # RB-1: the new calendar year has no income yet (Jan 1). Rather than
        # returning None and skipping the annual dashboard, report the prior
        # year's December — the just-completed close the owner wants.
        stats = current_month_stats(self._df_through_month(0))
        assert stats is not None
        assert stats["report_year"] == _CUR_YEAR - 1
        assert stats["report_month"] == 12
        assert stats["report_month_name"] == "Dec"

    def test_none_when_no_income_anywhere(self):
        # A genuinely empty pull (no income in any year) → nothing to report.
        empty = _make_df([{"year": y, "month": m, "income": 0, "value": 0}
                          for y in _PRIOR_YEARS + [_CUR_YEAR] for m in range(1, 13)])
        assert current_month_stats(empty) is None

    def test_deviation_near_zero_when_stable(self):
        stats = current_month_stats(self._df_through_month(5))
        assert abs(stats["avg_3yr"] - 0.30) < 0.001
        assert abs(stats["deviation"]) < 0.001

    def test_deviation_reflects_spike(self):
        df = self._df_through_month(5)
        mask = (df["year"] == _CUR_YEAR) & (df["month"] == 5)
        df.loc[mask, "value"] = 45000 * 0.50          # May jumps to 50% of income
        stats = current_month_stats(df, metric="ratio")
        assert stats["deviation"] > 0.15              # ~+20pp vs the 30% average

    def test_ratio_metric_reports_percentage(self):
        stats = current_month_stats(self._df_through_month(5), metric="ratio")
        assert stats["use_pct"] is True
        assert abs(stats["primary"] - 0.30) < 0.001   # primary is the ratio

    def test_absolute_metric_reports_dollars(self):
        stats = current_month_stats(self._df_through_month(5), metric="absolute")
        assert stats["use_pct"] is False
        assert abs(stats["primary"] - 45000 * 0.30) < 1   # primary is the dollar value


# ---------------------------------------------------------------------------
# RB-1 — year-boundary: the reporting year follows the data, not the clock
# ---------------------------------------------------------------------------

class TestReportingYearBoundary:
    """On January 1 the new calendar year has zero income rows. Keying on
    datetime.now().year rendered an empty 'current year' and skipped the December
    close — the year's most valuable send. The reporting year is now derived from
    the data, so December is reported even when the clock says Jan 1."""

    def _prior_year_only_df(self):
        # Prior year fully booked; the new calendar year has no rows at all yet.
        return _make_df([{"year": _CUR_YEAR - 1, "month": m, "income": 45000, "value": 13500}
                         for m in range(1, 13)])

    def test_mom_reports_prior_year_and_carries_year_column(self):
        mom = mom_analysis(self._prior_year_only_df())
        assert not mom.empty                       # not "No data for <new year> yet"
        assert set(mom["year"]) == {_CUR_YEAR - 1}  # the year column drives the label
        assert int(mom["month"].max()) == 12        # December present, not skipped

    def test_mom_reports_december_even_when_clock_says_jan_1(self):
        # Prove the clock is irrelevant: pin now() to Jan 1 of the new year.
        with patch("analytics.datetime", _Jan1Datetime):
            mom = mom_analysis(self._prior_year_only_df())
        assert int(mom["month"].max()) == 12

    def test_current_month_stats_reports_december_when_clock_says_jan_1(self):
        with patch("analytics.datetime", _Jan1Datetime):
            stats = current_month_stats(self._prior_year_only_df())
        assert stats is not None
        assert stats["report_year"] == _CUR_YEAR - 1
        assert stats["report_month_name"] == "Dec"
