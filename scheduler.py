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

from env import env_int
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
    """(year, month) of the latest month with income across the collected reports —
    the headline period guardrails reconcile. The year is the latest year that has
    income (the reporting year), not the calendar year: on Jan 1 the December close
    must still be reconciled, not skipped (RB-1, mirroring analytics._reporting_year).
    None when no report has any income."""
    reporting_year = None
    for value in collected.values():
        wi = value[0][value[0]["income"] > 0]
        if not wi.empty:
            y = int(wi["year"].max())
            reporting_year = y if reporting_year is None else max(reporting_year, y)
    if reporting_year is None:
        return None
    latest = None
    for value in collected.values():
        df = value[0]
        active = df[(df["year"] == reporting_year) & (df["income"] > 0)]
        if not active.empty:
            m = int(active["month"].max())
            latest = m if latest is None else max(latest, m)
    return (reporting_year, latest) if latest is not None else None


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
    """Daily revenue rate from any collected report's shared income column (for DSO).
    Keys on the reporting year (latest year with income) so DSO still computes on Jan 1."""
    for value in collected.values():
        df = value[0]
        wi = df[df["income"] > 0]
        if wi.empty:
            continue
        ry  = int(wi["year"].max())
        cur = df[(df["year"] == ry) & (df["income"] > 0)].sort_values("month")
        if not cur.empty:
            return _trailing_daily(cur["income"].tolist())
    return None


def _trailing_daily_cogs(collected: dict[str, tuple]) -> float | None:
    """Daily spend rate from the COGS report's value column (for DPO). None if absent.
    Keys on the reporting year (latest year with COGS spend) so DPO computes on Jan 1."""
    cogs = collected.get("COGS")
    if not cogs:
        return None
    df    = cogs[0]
    spent = df[df["value"] > 0]
    if spent.empty:
        return None
    ry  = int(spent["year"].max())
    cur = df[(df["year"] == ry) & (df["value"] > 0)].sort_values("month")
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


def _record_failure(name: str, tb: str) -> None:
    """Log a report that raised, keeping the exception's last line as the signal."""
    import incidents
    lines = [ln for ln in (tb or "").strip().splitlines() if ln.strip()]
    last = lines[-1] if lines else "unknown error"
    incidents.record(incidents.Incident(
        kind="report_failure",
        report=name,
        summary=f"Raised while being built or sent: {last}",
        reasoning=incidents.explain(tb or ""),
        remedy=("This report was skipped and the run continued, so the other "
                "reports still went out."),
        resolved=False,
    ))


def _record_holds(held: dict[str, list[str]]) -> None:
    """Log every held report once, with the complete set of reasons it failed.

    Recorded at the end of the run rather than at each check, so a report that
    trips two guardrails produces one entry listing both rather than two partial
    entries that each tell half the story.
    """
    import incidents
    for name in sorted(held):
        reasons = held[name]
        joined = "; ".join(reasons)
        incidents.record(incidents.Incident(
            kind="guardrail_hold",
            report=name,
            summary=f"Withheld from delivery: {joined}",
            reasoning=incidents.explain(joined),
            remedy=("Not sent. A guardrail holds a report rather than deliver "
                    "numbers that failed reconciliation, so the owner never "
                    "receives figures the system could not verify."),
            resolved=False,
        ))


