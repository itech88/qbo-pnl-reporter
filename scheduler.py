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
from datetime import datetime, timezone

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


def _reporting_period(collected: dict[str, tuple]) -> tuple[int, int] | None:
    """(year, month) of the latest current-year month with income, across the
    collected reports — the headline period guardrails reconcile. None if no data."""
    current_year = datetime.now().year
    latest = None
    for value in collected.values():
        df = value[0]
        active = df[(df["year"] == current_year) & (df["income"] > 0)]
        if not active.empty:
            m = int(active["month"].max())
            latest = m if latest is None else max(latest, m)
    return (current_year, latest) if latest is not None else None


def _trailing_daily(series, months: int = 3) -> float | None:
    """Average daily flow over the last `months` completed months with positive data.

    Used for DSO/DPO: divide an A/R or A/P balance by a daily revenue/spend rate.
    Returns None when there is no usable history, so the report shows '—' instead of
    a divide-by-zero artifact.
    """
    positive = [v for v in series if v and v > 0]
    if not positive:
        return None
    recent = positive[-months:]
    return float(sum(recent)) / (len(recent) * 30.0)


def _trailing_daily_income(collected: dict[str, tuple]) -> float | None:
    """Daily revenue rate from any collected report's shared income column (for DSO)."""
    current_year = datetime.now().year
    for value in collected.values():
        df = value[0]
        cur = df[(df["year"] == current_year) & (df["income"] > 0)].sort_values("month")
        if not cur.empty:
            return _trailing_daily(cur["income"].tolist())
    return None


def _trailing_daily_cogs(collected: dict[str, tuple]) -> float | None:
    """Daily spend rate from the COGS report's value column (for DPO). None if absent."""
    current_year = datetime.now().year
    cogs = collected.get("COGS")
    if not cogs:
        return None
    df  = cogs[0]
    cur = df[(df["year"] == current_year) & (df["value"] > 0)].sort_values("month")
    return _trailing_daily(cur["value"].tolist()) if not cur.empty else None


