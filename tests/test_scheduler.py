"""Unit tests for scheduler.py — PAT-expiry alerting + run() orchestration.

No network: every collaborator (fetch, analytics, render, mail) is mocked. Because
run() imports them locally (`from fetcher import ...`), the patch targets are the
source modules, not `scheduler.*`.
"""

import contextlib
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

import scheduler


def _expiry_in(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


class TestCheckPatExpiry:
    """Self-issued reminder before the un-rotatable GH_PAT lapses."""

    def test_noop_when_expiry_unknown(self):
        # github_pat_expiry returns None locally / on error → no email
        with patch("auth.github_pat_expiry", return_value=None), \
             patch("mailer.send_failure_alert") as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_not_called()

    def test_no_alert_when_healthy(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(90)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert") as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_not_called()

    def test_alerts_within_window(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(10)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()
            subject, body = alert.call_args.args[:2]
            assert "expires in" in subject.lower()
            assert "fine-grained" in body.lower()  # body carries the rotation steps

    def test_alerts_when_expired(self):
        with patch("auth.github_pat_expiry", return_value=_expiry_in(-2)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "30"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()
            subject, _ = alert.call_args.args[:2]
            assert "expired" in subject.lower()

    def test_custom_threshold_widens_window(self):
        # 40 days out is healthy at the default 30 but warns at 45
        with patch("auth.github_pat_expiry", return_value=_expiry_in(40)), \
             patch.dict("os.environ", {"GH_PAT_EXPIRY_WARN_DAYS": "45"}, clear=False), \
             patch("mailer.send_failure_alert", return_value=True) as alert:
            scheduler._check_pat_expiry("run123")
            alert.assert_called_once()

    def test_never_raises(self):
        # An exception anywhere in the check must not propagate into the run
        with patch("auth.github_pat_expiry", side_effect=RuntimeError("boom")), \
             patch("mailer.send_failure_alert"):
            scheduler._check_pat_expiry("run123")  # must not raise


# ---------------------------------------------------------------------------
# run() orchestration
# ---------------------------------------------------------------------------

_DATA_CFG = {
    "name": "COGS", "subject": "COGS — {month} {year}", "metric": "ratio",
    "higher_is_better": False, "email_to": "owner@example.com",
    "trigger_days": [1, 16],
    "extraction": {"type": "section_summary", "group": "COGS"},
}
_DATA_CFG2 = {
    "name": "Utilities", "subject": "Utilities — {month} {year}", "metric": "both",
    "higher_is_better": False, "email_to": "owner@example.com",
    "trigger_days": [1, 16],
    "extraction": {"type": "subsection_summary", "header_text": "Utilities"},
}
_SCORE_CFG = {
    "name": "Monthly Business Dashboard", "subject": "Dashboard — {month} {year}",
    "type": "scorecard", "trigger_days": [1], "email_to": "owner@example.com",
    "includes": ["COGS"],
}
_VENDOR_CFG = {
    "name": "COGS by Vendor", "subject": "Vendor — {month} {year}",
    "type": "vendor_breakdown", "trigger_days": [1, 16], "email_to": "owner@example.com",
}
_GP_CFG = {
    "name": "Gross Profit", "subject": "GP — {month} {year}", "metric": "both",
    "higher_is_better": True, "email_to": "owner@example.com", "trigger_days": [1, 16],
    "extraction": {"type": "section_summary", "group": "GrossProfit"},
}

# Section values that satisfy the P&L identities and the vendor tie, so a healthy
# run passes every guardrail. Income − COGS = Gross Profit; GP − OpEx = NOI; the
# vendor fixture below sums to COGS. The mocked "now" is 2026, so use that year.
_HEALTHY = {"COGS": 15000.0, "Gross Profit": 35000.0,
            "Net Operating Income": 15000.0, "Total Operating Expense Ratio": 20000.0}


# Default fixture "now" is 2026-06-16, so month 5 (May) is the last COMPLETE month —
# guardrail ratio bands apply. Tests that want a partial month set "now" to May.
def _healthy_df(cfg):
    val = _HEALTHY.get(cfg["name"], 2000.0)
    return pd.DataFrame([{"year": 2026, "month": 5, "income": 50000.0, "value": val}])


@pytest.fixture
def pipe():
    """Patch every external collaborator of run() and yield the mock handles.

    Default date is the 1st (so the scorecard triggers). Default config set is one
    data report + scorecard + vendor report. Tests tweak `pipe['load'].return_value`
    or `pipe['dt'].now.return_value` as needed.
    """
    # Vendor detail that ties to COGS (9000 + 6000 = 15000) so the vendor
    # reconciliation passes on a healthy run.
    vendor_df = pd.DataFrame([
        {"year": 2026, "month": 5, "vendor": "Luxottica",    "amount": 9000.0},
        {"year": 2026, "month": 5, "vendor": "CooperVision", "amount": 6000.0},
    ])
    with contextlib.ExitStack() as es:
        p = lambda *a, **k: es.enter_context(patch(*a, **k))
        m = {
            "load":   p("scheduler.load_report_configs",
                        return_value=[dict(_DATA_CFG), dict(_SCORE_CFG), dict(_VENDOR_CFG)]),
            "dt":     p("scheduler.datetime"),
            "fetch":  p("fetcher.fetch_raw_all", return_value={}),
            "bdf":    p("fetcher.build_dataframe", side_effect=lambda raw, cfg: _healthy_df(cfg)),
            "runall": p("analytics.run_all", return_value=(MagicMock(), MagicMock(), [])),
            "cms":    p("analytics.current_month_stats", side_effect=lambda *a, **k: {"primary": 0.3}),
            "report": p("report.build_report", return_value=("<html>", b"PNG")),
            "score":  p("report.build_scorecard", return_value=("<html>", b"PNG")),
            "vendor": p("report.build_vendor_report", return_value=("<html>", b"PNG")),
            "vfetch": p("vendor_fetcher.fetch_vendor_raw_all", return_value={}),
            "vbdf":   p("vendor_fetcher.build_vendor_dataframe", return_value=vendor_df),
            "send":   p("mailer.send_report"),
            "alert":  p("mailer.send_failure_alert", return_value=True),
        }
        p("auth.github_pat_expiry", return_value=None)  # PAT check no-ops
        m["dt"].now.return_value = datetime(2026, 6, 16, 12, 0, 0)  # the 16th; May complete
        yield m


class TestRunOrchestration:
    def test_skips_when_no_trigger_day(self, pipe):
        pipe["dt"].now.return_value = datetime(2026, 1, 7, 12, 0, 0)  # the 7th
        scheduler.run(force=False)
        pipe["fetch"].assert_not_called()
        pipe["send"].assert_not_called()

    def test_force_runs_all_report_types(self, pipe):
        pipe["dt"].now.return_value = datetime(2026, 1, 7, 12, 0, 0)  # non-trigger day
        scheduler.run(force=True)  # --force ignores the date
        pipe["fetch"].assert_called_once()           # P&L fetched exactly once
        pipe["report"].assert_called_once()          # the one data report
        pipe["vendor"].assert_called_once()          # vendor breakdown
        pipe["score"].assert_called_once()           # scorecard
        assert pipe["send"].call_count == 3          # data + vendor + scorecard delivered

    def test_fetch_happens_once_across_many_reports(self, pipe):
        pipe["load"].return_value = [dict(_DATA_CFG), dict(_DATA_CFG2),
                                     dict(_SCORE_CFG), dict(_VENDOR_CFG)]
        scheduler.run(force=True)
        pipe["fetch"].assert_called_once()
        assert pipe["report"].call_count == 2        # both data reports rendered

    def test_scorecard_included_on_first(self, pipe):
        pipe["dt"].now.return_value = datetime(2026, 1, 1, 12, 0, 0)
        scheduler.run(force=False)
        pipe["score"].assert_called_once()

    def test_scorecard_excluded_on_sixteenth(self, pipe):
        pipe["dt"].now.return_value = datetime(2026, 1, 16, 12, 0, 0)
        scheduler.run(force=False)
        pipe["score"].assert_not_called()            # scorecard is 1st-only
        pipe["report"].assert_called_once()          # but data reports still send

    def test_single_report_filter(self, pipe):
        scheduler.run(force=True, report_filter="COGS")
        pipe["report"].assert_called_once()
        pipe["vendor"].assert_not_called()
        pipe["score"].assert_not_called()
        assert pipe["send"].call_count == 1

    def test_unknown_report_filter_exits(self, pipe):
        with pytest.raises(SystemExit):
            scheduler.run(force=True, report_filter="Does Not Exist")
        pipe["fetch"].assert_not_called()

    def test_dry_run_writes_preview_and_sends_nothing(self, pipe, tmp_path):
        with patch("scheduler._preview_path",
                   side_effect=lambda name: str(tmp_path / f"preview_{name}.html")):
            scheduler.run(force=True, dry_run=True)
        pipe["send"].assert_not_called()             # no email in dry run
        written = list(tmp_path.glob("preview_*.html"))
        assert written                               # previews were written instead

    def test_partial_failure_exits_nonzero_and_alerts(self, pipe):
        pipe["report"].side_effect = RuntimeError("render boom")
        with pytest.raises(SystemExit):
            scheduler.run(force=True)                # live run, one report fails
        pipe["alert"].assert_called_once()           # operator gets the failure email
        # the failure email names the failed report
        body = pipe["alert"].call_args.args[1]
        assert "COGS" in body


def _bad_cogs_df(value, month=5):
    """build_dataframe side_effect: a poisoned value for COGS, healthy otherwise."""
    return lambda raw, cfg: pd.DataFrame(
        [{"year": 2026, "month": month, "income": 50000.0,
          "value": value if cfg["name"] == "COGS" else _HEALTHY.get(cfg["name"], 2000.0)}])


class TestGuardrails:
    """Pre-send reconciliation holds the affected report; the rest still send."""

    def test_absurd_ratio_holds_report_and_alerts(self, pipe):
        pipe["bdf"].side_effect = _bad_cogs_df(300000.0)   # COGS = 600% of income
        with pytest.raises(SystemExit):
            scheduler.run(force=True)
        pipe["report"].assert_not_called()                 # COGS never rendered/sent
        pipe["alert"].assert_called_once()
        assert "COGS" in pipe["alert"].call_args.args[1]

    def test_identity_mismatch_holds_both_sides(self, pipe):
        pipe["load"].return_value = [dict(_DATA_CFG), dict(_GP_CFG), dict(_SCORE_CFG)]
        pipe["bdf"].side_effect = lambda raw, cfg: pd.DataFrame(
            [{"year": 2026, "month": 1, "income": 50000.0,
              "value": {"COGS": 15000.0, "Gross Profit": 99999.0}.get(cfg["name"], 2000.0)}])
        with pytest.raises(SystemExit):
            scheduler.run(force=True)                       # 50000-15000 ≠ 99999
        body = pipe["alert"].call_args.args[1]
        assert "COGS" in body and "Gross Profit" in body

    def test_vendor_mismatch_holds_vendor_report(self, pipe):
        # COGS healthy (15000) but vendor detail sums to only 1000 → tie fails
        pipe["vbdf"].return_value = pd.DataFrame(
            [{"year": 2026, "month": 5, "vendor": "Luxottica", "amount": 1000.0}])
        with pytest.raises(SystemExit):
            scheduler.run(force=True)
        pipe["report"].assert_called_once()                # COGS still delivered
        pipe["vendor"].assert_not_called()                 # vendor report withheld
        assert "COGS by Vendor" in pipe["alert"].call_args.args[1]

    def test_dry_run_reports_holds_without_blocking(self, pipe, tmp_path):
        pipe["bdf"].side_effect = _bad_cogs_df(300000.0)
        with patch("scheduler._preview_path",
                   side_effect=lambda name: str(tmp_path / f"preview_{name}.html")):
            scheduler.run(force=True, dry_run=True)        # must NOT raise
        assert list(tmp_path.glob("preview_*.html"))       # previews still written
        pipe["alert"].assert_not_called()                  # no operator alert in dry run

    def test_heartbeat_ok_on_clean_run(self, pipe):
        with patch.dict("os.environ", {"HEARTBEAT_URL": "https://hc.example/abc"}, clear=False), \
             patch("requests.get") as hb:
            scheduler.run(force=True)
        hb.assert_called_once()
        assert hb.call_args.args[0] == "https://hc.example/abc"        # base = success

    def test_heartbeat_fail_on_hold(self, pipe):
        pipe["bdf"].side_effect = _bad_cogs_df(300000.0)
        with patch.dict("os.environ", {"HEARTBEAT_URL": "https://hc.example/abc"}, clear=False), \
             patch("requests.get") as hb, pytest.raises(SystemExit):
            scheduler.run(force=True)
        hb.assert_called_once()
        assert hb.call_args.args[0] == "https://hc.example/abc/fail"   # problem → /fail

    def test_partial_month_skips_ratio_band(self, pipe):
        # Reporting month IS the current month → an absurd ratio is legitimate
        # month-to-date, so it's delivered (labelled), not held.
        pipe["load"].return_value = [dict(_DATA_CFG), dict(_SCORE_CFG)]   # no vendor
        pipe["dt"].now.return_value = datetime(2026, 5, 16, 12, 0, 0)     # May = current
        pipe["bdf"].side_effect = _bad_cogs_df(300000.0, month=5)        # 600% but MTD
        scheduler.run(force=True)                                        # must NOT raise
        pipe["report"].assert_called_once()                             # COGS still delivered
