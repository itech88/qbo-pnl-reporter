"""Unit tests for guardrails.py — pure reconciliation/sanity, no network or state."""

import pandas as pd
import pytest

from guardrails import (
    report_sanity,
    reconcile_identities,
    reconcile_vendor,
    reconcile_aging,
    reconcile_balance_sheet,
    INCOME, COGS, GROSS_PROFIT, NOI, OPEX,
)


def _row(income, value, year=2026, month=5):
    return pd.DataFrame([{"year": year, "month": month, "income": income, "value": value}])


# ---------------------------------------------------------------------------
# report_sanity
# ---------------------------------------------------------------------------

class TestReportSanity:
    def test_healthy_passes(self):
        assert report_sanity(_row(50000, 15000), 2026, 5, "ratio") == []

    def test_missing_month_flagged(self):
        assert report_sanity(_row(50000, 15000, month=4), 2026, 5, "ratio")

    def test_non_finite_value_flagged(self):
        reasons = report_sanity(_row(50000, float("nan")), 2026, 5, "ratio")
        assert any("non-finite" in r for r in reasons)

    def test_absurd_ratio_flagged(self):
        # value = 4x income → 400%, outside the -200%..200% band
        reasons = report_sanity(_row(50000, 200000), 2026, 5, "ratio")
        assert any("sanity band" in r for r in reasons)

    def test_negative_noi_within_band_passes(self):
        # a normal net operating loss (-20% of income) is real data, not an error
        assert report_sanity(_row(50000, -10000), 2026, 5, "both") == []

    def test_zero_income_skips_ratio_check(self):
        # no division-by-zero; income==0 just can't be ratio-checked
        assert report_sanity(_row(0, 0), 2026, 5, "ratio") == []

    def test_partial_month_skips_ratio_band(self):
        # an absurd ratio is legitimate month-to-date when partial=True
        assert report_sanity(_row(50000, 200000), 2026, 5, "ratio", partial=True) == []

    def test_partial_still_catches_non_finite(self):
        # structural checks remain even for a partial month
        reasons = report_sanity(_row(50000, float("inf")), 2026, 5, "ratio", partial=True)
        assert any("non-finite" in r for r in reasons)


# ---------------------------------------------------------------------------
# reconcile_identities
# ---------------------------------------------------------------------------

class TestReconcileIdentities:
    def _totals(self, overrides=None):
        # NOTE: build via a dict literal so the *constants* are used as keys —
        # passing them as **kwargs would take the literal identifier instead.
        base = {INCOME: 50000, COGS: 15000, GROSS_PROFIT: 35000,
                OPEX: 20000, NOI: 15000}
        if overrides:
            base.update(overrides)
        return base

    def test_balanced_books_no_findings(self):
        assert reconcile_identities(self._totals()) == []

    def test_income_cogs_gp_mismatch(self):
        # perturb COGS — it appears only in the first identity, isolating it
        findings = reconcile_identities(self._totals({COGS: 99999}))
        assert len(findings) == 1
        reason, implicated = findings[0]
        assert set(implicated) == {COGS, GROSS_PROFIT}
        assert "Gross Profit" in reason

    def test_gp_opex_noi_mismatch(self):
        # perturb NOI — it appears only in the second identity
        findings = reconcile_identities(self._totals({NOI: 0}))
        assert len(findings) == 1
        _, implicated = findings[0]
        assert set(implicated) == {OPEX, NOI}

    def test_partial_totals_skip_uncomputable_identities(self):
        # only income + COGS present → nothing can be cross-footed
        assert reconcile_identities({INCOME: 50000, COGS: 15000}) == []

    def test_within_tolerance_passes(self):
        # a 50-cent rounding gap is under the $1 default tolerance (both identities)
        assert reconcile_identities(self._totals({GROSS_PROFIT: 35000.50})) == []


# ---------------------------------------------------------------------------
# reconcile_vendor
# ---------------------------------------------------------------------------

class TestReconcileVendor:
    def test_ties_out(self):
        assert reconcile_vendor(15000.0, 15000.0) == []

    def test_mismatch_flagged(self):
        reasons = reconcile_vendor(15000.0, 1000.0)
        assert reasons and "off by" in reasons[0]

    def test_one_percent_tolerance(self):
        # $100 gap on $15k COGS is within the 1% tolerance
        assert reconcile_vendor(15000.0, 15100.0) == []

    def test_missing_inputs_skip(self):
        assert reconcile_vendor(None, 15000.0) == []
        assert reconcile_vendor(15000.0, None) == []


# ---------------------------------------------------------------------------
# reconcile_aging
# ---------------------------------------------------------------------------

class TestReconcileAging:
    def test_ties_out(self):
        assert reconcile_aging(3000.0, 3000.0, label="A/R Aging") == []

    def test_mismatch_flagged(self):
        reasons = reconcile_aging(3000.0, 2500.0, label="A/R Aging")
        assert reasons and "off by" in reasons[0]
        assert "A/R Aging" in reasons[0]

    def test_one_percent_tolerance(self):
        # $20 gap on $3000 is within the 1% tolerance
        assert reconcile_aging(3000.0, 3020.0) == []

    def test_missing_inputs_skip(self):
        assert reconcile_aging(None, 3000.0) == []
        assert reconcile_aging(3000.0, None) == []


# ---------------------------------------------------------------------------
# reconcile_balance_sheet
# ---------------------------------------------------------------------------

class TestReconcileBalanceSheet:
    def test_ties_out(self):
        assert reconcile_balance_sheet(3000.0, 3000.0, label="A/R") == []

    def test_mismatch_flagged(self):
        reasons = reconcile_balance_sheet(3000.0, 1800.0, label="A/R")
        assert reasons and "Balance Sheet A/R" in reasons[0]

    def test_missing_inputs_skip(self):
        assert reconcile_balance_sheet(None, 3000.0) == []
        assert reconcile_balance_sheet(3000.0, None) == []