def _section_totals(collected: dict[str, tuple], year: int, month: int) -> dict[str, float]:
    """Section value (plus the shared income) for the reporting month, keyed by report name."""
    from guardrails import INCOME
    totals: dict[str, float] = {}
    for name, value in collected.items():
        df = value[0]
        sub = df[(df["year"] == year) & (df["month"] == month)]
        if sub.empty:
            continue
        totals.setdefault(INCOME, float(sub.iloc[0]["income"]))
        totals[name] = float(sub.iloc[0]["value"])
    return totals


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
    _NON_DATA      = ("scorecard", "vendor_breakdown", "aging", "cash_outlook")
    data_cfgs_all  = [c for c in all_configs if c.get("type") not in _NON_DATA]
    scorecard_cfgs = [c for c in all_configs if c.get("type") == "scorecard"]
    vendor_cfgs    = [c for c in all_configs if c.get("type") == "vendor_breakdown"]
    aging_cfgs     = [c for c in all_configs if c.get("type") == "aging"]
    outlook_cfgs   = [c for c in all_configs if c.get("type") == "cash_outlook"]

    # ── Determine which configs to send ──────────────────────────────────────
    if report_filter:
        f = report_filter.lower()
        send_data    = [c for c in data_cfgs_all  if c["name"].lower() == f]
        send_score   = [c for c in scorecard_cfgs if c["name"].lower() == f]
        send_vendor  = [c for c in vendor_cfgs    if c["name"].lower() == f]
        send_aging   = [c for c in aging_cfgs     if c["name"].lower() == f]
        send_outlook = [c for c in outlook_cfgs   if c["name"].lower() == f]
        if not (send_data or send_score or send_vendor or send_aging or send_outlook):
            log.error("No config found matching --report %r", report_filter)
            sys.exit(1)
    elif force:
        send_data, send_score, send_vendor = data_cfgs_all, scorecard_cfgs, vendor_cfgs
        send_aging, send_outlook = aging_cfgs, outlook_cfgs
    else:
        day = today.day
        send_data    = [c for c in data_cfgs_all  if day in c.get("trigger_days", [1, 16])]
        send_score   = [c for c in scorecard_cfgs if day in c.get("trigger_days", [1])]
        send_vendor  = [c for c in vendor_cfgs    if day in c.get("trigger_days", [1, 16])]
        send_aging   = [c for c in aging_cfgs     if day in c.get("trigger_days", [1, 16])]
        send_outlook = [c for c in outlook_cfgs   if day in c.get("trigger_days", [1, 16])]
        if not (send_data or send_score or send_vendor or send_aging or send_outlook):
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

    log.info("[%s] Sending: %d data + %d vendor + %d aging + %d outlook + %d scorecard",
             run_id, len(send_data), len(send_vendor), len(send_aging),
             len(send_outlook), len(send_score))

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

    # ── Step 2.5: Pre-send data-quality guardrails ───────────────────────────
    # Reconcile this pull against QBO's own section totals and the P&L identities
    # (no stored history). A report that fails its checks is held back from
    # delivery; the others still send. A dry run only logs what *would* be held.
    from guardrails import reconcile_identities, report_sanity
    held: dict[str, list[str]] = {}
    period = _reporting_period(collected)
    if period:
        ry, rm = period
        # Partial = the reporting month is the current, still-incomplete calendar
        # month. Mid-month ratios are legitimate month-to-date (labelled by the
        # renderers), so the ratio sanity band is relaxed; identity + vendor
        # reconciliation still apply.
        is_partial = (ry == today.year and rm == today.month)
        totals   = _section_totals(collected, ry, rm)
        sendable = {c["name"] for c in send_data}
        for reason, implicated in reconcile_identities(totals):
            for nm in implicated:
                if nm in sendable:
                    held.setdefault(nm, []).append(reason)
        for cfg in send_data:
            nm = cfg["name"]
            if nm in collected:
                reasons = report_sanity(collected[nm][0], ry, rm,
                                        cfg.get("metric", "ratio"), partial=is_partial)
                if reasons:
                    held.setdefault(nm, []).extend(reasons)
        if held:
            log.warning("[%s] Guardrails flagged %d report(s) for hold: %s",
                        run_id, len(held), ", ".join(sorted(held)))

    failed_names: list[str] = []

    # ── Step 3: Send individual data reports ─────────────────────────────────
    for cfg in send_data:
        name = cfg["name"]
        if name in held:
            log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, name, "; ".join(held[name]))
            if not dry_run:
                continue   # withhold from the owner (previews still written in a dry run)
        try:
            metric = cfg.get("metric", "ratio")
            _, mom, yoy, flags = collected[name]
            log.info("[%s] ── %s (metric=%s) ──", run_id, name, metric)
            html, chart_png = build_report(mom, yoy, flags, cfg)
            log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, cfg, dry_run, run_id)
        except Exception:
            failed_names.append(name)
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
                    # Guardrail: the per-vendor detail must tie to the COGS summary
                    if period:
                        from guardrails import reconcile_vendor
                        vry, vrm = period
                        vendor_total = (
                            float(vdf[(vdf["year"] == vry) & (vdf["month"] == vrm)]["amount"].sum())
                            if not vdf.empty else 0.0
                        )
                        cogs_total = _section_totals(collected, vry, vrm).get("COGS")
                        vreasons = reconcile_vendor(cogs_total, vendor_total)
                        if vreasons:
                            held.setdefault(name, []).extend(vreasons)
                            log.error("[%s]   GUARDRAIL HOLD — %s: %s",
                                      run_id, name, "; ".join(vreasons))
                            if not dry_run:
                                continue
                    # Pin the vendor breakdown to the same reporting month the
                    # metric reports use (income-based). `period` is None only on a
                    # single-report run where no metric data was collected — fall
                    # back to the current calendar month there.
                    vendor_period = period if period else (today.year, today.month)
                    html, chart_png = build_vendor_report(vdf, cfg, report_period=vendor_period)
                    log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
                    subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
                    _deliver(html, chart_png, subject, cfg, dry_run, run_id)
                except Exception:
                    failed_names.append(name)
                    log.error("[%s]   FAILED vendor report '%s':\n%s",
                              run_id, name, traceback.format_exc())
        except Exception:
            failed_names.append("vendor detail fetch")
            log.error("[%s]   FAILED to fetch vendor detail:\n%s", run_id, traceback.format_exc())

    # ── Step 4.5: Aging reports (A/R, A/P) — point-in-time, separate endpoints
    # Each side's open-document detail must tie to QBO's own aging summary total
    # before it sends. Reconciled summaries are captured for the Cash Outlook step;
    # a side that fails reconciliation is held (if it was being sent) and excluded
    # from the outlook.
    aging_summaries: dict[str, dict] = {}   # side -> aging_summary (only when reconciled)
    if send_aging or send_outlook:
        from aging_fetcher import fetch_aging_raw, build_aging_dataframe
        from aging_analytics import aging_summary
        from report import build_aging_report
        from guardrails import reconcile_aging

        send_by_side = {c["side"]: c for c in send_aging}
        # Cash Outlook needs both sides even if only the outlook is being sent.
        needed_sides = set(send_by_side)
        if send_outlook:
            needed_sides |= {"receivable", "payable"}

        daily_income = _trailing_daily_income(collected)
        daily_cogs   = _trailing_daily_cogs(collected)

        for side in sorted(needed_sides):
            cfg = send_by_side.get(side)
            nm  = cfg["name"] if cfg else f"{side} aging"
            try:
                log.info("[%s] ── Aging (%s) ──", run_id, side)
                raw      = fetch_aging_raw(side)
                bdf, ddf = build_aging_dataframe(raw, cfg or {"name": nm, "side": side})
                summ     = aging_summary(bdf)
                detail_total = float(ddf["open_balance"].sum()) if not ddf.empty else 0.0
                reasons  = reconcile_aging(summ["total"], detail_total, label=nm)
                if reasons:
                    log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, "; ".join(reasons))
                    if cfg:
                        held.setdefault(nm, []).extend(reasons)
                else:
                    aging_summaries[side] = summ   # tied → safe to feed the outlook

                if cfg:
                    if nm in held and not dry_run:
                        continue   # withhold from owner; dry-run still writes a preview
                    trailing = daily_income if side == "receivable" else daily_cogs
                    html, chart_png = build_aging_report(bdf, ddf, cfg, trailing_daily=trailing)
                    log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
                    subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
                    _deliver(html, chart_png, subject, cfg, dry_run, run_id)
            except Exception:
                failed_names.append(nm)
                log.error("[%s]   FAILED aging '%s':\n%s", run_id, nm, traceback.format_exc())

    # ── Step 4.6: Cash Outlook — composes the reconciled A/R + A/P + cash on hand
    for oc in send_outlook:
        nm = oc["name"]
        try:
            log.info("[%s] ── Cash Outlook: %s ──", run_id, nm)
            if not ({"receivable", "payable"} <= aging_summaries.keys()):
                reason = "upstream A/R or A/P aging did not reconcile (both required)"
                held.setdefault(nm, []).append(reason)
                log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, reason)
                continue   # cannot build a trustworthy position without both tied summaries

            from cash_outlook import fetch_balance_sheet, extract_balance_sheet, build_outlook
            from report import build_cash_outlook
            from guardrails import reconcile_balance_sheet

            bs     = extract_balance_sheet(fetch_balance_sheet())
            ar_sum = aging_summaries["receivable"]
            ap_sum = aging_summaries["payable"]

            # Cross-anchor: the Balance Sheet's own A/R and A/P must match the aging totals.
            bs_reasons  = reconcile_balance_sheet(bs.get("ar"), ar_sum["total"], label="A/R")
            bs_reasons += reconcile_balance_sheet(bs.get("ap"), ap_sum["total"], label="A/P")
            if bs_reasons:
                held.setdefault(nm, []).extend(bs_reasons)
                log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, "; ".join(bs_reasons))
                if not dry_run:
                    continue

            outlook = build_outlook(ar_sum, ap_sum, bs.get("cash"))
            html, chart_png = build_cash_outlook(outlook, oc)
            log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = oc["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, oc, dry_run, run_id)
        except Exception:
            failed_names.append(nm)
            log.error("[%s]   FAILED cash outlook '%s':\n%s", run_id, nm, traceback.format_exc())

    # ── Step 5: Build and send scorecard(s) ──────────────────────────────────
    for sc_cfg in send_score:
        try:
            log.info("[%s] ── Scorecard: %s ──", run_id, sc_cfg["name"])
            threshold = float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))
            metrics_data = []
            for metric_name in sc_cfg.get("includes", []):
                if metric_name in held:
                    log.warning("[%s]   Scorecard: '%s' held by guardrails — excluding",
                                run_id, metric_name)
                    continue
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
            failed_names.append(sc_cfg["name"])
            log.error("[%s]   FAILED scorecard '%s':\n%s",
                      run_id, sc_cfg["name"], traceback.format_exc())

    elapsed = time.monotonic() - started_at
    failures = len(failed_names)
    held_names = sorted(held)
    total_reports = (len(send_data) + len(send_vendor) + len(send_aging)
                     + len(send_outlook) + len(send_score))
    log.info("━" * 60)
    log.info("EXECUTION COMPLETE — run_id=%s reports=%d held=%d failures=%d anomalies=%d "
             "duration=%.2fs email_sent=%s",
             run_id, total_reports, len(held_names), failures, total_anomalies, elapsed, not dry_run)
    log.info("━" * 60)

    if not dry_run:
        _check_pat_expiry(run_id)
        # Dead-man's-switch: a clean run pings OK; any held/failed report pings /fail.
        _ping_heartbeat(run_id, success=not (failures or held_names))

    if held_names:
        log.error("[%s] %d report(s) HELD by guardrails (not delivered): %s",
                  run_id, len(held_names), ", ".join(held_names))
    if failures:
        log.error("[%s] %d report(s) FAILED — see errors above.", run_id, failures)

    if (failures or held_names) and not dry_run:
        _alert_problems(today, env, run_id, failed_names, held)

    # Holds fail a live run (so the issue is visible) but never a dry run, which is
    # only validating; an outright failure exits non-zero in either mode.
    if failures or (held_names and not dry_run):
        sys.exit(1)


