"""
Scheduler — main entry point.

Fetches raw P&L data once, then loops through eligible report configs.
Configs declare which days they trigger via the `trigger_days` field.
The scorecard (type: scorecard) collects data from all 8 data reports
and renders a single summary email.

Usage:
    python scheduler.py                                   # respects trigger_days
    python scheduler.py --force                           # runs all regardless of date
    python scheduler.py --force --report "COGS"          # single named report
    python scheduler.py --force --report "Monthly Business Dashboard"
    python scheduler.py --force --dry-run                # no emails sent
"""

import argparse
import base64
import glob
import os
import sys
import time
import traceback
import uuid
from datetime import datetime

import yaml

from logger import get_logger

log = get_logger(__name__)

_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_report_configs() -> list[dict]:
    paths = sorted(glob.glob(os.path.join(_REPORTS_DIR, "*.yaml")))
    if not paths:
        raise RuntimeError(f"No report configs found in {_REPORTS_DIR}")
    return [yaml.safe_load(open(p)) for p in paths]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QBO monthly report scheduler")
    p.add_argument("--force",   action="store_true", help="Run regardless of today's date")
    p.add_argument("--dry-run", action="store_true", help="Build reports but skip sending")
    p.add_argument("--report",  default=None,        help="Run only the named report config")
    return p.parse_args()


def _ordinal(n: int) -> str:
    return "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _preview_path(name: str) -> str:
    slug = name.lower().replace(" ", "_").replace("/", "_")
    return os.path.join(os.path.dirname(__file__), f"preview_{slug}.html")


def _deliver(html: str, chart_png: bytes, subject: str, cfg: dict,
             dry_run: bool, run_id: str) -> None:
    """Write a preview file (dry-run) or send the email (live)."""
    if dry_run:
        out = _preview_path(cfg["name"])
        preview = html.replace(
            'src="cid:monthly_chart"',
            f'src="data:image/png;base64,{base64.b64encode(chart_png).decode()}"',
        )
        with open(out, "w") as f:
            f.write(preview)
        log.info("[%s]   [DRY RUN] → %s", run_id, out)
    else:
        from mailer import send_report
        send_report(html, chart_png, subject=subject, email_to=cfg["email_to"])
        log.info("[%s]   Email sent → %s", run_id, cfg["email_to"])


