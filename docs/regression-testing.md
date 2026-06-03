# Regression Testing Runbook

This is a production financial-reporting pipeline the business relies on multiple
times a month. Because it only sends on the **1st and 16th**, a regression can hide
for up to two weeks before anyone notices. This runbook is the protocol that keeps
that from happening: a fast unit/regression gate on every change, plus an on-demand
dry run against real production data before you trust a deploy.

There are **two layers**:

| Layer | What it proves | Cost | When |
|---|---|---|---|
| **1. Unit/regression suite** (`pytest`) | The logic is correct in isolation — parsing, analytics, anomaly flags, rendering, orchestration, guardrails, auth/token handling, alerting | Seconds, no secrets, no network | Every push + PR (CI), and locally before you push |
| **2. Dry-run integration** (GitHub Actions) | The whole pipeline runs end-to-end against live QBO, builds all reports, and would deliver — **without sending email** | ~1 min, read-only prod API | Before/after a meaningful change; any time you want a production health check |
| **3. Runtime guardrails + heartbeat** (every live run) | Each pull reconciles against QBO's own totals before sending (bad report held, not emailed); a dead-man's-switch catches a schedule that silently stops firing | Built in, ~0 | Automatically, on every 1st/16th send |

Layers 1–2 are *commit-time* safety (does the code work?); layer 3 is *runtime* safety
(is this specific production execution trustworthy, and did it even happen?).

---

## The baseline (what the suite protects)

200 tests, all pure/mocked (no network). Coverage ~87% overall; the business-facing
and orchestration code is the most heavily covered.

| Module | What its tests guarantee |
|---|---|
| `analytics.py` | MoM / YoY math, anomaly flagging (ratio + absolute), `current_month_stats` (scorecard snapshot, prior-month fallback, zero-division) |
| `report.py` | Every report + scorecard + vendor view renders valid HTML and a real PNG chart; negative values render red; anomalies coloured; missing prior years tolerated; status thresholds (ON TRACK / WATCH / ACTION / GREY) and `higher_is_better` sign-flip |
| `guardrails.py` | Stateless reconciliation — P&L identities, vendor-vs-summary tie, per-report sanity bands; tolerance handling |
| `scheduler.py` | `run()` orchestration — trigger-day filtering (scorecard 1st-only), `--force`, `--report` filter, dry-run writes previews and sends nothing, P&L fetched once, one failing report still exits non-zero **and** alerts; **guardrail holds** (affected report withheld, others sent) and **heartbeat** ping; plus the `GH_PAT` expiry reminder |
| `auth.py` | Token-expiry detection, `refresh_tokens` success/failure, `QBOSession` 401-retry, rotated-token writeback gating (CI-only), PAT-expiry header parsing |
| `mailer.py` | Provider routing (smtp/sendgrid/ses), unknown-provider guard, inline-chart MIME, best-effort failure alert |
| `fetcher.py` / `vendor_fetcher.py` | P&L parsing, section/line-item/sub-section extraction, per-vendor COGS, memo fallback for payee-less card charges |
| `vendor_analytics.py` | Vendor share, monthly matrix + "Other" folding, vendor YoY |

---

## Layer 1 — Unit/regression gate

### Locally
```bash
source .venv/bin/activate
pytest                       # runs tests/ (testpaths set in pytest.ini)
pytest --cov=. --cov-report=term-missing   # with coverage
pytest tests/test_report.py -v             # one module
```
All green is the bar. **Do not push red.**

### In CI (the gate "GitHub must run before deployment")
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs the full suite on **every
push and every pull request** (Python 3.13, no secrets). Check it after pushing:
```bash
gh run list --workflow=ci.yml -L 5
gh run watch                 # follow the latest run
```
Branch protection is intentionally **not** enabled (solo direct-to-main workflow), so
CI is a signal, not a hard block — treat a red `CI` run as "stop and fix before the
next 1st/16th."

---

## Layer 2 — Dry-run integration validation

Runs the real pipeline against production QBO (**read-only**, ~12 API calls), builds all
10 reports + the scorecard, and uploads them as preview HTML — **no email is sent**.

