"""
Scheduler — main entry point.

Called by cron, GitHub Actions, or Railway on the 1st of each month.
Runs the full pipeline: fetch → analytics → render → email.

Usage:
    python scheduler.py              # runs only if today is the 1st
    python scheduler.py --force      # runs regardless of date
    python scheduler.py --dry-run    # full pipeline, no email sent
"""

import argparse
import os
import sys
import time
import traceback
import uuid
from datetime import datetime

from logger import get_logger

log = get_logger(__name__)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="QBO COGS monthly report scheduler")
    p.add_argument("--force",   action="store_true", help="Run regardless of today's date")
    p.add_argument("--dry-run", action="store_true", help="Build report but skip sending")
    return p.parse_args()


def run(force: bool = False, dry_run: bool = False) -> None:
    today = datetime.now()

    if not force and today.day != 1:
        log.info(
            "Skipping — today is the %d%s, not the 1st. Use --force to override.",
            today.day, _ordinal(today.day),
        )
        return

    run_id = str(uuid.uuid4())[:8]
    started_at = time.monotonic()
    env = os.getenv("QBO_ENVIRONMENT", "sandbox")

    log.info("━" * 60)
    log.info(
        "EXECUTION START — run_id=%s env=%s dry_run=%s pid=%d",
        run_id, env, dry_run, os.getpid(),
    )
    log.info("━" * 60)

    # ── Step 1: Fetch ────────────────────────────────────────────────────────
    log.info("[%s] Step 1/4 — Fetching P&L data from QuickBooks Online…", run_id)
    from fetcher import fetch_all
    raw_df = fetch_all()
    log.info("[%s] %d rows fetched across %d year(s)", run_id, len(raw_df), raw_df["year"].nunique())

    if raw_df.empty or raw_df["income"].sum() == 0:
        log.error("[%s] No income data returned — aborting. Check QBO credentials and realm ID.", run_id)
        sys.exit(1)

    # ── Step 2: Analytics ────────────────────────────────────────────────────
    log.info("[%s] Step 2/4 — Running analytics…", run_id)
    from analytics import run_all
    mom_df, yoy_df, flags_df = run_all(raw_df)
    log.info("[%s] MoM rows: %d  |  Anomalies flagged: %d", run_id, len(mom_df), len(flags_df))

    # ── Step 3: Render ───────────────────────────────────────────────────────
    log.info("[%s] Step 3/4 — Rendering HTML report…", run_id)
    from report import build_report
    html = build_report(mom_df, yoy_df, flags_df)
    log.info("[%s] Report size: %d chars", run_id, len(html))

    # ── Step 4: Send ─────────────────────────────────────────────────────────
    if dry_run:
        out = os.path.join(os.path.dirname(__file__), "preview_report.html")
        with open(out, "w") as f:
            f.write(html)
        log.info("[%s] Step 4/4 — [DRY RUN] Report saved to %s — no email sent.", run_id, out)
    else:
        log.info("[%s] Step 4/4 — Sending email…", run_id)
        from mailer import send_report
        send_report(html)
        log.info("[%s] Email sent successfully.", run_id)

    elapsed = time.monotonic() - started_at
    log.info("━" * 60)
    log.info(
        "EXECUTION COMPLETE — run_id=%s duration=%.2fs anomalies=%d email_sent=%s",
        run_id, elapsed, len(flags_df), not dry_run,
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