def run(force: bool = False, dry_run: bool = False, report_filter: str | None = None) -> None:
    today      = datetime.now()
    run_id     = str(uuid.uuid4())[:8]
    started_at = time.monotonic()
    env        = os.getenv("QBO_ENVIRONMENT", "sandbox")

    log.info("━" * 60)
    log.info("EXECUTION START — run_id=%s env=%s dry_run=%s pid=%d",
             run_id, env, dry_run, os.getpid())
    log.info("━" * 60)

    all_configs    = load_report_configs()
    data_cfgs_all  = [c for c in all_configs if c.get("type") not in ("scorecard", "vendor_breakdown")]
    scorecard_cfgs = [c for c in all_configs if c.get("type") == "scorecard"]
    vendor_cfgs    = [c for c in all_configs if c.get("type") == "vendor_breakdown"]

    # ── Determine which configs to send ──────────────────────────────────────
    if report_filter:
        f = report_filter.lower()
        send_data   = [c for c in data_cfgs_all  if c["name"].lower() == f]
        send_score  = [c for c in scorecard_cfgs if c["name"].lower() == f]
        send_vendor = [c for c in vendor_cfgs    if c["name"].lower() == f]
        if not send_data and not send_score and not send_vendor:
            log.error("No config found matching --report %r", report_filter)
            sys.exit(1)
    elif force:
        send_data, send_score, send_vendor = data_cfgs_all, scorecard_cfgs, vendor_cfgs
    else:
        day = today.day
        send_data   = [c for c in data_cfgs_all  if day in c.get("trigger_days", [1, 16])]
        send_score  = [c for c in scorecard_cfgs if day in c.get("trigger_days", [1])]
        send_vendor = [c for c in vendor_cfgs    if day in c.get("trigger_days", [1, 16])]
        if not send_data and not send_score and not send_vendor:
            log.info("Skipping — today is the %d%s, no configs trigger on this day.",
                     day, _ordinal(day))
            return

    # When scorecard is being sent, collect analytics for all its includes
    # (even if those reports aren't being individually emailed this run)
    scorecard_includes = set()
    for sc in send_score:
        scorecard_includes.update(sc.get("includes", []))
    collect_cfgs = {c["name"]: c for c in data_cfgs_all
                    if c["name"] in {d["name"] for d in send_data} | scorecard_includes}

    log.info("[%s] Sending: %d data report(s) + %d scorecard(s)",
             run_id, len(send_data), len(send_score))

    # ── Step 1+2: Fetch P&L once and build analytics (skip if nothing needs it)
    from analytics import run_all, current_month_stats
    from report import build_report, build_scorecard

    collected: dict[str, tuple] = {}   # name -> (df, mom, yoy, flags)
    total_anomalies = 0

    if collect_cfgs:
        log.info("[%s] Fetching raw P&L data (6 API calls)…", run_id)
        from fetcher import fetch_raw_all, build_dataframe
        raw_by_year = fetch_raw_all()

        for cfg in collect_cfgs.values():
            name   = cfg["name"]
            metric = cfg.get("metric", "ratio")
            df     = build_dataframe(raw_by_year, cfg)
            mom, yoy, flags = run_all(df, metric)
            collected[name] = (df, mom, yoy, flags)
            total_anomalies += len(flags)
            log.info("[%s]   Built: %s — anomalies=%d", run_id, name, len(flags))

    failures = 0

    # ── Step 3: Send individual data reports ─────────────────────────────────
    for cfg in send_data:
        name = cfg["name"]
        try:
            metric = cfg.get("metric", "ratio")
            _, mom, yoy, flags = collected[name]
            log.info("[%s] ── %s (metric=%s) ──", run_id, name, metric)
            html, chart_png = build_report(mom, yoy, flags, cfg)
            log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, cfg, dry_run, run_id)
        except Exception:
            failures += 1
            log.error("[%s]   FAILED to process report '%s':\n%s",
                      run_id, name, traceback.format_exc())

    # ── Step 4: Vendor-breakdown reports (separate endpoint) ─────────────────
    if send_vendor:
        try:
            from vendor_fetcher import fetch_vendor_raw_all, build_vendor_dataframe
            from report import build_vendor_report
            log.info("[%s] Fetching vendor COGS detail (6 API calls)…", run_id)
            vendor_raw = fetch_vendor_raw_all()
            for cfg in send_vendor:
                name = cfg["name"]
                try:
                    log.info("[%s] ── %s (vendor breakdown) ──", run_id, name)
                    vdf = build_vendor_dataframe(vendor_raw, cfg)
                    log.info("[%s]   %d rows, %d unique vendors",
                             run_id, len(vdf), vdf["vendor"].nunique() if not vdf.empty else 0)
                    html, chart_png = build_vendor_report(vdf, cfg)
                    log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
                    subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
                    _deliver(html, chart_png, subject, cfg, dry_run, run_id)
                except Exception:
                    failures += 1
                    log.error("[%s]   FAILED vendor report '%s':\n%s",
                              run_id, name, traceback.format_exc())
        except Exception:
            failures += 1
            log.error("[%s]   FAILED to fetch vendor detail:\n%s", run_id, traceback.format_exc())

    # ── Step 5: Build and send scorecard(s) ──────────────────────────────────
    for sc_cfg in send_score:
        try:
            log.info("[%s] ── Scorecard: %s ──", run_id, sc_cfg["name"])
            threshold = float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))
            metrics_data = []
            for metric_name in sc_cfg.get("includes", []):
                if metric_name not in collected:
                    log.warning("[%s]   Scorecard: no data for '%s' — skipping", run_id, metric_name)
                    continue
                df, _, _, _ = collected[metric_name]
                src_cfg = collect_cfgs[metric_name]
                stats   = current_month_stats(df, src_cfg.get("metric", "ratio"), threshold)
                if stats is None:
                    log.warning("[%s]   Scorecard: no current-month data for '%s'", run_id, metric_name)
                    continue
                stats["name"]             = metric_name
                stats["higher_is_better"] = src_cfg.get("higher_is_better", False)
                metrics_data.append(stats)

            if not metrics_data:
                log.warning("[%s]   Scorecard has no metric data — skipping.", run_id)
                continue

            html, chart_png = build_scorecard(metrics_data, sc_cfg)
            log.info("[%s]   Scorecard HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = sc_cfg["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, sc_cfg, dry_run, run_id)
        except Exception:
            failures += 1
            log.error("[%s]   FAILED scorecard '%s':\n%s",
                      run_id, sc_cfg["name"], traceback.format_exc())

    elapsed = time.monotonic() - started_at
    total_reports = len(send_data) + len(send_vendor) + len(send_score)
    log.info("━" * 60)
    log.info("EXECUTION COMPLETE — run_id=%s reports=%d failures=%d anomalies=%d duration=%.2fs email_sent=%s",
             run_id, total_reports, failures, total_anomalies, elapsed, not dry_run)
    log.info("━" * 60)

    if failures:
        log.error("[%s] %d report(s) failed — see errors above.", run_id, failures)
        sys.exit(1)


if __name__ == "__main__":
    args = _parse_args()
    try:
        run(force=args.force, dry_run=args.dry_run, report_filter=args.report)
    except Exception:
        log.error("FATAL ERROR — pipeline aborted:\n%s", traceback.format_exc())
        sys.exit(1)