def _check_pat_expiry(run_id: str) -> None:
    """
    Warn by email when the GH_PAT is within GH_PAT_EXPIRY_WARN_DAYS (default 30)
    of expiring — or has already expired. The PAT is the one credential that
    cannot auto-rotate; if it lapses, rotated QBO tokens stop being written back
    and reporting hard-fails ~15 days later. This turns the documented manual
    calendar reminder into a self-issued alert.

    Best-effort: no-op locally (no PAT) and never raises. Fires on every live run
    inside the window, so the twice-monthly schedule nudges until the PAT is
    rotated. Rotating before expiry avoids the QBO re-bootstrap entirely.
    """
    try:
        from auth import github_pat_expiry
        expiry = github_pat_expiry()
        if expiry is None:
            return  # no PAT (local), unreachable, or non-expiring — nothing to warn

        warn_days = int(os.getenv("GH_PAT_EXPIRY_WARN_DAYS", "30"))
        days_left = (expiry - datetime.now(timezone.utc)).days

        if days_left > warn_days:
            log.info("[%s] GH_PAT healthy — %d day(s) to expiry (%s).",
                     run_id, days_left, expiry.strftime("%Y-%m-%d"))
            return

        log.warning("[%s] GH_PAT within %d-day window (%d day(s) left) — sending reminder.",
                    run_id, warn_days, days_left)

        expired = days_left < 0
        when = expiry.strftime("%Y-%m-%d %H:%M UTC")
        if expired:
            subject = "⚠️ GH_PAT has EXPIRED — QBO reporter needs a new token"
            headline = f"EXPIRED on {when} ({-days_left} day(s) ago)"
        else:
            subject = f"⚠️ GH_PAT expires in {days_left} day(s) — QBO reporter needs a new token"
            headline = f"expires on {when} ({days_left} day(s) from now)"

        body = (
            f"The GitHub PAT (GH_PAT) used by the QBO P&L reporter {headline}.\n\n"
            "This PAT is the one credential that cannot auto-rotate. If it lapses, the "
            "rotated QBO refresh token stops being written back to GitHub Secrets and "
            "reporting hard-fails about 15 days later (invalid_grant).\n\n"
            "Rotate it (about 2 minutes — no QBO re-bootstrap needed if done before expiry):\n"
            "  1. Create a fresh fine-grained PAT scoped to only the qbo-pnl-reporter repo,\n"
            "     permissions: Secrets = Read and write, Metadata = Read-only.\n"
            "  2. Update the GH_PAT repository secret with the new value.\n"
            "  3. Trigger any report manually and confirm the logs show\n"
            "     'Rotated tokens persisted to GitHub Secrets'.\n\n"
            f"Run id: {run_id}"
        )

        from mailer import send_failure_alert
        if send_failure_alert(subject, body):
            log.info("[%s] PAT expiry reminder sent.", run_id)
        else:
            log.warning("[%s] PAT expiry reminder not sent (SMTP not configured).", run_id)
    except Exception:
        log.error("[%s] PAT expiry check failed:\n%s", run_id, traceback.format_exc())