```bash
# Trigger an all-reports dry run
gh workflow run monthly_report.yml -f dry_run=true

# Watch it
gh run list --workflow=monthly_report.yml -L 3
gh run watch <run-id>

# Download the rendered previews and open them
gh run download <run-id> -n preview-reports -D /tmp/qbo-preview
open /tmp/qbo-preview/preview_*.html        # macOS; or just open the folder
```

> A single-report dry run is also available per report, e.g.
> `gh workflow run report_cogs.yml -f dry_run=true` (artifact `preview-cogs`).

### What to verify in the previews
- **Numbers tie out** — pull the same report/month in QuickBooks and confirm the figure matches.
- **Vendor names are clean** — COGS-by-Vendor shows real suppliers, minimal/no `Unattributed`.
  New `Unattributed` or misspelled rows → add a descriptor to `aliases` in
  `reports/cogs_by_vendor.yaml`.
- **Anomaly flags are sane** — flagged months look genuinely off; nothing obvious is missed.
  If noisy/quiet, tune `COGS_VARIANCE_THRESHOLD`.
- **Scorecard reports the completed month** (the prior month on the 1st), not an empty one.
- **Charts render** in every preview (no missing/blank images).
- **Logs show no send** — the run log ends with `email_sent=False` / `[DRY RUN]` lines.

---

## Layer 3 — Runtime guardrails & heartbeat

These run automatically on every live execution; there's nothing to invoke. They protect
the two failure modes that hurt a business most: **wrong numbers that look right**, and
**no report at all**.

### Pre-send reconciliation (guardrails)

`guardrails.py` reconciles each pull **statelessly** — against QBO's own totals and the
P&L identities, no database — before any email goes out:
- `Income − COGS = Gross Profit`, `Gross Profit − OpEx = Net Operating Income`
- `Σ(per-vendor COGS detail) ≈ COGS summary` (the two QBO endpoints must agree)
- per-report sanity: value present, finite, plausible vs income

A report that fails is **held back; the rest still send**, and you get an alert naming
the held report and what didn't reconcile. The run log shows `held=N` on the
`EXECUTION COMPLETE` line, and a live run with any hold exits non-zero.

**When a report is held:** the data didn't tie out — usually a QBO sync still settling, a
mis-mapped account, or an extraction change. Confirm in QuickBooks, fix the mapping if
needed, then re-run (`gh workflow run report_<name>.yml` or the monthly workflow). Tune
`RECON_TOLERANCE` only if rounding causes false holds.

### Heartbeat / dead-man's-switch

Set the `HEARTBEAT_URL` secret to a free monitor (e.g. **healthchecks.io**) configured to
expect a ping on the 1st and 16th. The scheduled run pings it on success (and `<url>/fail`
on a held/failed run); if a ping never arrives — because GitHub silently disabled the cron,
the runner never started, etc. — the monitor alerts *you*. This is the only safeguard that
catches total silence. No `HEARTBEAT_URL` → the ping no-ops.

**Setup:** create a check on healthchecks.io with period = 1 day and a grace window; set its
schedule to the 1st/16th; copy its ping URL into the `HEARTBEAT_URL` repo secret.

---

## Pre-deployment checklist

Run top to bottom before trusting any change in production:

1. `pytest` green locally.
2. Push → **CI green** (`gh run watch`).
3. `gh workflow run monthly_report.yml -f dry_run=true` → run succeeds.
4. Download previews → numbers, vendors, flags, scorecard month, and charts all check out.
5. Only then is the change trusted for the live 1st/16th send.

If any step fails, stop — a broken pipeline that still exits green on a dry run is exactly
the silent failure this protocol exists to catch.

---

## Acceptance criteria (green = deployable)

- [ ] `pytest` — 179/179 passing locally.
- [ ] `CI` workflow green on the pushed commit.
- [ ] Dry-run workflow **succeeds** and uploads a non-empty `preview-reports` artifact.
- [ ] All 10 reports + scorecard present and rendered; at least one figure reconciled to QBO.
- [ ] No unexpected `Unattributed` vendors; anomaly flags look right.
- [ ] Run log shows `email_sent=False` (nothing delivered during validation).
