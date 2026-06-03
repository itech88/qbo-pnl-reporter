# Regression Testing Runbook

This is a production financial-reporting pipeline the business relies on multiple
times a month. Because it only sends on the **1st and 16th**, a regression can hide
for up to two weeks before anyone notices. This runbook is the protocol that keeps
that from happening: a fast unit/regression gate on every change, plus an on-demand
dry run against real production data before you trust a deploy.

There are **two layers**:

| Layer | What it proves | Cost | When |
|---|---|---|---|
| **1. Unit/regression suite** (`pytest`) | The logic is correct in isolation — parsing, analytics, anomaly flags, rendering, orchestration, auth/token handling, alerting | Seconds, no secrets, no network | Every push + PR (CI), and locally before you push |
| **2. Dry-run integration** (GitHub Actions) | The whole pipeline runs end-to-end against live QBO, builds all reports, and would deliver — **without sending email** | ~1 min, read-only prod API | Before/after a meaningful change; any time you want a production health check |

---

## The baseline (what the suite protects)

179 tests, all pure/mocked (no network). Coverage ~87% overall; the business-facing
and orchestration code is the most heavily covered.

| Module | What its tests guarantee |
|---|---|
| `analytics.py` | MoM / YoY math, anomaly flagging (ratio + absolute), `current_month_stats` (scorecard snapshot, prior-month fallback, zero-division) |
| `report.py` | Every report + scorecard + vendor view renders valid HTML and a real PNG chart; negative values render red; anomalies coloured; missing prior years tolerated; status thresholds (ON TRACK / WATCH / ACTION / GREY) and `higher_is_better` sign-flip |
| `scheduler.py` | `run()` orchestration — trigger-day filtering (scorecard 1st-only), `--force`, `--report` filter, dry-run writes previews and sends nothing, P&L fetched once, one failing report still exits non-zero **and** alerts; plus the `GH_PAT` expiry reminder |
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