def _heal_aging_from_detail(ddf, summary_total: float, bs_total: float | None,
                            detail_total: float | None):
    """Rebuild aging buckets from the per-document detail when the summary is unusable.

    QBO's aging *summary* endpoint intermittently returns no per-party rows while
    the detail endpoint returns the documents perfectly well. Losing the report
    over that is a waste: the detail carries every open balance and due date the
    summary would have bucketed.

    The repair is deliberately conservative. It only runs when the Balance Sheet —
    the ledger of record — independently agrees with the detail, so a bad summary
    is never swapped for an equally unverified substitute. The rebuilt frame is
    then re-checked against that same anchor, so self-healing satisfies the
    guardrail rather than bypassing it: a derivation that does not reconcile is
    discarded and the report is held exactly as before.

    Returns (buckets_df, summary_dict) on success, or None to leave the hold in place.
    """
    if bs_total is None or detail_total is None:
        return None   # no independent corroboration available — do not substitute

    from guardrails import tolerance
    tol = max(tolerance(), abs(bs_total) * 0.01)

    summary_is_wrong = abs(summary_total - bs_total) > tol
    detail_is_trusted = abs(detail_total - bs_total) <= tol
    if not (summary_is_wrong and detail_is_trusted):
        return None

    from aging_analytics import aging_summary
    from aging_fetcher import derive_buckets_from_detail

    healed_df = derive_buckets_from_detail(ddf)
    healed = aging_summary(healed_df)
    if abs(healed["total"] - bs_total) > tol:
        return None   # the rebuild did not actually reconcile; hold the report

    return healed_df, healed


