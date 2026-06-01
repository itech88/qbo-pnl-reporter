"""
Scheduler — main entry point.

Fetches raw P&L data once, then loops through every report config in reports/
and sends a separate email per config. Called by cron or GitHub Actions on the
1st and 16th of each month.

Usage:
    python scheduler.py              # runs only on the 1st or 16th
    python scheduler.py --force      # runs regardless of date
    python scheduler.py --dry-run    # full pipeline, no emails sent
"""

import argparse
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
_TRIGGER_DAYS = {1, 16}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_report_configs() -> list[dict]:
    paths = sorted(glob.glob(os.path.join(_REPORTS_DIR, "*.yaml")))
    if not paths:
        raise RuntimeError(f"No report configs found in {_REPORTS_DIR}")
    configs = []
    for path in paths:
        with open(path) as f:
            configs.append(yaml.safe_load(f))
    return configs


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QBO monthly report scheduler")
    p.add_argument("--force",   action="store_true", help="Run regardless of today's date")
    p.add_argument("--dry-run", action="store_true", help="Build reports but skip sending")
    return p.parse_args()


def run(force: bool = False, dry_run: bool = False) -> None:
    today = datetime.now()

    if not force and today.day not in _TRIGGER_DAYS:
        log.info(
            "Skipping — today is the %d%s, not the 1st or 16th. Use --force to override.",
            today.day, _ordinal(today.day),
        )
        return

    run_id    = str(uuid.uuid4())[:8]
    started_at = time.monotonic()
    env       = os.getenv("QBO_ENVIRONMENT", "sandbox")

    log.info("━" * 60)
    log.info(
        "EXECUTION START — run_id=%s env=%s dry_run=%s pid=%d",
        run_id, env, dry_run, os.getpid(),
    )
    log.info("━" * 60)

    # ── Step 1: Load configs ─────────────────────────────────────────────────
    configs = load_report_configs()
    log.info("[%s] Loaded %d report config(s): %s",
             run_id, len(configs), ", ".join(c["name"] for c in configs))

    # ── Step 2: Fetch raw data once ──────────────────────────────────────────
    log.info("[%s] Step 1 — Fetching raw P&L data (6 API calls for 3 years)…", run_id)
    from fetcher import fetch_raw_all, build_dataframe
    raw_by_year = fetch_raw_all()
    log.info("[%s] Raw data fetched for years: %s", run_id, sorted(raw_by_year.keys()))

    # ── Step 3: Run each report ──────────────────────────────────────────────
    from analytics import run_all
    from report import build_report
    from mailer import send_report

    total_anomalies = 0

    for i, cfg in enumerate(configs, 1):
        name   = cfg["name"]
        metric = cfg.get("metric", "ratio")
        log.info("[%s] ── Report %d/%d: %s (metric=%s) ──", run_id, i, len(configs), name, metric)

        df = build_dataframe(raw_by_year, cfg)
        active_rows = len(df[df["income"] > 0])
        log.info("[%s]   DataFrame: %d total rows, %d with income data", run_id, len(df), active_rows)

        mom_df, yoy_df, flags_df = run_all(df, metric)
        log.info("[%s]   Anomalies flagged: %d", run_id, len(flags_df))
        total_anomalies += len(flags_df)

        html, chart_png = build_report(mom_df, yoy_df, flags_df, cfg)
        log.info("[%s]   Report rendered: %d chars HTML, %d bytes chart", run_id, len(html), len(chart_png))

        month_name = today.strftime("%B")
        subject = cfg["subject"].format(month=month_name, year=today.year)

        if dry_run:
            slug = name.lower().replace(" ", "_").replace("/", "_")
            out = os.path.join(os.path.dirname(__file__), f"preview_{slug}.html")
            preview = html.replace(
                'src="cid:monthly_chart"',
                f'src="data:image/png;base64,{__import__("base64").b64encode(chart_png).decode()}"',
            )
            with open(out, "w") as f:
                f.write(preview)
            log.info("[%s]   [DRY RUN] Saved to %s", run_id, out)
        else:
            send_report(html, chart_png, subject=subject, email_to=cfg["email_to"])
            log.info("[%s]   Email sent → %s", run_id, cfg["email_to"])

    elapsed = time.monotonic() - started_at
    log.info("━" * 60)
    log.info(
        "EXECUTION COMPLETE — run_id=%s reports=%d anomalies=%d duration=%.2fs email_sent=%s",
        run_id, len(configs), total_anomalies, elapsed, not dry_run,
    )
    log.info("━" * 60)


def _ordinal(n: int) -> str:
    return "th" if 11 <= n <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


if __name__ == "__main__":
    args = _parse_args()
    try:
        run(force=args.force, dry_run=args.dry_run)
    except Exception:
        log.error("FATAL ERROR — pipeline aborted:\n%s", traceback.format_exc())
        sys.exit(1)