def _ping_heartbeat(run_id: str, success: bool) -> None:
    """
    Best-effort dead-man's-switch ping. If HEARTBEAT_URL is set, GET it on a clean
    run or <url>/fail on a problem run. An external monitor (e.g. healthchecks.io)
    alerts if an expected ping never arrives — catching the scariest failure mode,
    where the schedule silently stops firing and there is otherwise no signal at
    all. No-op without the env var; never raises.
    """
    url = os.getenv("HEARTBEAT_URL", "").strip()
    if not url:
        return
    target = url if success else url.rstrip("/") + "/fail"
    try:
        import requests
        requests.get(target, timeout=10)
        log.info("[%s] Heartbeat pinged (%s).", run_id, "ok" if success else "fail")
    except Exception as exc:  # noqa: BLE001 — monitoring must never break the run
        log.warning("[%s] Heartbeat ping failed: %s", run_id, exc)


def _alert_problems(today: datetime, env: str, run_id: str,
                    failed_names: list[str], held: dict[str, list[str]]) -> None:
    """Email the operator about held and/or failed reports. Best-effort, never raises."""
    parts: list[str] = []
    if held:
        parts.append("HELD by data-quality guardrails (NOT delivered to the owner):")
        for nm in sorted(held):
            parts.append(f"  - {nm}")
            parts.extend(f"      • {r}" for r in held[nm])
    if failed_names:
        if parts:
            parts.append("")
        parts.append("FAILED with errors:")
        parts.extend(f"  - {nm}" for nm in failed_names)

    summary = []
    if held:
        summary.append(f"{len(held)} held")
    if failed_names:
        summary.append(f"{len(failed_names)} failed")

    _alert_failure(
        f"QBO reports — {', '.join(summary)} ({today:%b %d %Y})",
        f"Run {run_id} on the {today:%B %d, %Y} schedule had issues; reports that passed "
        f"their checks were still delivered.\n\n" + "\n".join(parts) +
        f"\n\nEnvironment: {env}\nCheck the GitHub Actions logs (grep run_id={run_id}).",
    )


def _alert_failure(subject: str, body: str) -> None:
    """Best-effort failure alert; never raises."""
    try:
        from mailer import send_failure_alert
        if send_failure_alert(subject, body):
            log.info("Failure alert sent.")
        else:
            log.warning("Failure alert not sent (SMTP not configured).")
    except Exception:
        log.error("Could not send failure alert:\n%s", traceback.format_exc())


if __name__ == "__main__":
    args = _parse_args()
    try:
        run(force=args.force, dry_run=args.dry_run, report_filter=args.report)
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log.error("FATAL ERROR — pipeline aborted:\n%s", tb)
        if not args.dry_run:
            _alert_failure(
                "QBO reports FATAL ERROR — pipeline aborted",
                "The report pipeline aborted before completing.\n\n"
                f"{tb}\n\nCheck the GitHub Actions run logs for details.",
            )
        sys.exit(1)