def run(force: bool = False, dry_run: bool = False, report_filter: str | None = None) -> None:
    today      = datetime.now()
    run_id     = str(uuid.uuid4())[:8]
    started_at = time.monotonic()
    env        = os.getenv("QBO_ENVIRONMENT", "sandbox")

    # Start each run with a clean token-writeback state; a failed writeback during
    # this run (CI only) is recorded in auth and fails the run at the end (SEC-2).
    from auth import reset_writeback_state
    reset_writeback_state()

    # The incident buffer is module-level too; clear it so a long-lived process
    # (tests, a local REPL) can't leak one run's incidents into the next.
    import incidents
    incidents.reset()

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
    from analytics import run_all, current_month_stats, variance_threshold
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
            _tb = traceback.format_exc()
            failed_names.append(name)
            log.error("[%s]   FAILED to process report '%s':\n%s",
                      run_id, name, _tb)
            _record_failure(name, _tb)

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
                    _tb = traceback.format_exc()
                    failed_names.append(name)
                    log.error("[%s]   FAILED vendor report '%s':\n%s",
                              run_id, name, _tb)
                    _record_failure(name, _tb)
        except Exception:
            _tb = traceback.format_exc()
            failed_names.append("vendor detail fetch")
            log.error("[%s]   FAILED to fetch vendor detail:\n%s", run_id, _tb)
            _record_failure("vendor detail fetch", _tb)

    # ── Step 4.5: Balance-sheet snapshot reports (A/R + A/P aging) ────────────
    # The Balance Sheet is fetched once and used two ways: its own A/R / A/P lines
    # are the authoritative anchor each aging summary must reconcile against, and its
    # cash on hand feeds the Cash Outlook. Anchoring on the Balance Sheet (not only on
    # detail-vs-summary) keeps the reconcile-before-send guarantee even when a detail
    # endpoint is unavailable — e.g. Intuit's "wrong cluster" fault on AgedPayableDetail
    # — in which case the report degrades to a summary-only view.
    aging_summaries: dict[str, dict] = {}   # side -> aging_summary (only when reconciled)
    bs: dict = {}
    if send_aging or send_outlook:
        from aging_fetcher import fetch_aging_raw, build_aging_dataframe
        from aging_analytics import aging_summary
        from report import build_aging_report
        from guardrails import reconcile_aging, reconcile_balance_sheet

        try:
            from cash_outlook import fetch_balance_sheet, extract_balance_sheet
            bs = extract_balance_sheet(fetch_balance_sheet())
            log.info("[%s] Balance Sheet — cash=%s A/R=%s A/P=%s",
                     run_id, bs.get("cash"), bs.get("ar"), bs.get("ap"))
        except Exception:
            log.error("[%s] Balance Sheet fetch failed:\n%s", run_id, traceback.format_exc())
            bs = {}

        send_by_side = {c["side"]: c for c in send_aging}
        needed_sides = set(send_by_side)        # Cash Outlook needs the A/R side
        if send_outlook:
            needed_sides |= {"receivable"}      # "owed" comes from BS current liabilities, not A/P

        daily_income = _trailing_daily_income(collected)
        daily_cogs   = _trailing_daily_cogs(collected)
        _bs_key      = {"receivable": "ar", "payable": "ap"}
        _label       = {"receivable": "A/R", "payable": "A/P"}

        for side in sorted(needed_sides):
            cfg = send_by_side.get(side)
            nm  = cfg["name"] if cfg else f"{side} aging"
            try:
                log.info("[%s] ── Aging (%s) ──", run_id, side)
                raw           = fetch_aging_raw(side)   # detail is best-effort inside
                bdf, ddf      = build_aging_dataframe(raw, cfg or {"name": nm, "side": side})
                summ          = aging_summary(bdf)
                detail_avail  = raw.get("detail") is not None
                bs_total      = bs.get(_bs_key[side])
                detail_total  = (float(ddf["open_balance"].sum())
                                 if detail_avail and not ddf.empty else
                                 (0.0 if detail_avail else None))

                # Self-heal before reconciling: a summary that returned no rows can
                # be rebuilt from the detail, provided the Balance Sheet vouches for
                # the detail. See _heal_aging_from_detail for why that guard matters.
                repair = _heal_aging_from_detail(ddf, summ["total"], bs_total, detail_total)
                if repair is not None:
                    broken_total = summ["total"]
                    bdf, summ = repair
                    detail_total = summ["total"]   # the buckets now *are* the detail
                    log.warning(
                        "[%s]   SELF-HEALED %s — aging summary returned %.2f against a "
                        "Balance Sheet %s of %.2f; rebuilt the buckets from the "
                        "%d open document(s) in the detail report, which the Balance "
                        "Sheet corroborates. Report delivered.",
                        run_id, nm, broken_total, _label[side], bs_total, len(ddf),
                    )
                    incidents.record(incidents.Incident(
                        kind="self_heal",
                        report=nm,
                        summary=(
                            f"The {_label[side]} aging summary endpoint returned "
                            f"{broken_total:,.2f} while the Balance Sheet showed "
                            f"{bs_total:,.2f} for the same date."
                        ),
                        reasoning=incidents.explain(
                            f"aging total {broken_total:.2f} != Balance Sheet "
                            f"{bs_total:.2f}"
                        ),
                        remedy=(
                            "Rebuilt the aging buckets from the per-document detail "
                            "report, which the Balance Sheet independently "
                            "corroborates, then re-ran the same reconciliation "
                            "against the Balance Sheet before sending. The report "
                            "was delivered rather than held."
                        ),
                        resolved=True,
                    ))

                reasons: list[str] = []
                anchored = False
                if bs_total is not None:
                    reasons += reconcile_balance_sheet(bs_total, summ["total"], label=_label[side])
                    anchored = True
                if detail_avail:
                    reasons += reconcile_aging(summ["total"], detail_total or 0.0, label=nm)
                    anchored = True
                if not anchored:
                    reasons.append("no reconciliation anchor — Balance Sheet line and detail "
                                   "endpoint both unavailable")

                if reasons:
                    log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, "; ".join(reasons))
                    if cfg:
                        held.setdefault(nm, []).extend(reasons)
                else:
                    aging_summaries[side] = summ   # reconciled → safe to feed the outlook

                if cfg:
                    if nm in held and not dry_run:
                        continue   # withhold from owner; dry-run still writes a preview
                    trailing = daily_income if side == "receivable" else daily_cogs
                    html, chart_png = build_aging_report(
                        bdf, ddf, cfg, trailing_daily=trailing, detail_available=detail_avail)
                    log.info("[%s]   HTML=%d chars  chart=%d bytes%s", run_id, len(html),
                             len(chart_png), "" if detail_avail else "  (summary-only)")
                    subject = cfg["subject"].format(month=today.strftime("%B"), year=today.year)
                    _deliver(html, chart_png, subject, cfg, dry_run, run_id)
            except Exception:
                _tb = traceback.format_exc()
                failed_names.append(nm)
                log.error("[%s]   FAILED aging '%s':\n%s", run_id, nm, _tb)
                _record_failure(nm, _tb)

    # ── Step 4.6: Cash Outlook — composes reconciled A/R + cash on hand + current liabilities
    for oc in send_outlook:
        nm = oc["name"]
        try:
            log.info("[%s] ── Cash Outlook: %s ──", run_id, nm)
            if "receivable" not in aging_summaries:
                reason = "upstream A/R aging did not reconcile"
                held.setdefault(nm, []).append(reason)
                log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, reason)
                continue   # cannot build a trustworthy position without a reconciled A/R summary
            if bs.get("current_liabilities") is None:
                reason = "Balance Sheet 'Total Current Liabilities' unavailable — no trustworthy 'owed' figure"
                held.setdefault(nm, []).append(reason)
                log.error("[%s]   GUARDRAIL HOLD — %s: %s", run_id, nm, reason)
                continue   # "owed" must come from a real BS line, never a $0 default

            from cash_outlook import build_outlook
            from report import build_cash_outlook
            # A/R was anchored to the Balance Sheet above; "owed" is the BS's own Total
            # Current Liabilities line. The outlook composes them with cash on hand.
            outlook = build_outlook(aging_summaries["receivable"],
                                    bs.get("cash"), bs.get("current_liabilities"))
            html, chart_png = build_cash_outlook(outlook, oc)
            log.info("[%s]   HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = oc["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, oc, dry_run, run_id)
        except Exception:
            _tb = traceback.format_exc()
            failed_names.append(nm)
            log.error("[%s]   FAILED cash outlook '%s':\n%s", run_id, nm, _tb)
            _record_failure(nm, _tb)

    # ── Step 5: Build and send scorecard(s) ──────────────────────────────────
    for sc_cfg in send_score:
        try:
            log.info("[%s] ── Scorecard: %s ──", run_id, sc_cfg["name"])
            threshold = variance_threshold()
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

            # Pass the threshold the metrics were scored against, so the scorecard's
            # status bands and the anomaly flags can't drift apart (CQ-6).
            html, chart_png = build_scorecard(metrics_data, sc_cfg, threshold=threshold)
            log.info("[%s]   Scorecard HTML=%d chars  chart=%d bytes", run_id, len(html), len(chart_png))
            subject = sc_cfg["subject"].format(month=today.strftime("%B"), year=today.year)
            _deliver(html, chart_png, subject, sc_cfg, dry_run, run_id)
        except Exception:
            _tb = traceback.format_exc()
            failed_names.append(sc_cfg["name"])
            log.error("[%s]   FAILED scorecard '%s':\n%s",
                      run_id, sc_cfg["name"], _tb)
            _record_failure(sc_cfg["name"], _tb)

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

    # A CI token writeback that failed this run means the rotated refresh token
    # never reached GitHub Secrets — the next run would hit invalid_grant. None on
    # local runs and clean CI runs.
    from auth import writeback_failed
    wb_error = writeback_failed()

    # RB-5: operator alerts are SMTP-only and independent of EMAIL_PROVIDER. If
    # those credentials were dropped (e.g. after moving reports to SendGrid/SES),
    # every failure/hold/PAT alert would silently no-op — the safety net vanishing
    # exactly when the config changed. Surface it through the SMTP-independent
    # heartbeat so an external monitor still catches it.
    from mailer import alerting_configured
    alerts_down = not dry_run and not alerting_configured()
    if alerts_down:
        log.error("[%s] ALERTING CHANNEL DOWN — SMTP credentials for operator alerts "
                  "are not configured; failure/hold/PAT alerts would silently no-op. "
                  "Reports still send. Pinging heartbeat /fail so the dead-man's-switch "
                  "surfaces this (RB-5).", run_id)

    if not dry_run:
        _check_pat_expiry(run_id)
        # Dead-man's-switch: a clean run pings OK; a held/failed report, a failed
        # token writeback, or a down alert channel pings /fail.
        _ping_heartbeat(run_id,
                        success=not (failures or held_names or wb_error or alerts_down))

    if held_names:
        log.error("[%s] %d report(s) HELD by guardrails (not delivered): %s",
                  run_id, len(held_names), ", ".join(held_names))
    if failures:
        log.error("[%s] %d report(s) FAILED — see errors above.", run_id, failures)
    if wb_error:
        log.error("[%s] TOKEN WRITEBACK FAILED — the rotated QBO refresh token was not "
                  "persisted to GitHub Secrets; the next run will fail with invalid_grant "
                  "until this is fixed. Reason: %s", run_id, wb_error)

    # Every held report becomes one incident carrying its full reason list. Done
    # here, after all checks have run, so a report that trips two guardrails is
    # recorded once with both reasons rather than twice with one each.
    _record_holds(held)

    if wb_error:
        incidents.record(incidents.Incident(
            kind="report_failure",
            report=None,
            summary=f"The rotated QBO refresh token was not written back: {wb_error}",
            reasoning=incidents.explain(wb_error),
            remedy=("Reports for this run already went out, but the next run will "
                    "fail on an expired token chain until the PAT or secret is "
                    "repaired. The run is failed deliberately so this surfaces now, "
                    "while recovery is still a secret fix rather than a re-consent."),
            resolved=False,
        ))

    # Append this run's incidents to docs/incident-log.md. The workflow commits the
    # file, so the record outlives both the runner and the Actions log retention.
    incidents.flush(run_id, f"{env}, dry run" if dry_run else env,
                    run_url=os.getenv("RUN_URL") or None)

    if (failures or held_names) and not dry_run:
        _alert_problems(today, env, run_id, failed_names, held)
    if wb_error and not dry_run:
        _alert_failure(
            "⚠️ QBO token writeback FAILED — fix before the next run",
            "The QBO refresh token was rotated on this run but could NOT be written back "
            "to GitHub Secrets. Reports already went out, but the NEXT scheduled run will "
            "fail with invalid_grant — and require a full QBO re-consent — unless this is "
            "fixed first.\n\n"
            f"Reason: {wb_error}\n\n"
            "Most likely the GH_PAT expired or lost Secrets:write. Rotate/repair the PAT, "
            "then trigger any report manually and confirm the logs show "
            "'Rotated tokens persisted to GitHub Secrets'.\n\n"
            f"Run id: {run_id}\nEnvironment: {env}",
        )

    # Holds fail a live run (so the issue is visible) but never a dry run, which is
    # only validating. An outright failure or a failed token writeback exits non-zero
    # in either mode — a stale refresh token is dangerous even after a dry run, which
    # still rotates the token in CI.
    if failures or wb_error or (held_names and not dry_run):
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

        warn_days = env_int("GH_PAT_EXPIRY_WARN_DAYS", 30)
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

        # An abort is the incident most worth recording: nothing was delivered, and
        # the Actions log that explains why ages out. run() never reached its own
        # flush, so record and flush here.
        try:
            import incidents
            lines = [ln for ln in tb.strip().splitlines() if ln.strip()]
            incidents.record(incidents.Incident(
                kind="abort",
                report=None,
                summary=("The pipeline aborted before delivering anything: "
                         + (lines[-1] if lines else "unknown error")),
                reasoning=incidents.explain(tb),
                remedy=("No reports were sent. Every report in the run was lost, "
                        "not just the one that failed."),
                resolved=False,
            ))
            incidents.flush(
                "aborted", os.getenv("QBO_ENVIRONMENT", "sandbox"),
                run_url=os.getenv("RUN_URL") or None,
            )
        except Exception:  # noqa: BLE001 — never mask the original failure
            log.exception("Could not record the abort incident.")

        if not args.dry_run:
            _alert_failure(
                "QBO reports FATAL ERROR — pipeline aborted",
                "The report pipeline aborted before completing.\n\n"
                f"{tb}\n\nCheck the GitHub Actions run logs for details.",
            )
        sys.exit(1)
