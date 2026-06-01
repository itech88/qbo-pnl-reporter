"""Unit tests for analytics.py — synthetic DataFrames, no API calls."""

import pandas as pd
import pytest

from analytics import mom_analysis, yoy_analysis, flag_anomalies, run_all


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
