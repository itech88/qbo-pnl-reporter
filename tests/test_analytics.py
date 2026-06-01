"""Unit tests for analytics.py — synthetic DataFrames, no API calls."""

import pandas as pd
import pytest

from analytics import mom_analysis, yoy_analysis, flag_anomalies


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_df(rows):
    return pd.DataFrame(rows, columns=["year", "month", "income", "cogs"])


@pytest.fixture
def three_year_df():
    """Realistic three-year optometry dataset with consistent ~30% COGS."""
    rows = []
    income = {1: 42000, 2: 45000, 3: 41000, 4: 47000, 5: 45000, 6: 48500,
              7: 44000, 8: 46000, 9: 43000, 10: 48000, 11: 50000, 12: 52000}
    for year in [2024, 2025, 2026]:
        for month, inc in income.items():
            rows.append({"year": year, "month": month,
                          "income": inc, "cogs": round(inc * 0.30, 2)})
    return _make_df(rows)


@pytest.fixture
def spike_df(three_year_df):
    """Same as three_year_df but May 2026 has a COGS spike to 50%."""
    df = three_year_df.copy()
    mask = (df["year"] == 2026) & (df["month"] == 5)
    df.loc[mask, "cogs"] = df.loc[mask, "income"] * 0.50
    return df


@pytest.fixture
def no_history_df():
    """Only current year — no prior years to compare against."""
    rows = [{"year": 2026, "month": m, "income": 45000, "cogs": 13500}
            for m in range(1, 13)]
    return _make_df(rows)


# ---------------------------------------------------------------------------
# mom_analysis
# ---------------------------------------------------------------------------

class TestMomAnalysis:
    def test_returns_only_current_year(self, three_year_df):
        result = mom_analysis(three_year_df)
        assert result["month"].tolist() == list(range(1, 13))

    def test_cogs_pct_calculated_correctly(self, three_year_df):
        result = mom_analysis(three_year_df)
        # All months should be ~30%
        assert all(abs(result["cogs_pct"] - 0.30) < 0.001)

    def test_zero_income_gives_nan_cogs_pct(self):
        df = _make_df([{"year": 2026, "month": 1, "income": 0, "cogs": 0}])
        result = mom_analysis(df)
        assert pd.isna(result.iloc[0]["cogs_pct"])

    def test_month_name_column_present(self, three_year_df):
        result = mom_analysis(three_year_df)
        assert "month_name" in result.columns
        assert result.loc[result["month"] == 1, "month_name"].iloc[0] == "Jan"
        assert result.loc[result["month"] == 12, "month_name"].iloc[0] == "Dec"

    def test_required_columns_present(self, three_year_df):
        result = mom_analysis(three_year_df)
        for col in ["month", "month_name", "income", "cogs", "cogs_pct"]:
            assert col in result.columns


# ---------------------------------------------------------------------------
# flag_anomalies
# ---------------------------------------------------------------------------

class TestFlagAnomalies:
    def test_no_flags_when_stable(self, three_year_df):
        result = flag_anomalies(three_year_df, threshold=0.05)
        assert result.empty

    def test_flags_high_spike(self, spike_df):
        result = flag_anomalies(spike_df, threshold=0.05)
        assert not result.empty
        flagged = result[result["month"] == 5]
        assert len(flagged) == 1
        assert flagged.iloc[0]["direction"] == "HIGH"

    def test_flags_low_dip(self):
        rows = []
        for year in [2024, 2025]:
            for month in range(1, 13):
                rows.append({"year": year, "month": month, "income": 45000, "cogs": 13500})
        # 2026 May dips to 5% COGS vs 30% average
        for month in range(1, 13):
            cogs = 2250 if month == 5 else 13500
            rows.append({"year": 2026, "month": month, "income": 45000, "cogs": cogs})
        df = _make_df(rows)
        result = flag_anomalies(df, threshold=0.05)
        flagged = result[result["month"] == 5]
        assert flagged.iloc[0]["direction"] == "LOW"

    def test_deviation_value_is_correct(self, spike_df):
        result = flag_anomalies(spike_df, threshold=0.05)
        flagged = result[result["month"] == 5].iloc[0]
        # spike is 50%, history is 30%, deviation ≈ +0.20
        assert abs(flagged["deviation"] - 0.20) < 0.01

    def test_no_flags_without_prior_years(self, no_history_df):
        result = flag_anomalies(no_history_df, threshold=0.05)
        assert result.empty

    def test_threshold_boundary_not_flagged(self, spike_df):
        # spike is ~20pp over — threshold of 0.25 should not flag it
        result = flag_anomalies(spike_df, threshold=0.25)
        assert result.empty

    def test_output_columns(self, spike_df):
        result = flag_anomalies(spike_df, threshold=0.05)
        for col in ["month", "month_name", "cogs_pct_current", "avg_3yr", "deviation", "direction"]:
            assert col in result.columns


# ---------------------------------------------------------------------------
# yoy_analysis
# ---------------------------------------------------------------------------

class TestYoyAnalysis:
    def test_has_column_per_year(self, three_year_df):
        result = yoy_analysis(three_year_df)
        for yr in [2024, 2025, 2026]:
            assert f"cogs_pct_{yr}" in result.columns

    def test_twelve_rows(self, three_year_df):
        result = yoy_analysis(three_year_df)
        assert len(result) == 12

    def test_month_name_column(self, three_year_df):
        result = yoy_analysis(three_year_df)
        assert result.loc[result["month"] == 6, "month_name"].iloc[0] == "Jun"

    def test_values_match_mom(self, three_year_df):
        yoy = yoy_analysis(three_year_df)
        jan_2026 = yoy.loc[yoy["month"] == 1, "cogs_pct_2026"].iloc[0]
        assert abs(jan_2026 - 0.30) < 0.001
