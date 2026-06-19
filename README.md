# QBO P&L Reporter

Automated Profit & Loss analytics for an optometry practice. On the **1st and
16th of each month** it pulls QuickBooks Online P&L data, computes month-over-month
and year-over-year metrics, flags anomalies, and emails a set of HTML reports to
the business owner — fully unattended, running on GitHub Actions.

> Scope note: this started as a single COGS report (`qbo-cogs-reporter`) and grew
> into full P&L analysis, hence the rename to **`qbo-pnl-reporter`**.

---

## What it produces

Ten reports, all config-driven (`reports/*.yaml`):

| Report | Type | Metric |
|---|---|---|
| COGS | section summary | % of income |
| Gross Profit | section summary | $ + % |
| Net Operating Income | section summary | $ + % |
| Total Operating Expense Ratio | section summary | $ + % |
| Total Payroll Burden | sub-section summary | $ + % |
| Utilities | sub-section summary | $ + % |
| Home Office Supplies | line item | $ + % |
| Payroll Expense Home Office Reimbursement | line item | $ + % |
| **COGS by Vendor** | vendor breakdown | $ share, dynamic vendors |
| **A/R Aging** | aging snapshot | money owed *to you*, by payer + age bucket |
| **A/P Aging** | aging snapshot | money *you owe*, by creditor + age bucket |
| **Cash Outlook** | position snapshot | cash on hand + A/R − A/P = net position |
| **Monthly Business Dashboard** | scorecard (1st only) | traffic-light vs 3-yr avg |

Schedule: the 8 metric reports + COGS by Vendor + the three balance-sheet reports
(A/R Aging, A/P Aging, Cash Outlook) send on the **1st and 16th**; the scorecard sends
on the **1st only**. Cron is `0 18 1,16 * *` (18:00 UTC = 10 AM PST / 11 AM PDT).

> **Flow vs. snapshot:** the P&L reports answer *"am I profitable?"* over a month; the
> aging + outlook reports answer *"where is my cash?"* as of today. Aging is a
> point-in-time snapshot (no MoM/YoY), reconciled against QBO's own aging totals and
> the Balance Sheet before sending. See [docs/business-requirements.md](docs/business-requirements.md).

---

## Architecture at a glance

```
GitHub Actions (cron) ──> scheduler.py
                              │  loads reports/*.yaml
                              │  fetches P&L once, reuses across reports
                              ├─ fetcher.py / vendor_fetcher.py  ── QBO Reports API
                              ├─ analytics.py / vendor_analytics.py
                              ├─ report.py (Jinja2 + matplotlib)
                              └─ mailer.py ── Gmail SMTP ──> owner inbox
auth.py: OAuth refresh + rotated-token writeback to GitHub Secrets
logger.py: rotating file + stdout, captures intuit_tid on every API call
```

See [docs/solution-architecture.md](docs/solution-architecture.md) and
[docs/dataflow.md](docs/dataflow.md) for diagrams,
[docs/business-requirements.md](docs/business-requirements.md) for the use cases
in Gherkin, and
[docs/tokenization-and-api-calls.md](docs/tokenization-and-api-calls.md) for a
plain-language explanation of how API tokens and authentication work here.

---

## Module map

| File | Responsibility |
|---|---|
| `auth.py` | OAuth 2.0, token refresh on 401 / pre-expiry, `QBOSession`, **writeback of rotated tokens to GitHub Secrets** |
| `fetcher.py` | `ProfitAndLoss` summary → DataFrame; section / line-item / sub-section extraction |
| `vendor_fetcher.py` | `ProfitAndLossDetail` → per-vendor COGS; **memo fallback** for blank-payee charges |
| `aging_fetcher.py` | `AgedReceivables`/`AgedPayables` (+ detail) → buckets per payer/creditor; party aliasing |
| `cash_outlook.py` | `BalanceSheet` cash on hand + composes the A/R/A/P summaries into a position |
| `analytics.py` | MoM, YoY, anomaly flagging, scorecard stats |
| `vendor_analytics.py` | Vendor share breakdown, monthly matrix, vendor YoY |
| `aging_analytics.py` | Aging totals, per-party bucket breakdown, oldest-items worklist, DSO/DPO |
| `report.py` | Renders HTML + matplotlib charts (report, scorecard, vendor) |
| `guardrails.py` | **Stateless pre-send reconciliation** — P&L cross-foot identities, vendor-vs-summary tie, aging detail-vs-summary + Balance-Sheet tie, per-report sanity bands |
| `mailer.py` | SMTP / SendGrid / SES delivery (CID inline chart); `send_failure_alert` |
| `scheduler.py` | Entry point: load configs, fetch once, **guardrail-check, hold bad reports**, run each, alert, heartbeat |
| `logger.py` | Shared rotating logger |
| `reports/*.yaml` | Report definitions (extraction type, metric, schedule, recipient) |
| `templates/*.html` | Email templates |
| `.github/workflows/*.yml` | Scheduled run + one manual-dispatch workflow per report |

---

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in QBO + SMTP credentials

python auth.py                # one-time OAuth browser flow → writes tokens to .env
python scheduler.py --force --dry-run         # all reports, no email, writes preview_*.html
python scheduler.py --force --dry-run --report "COGS by Vendor"   # one report
pytest tests/                 # full suite
```

Writeback to GitHub Secrets is **CI-only** (gated on `GH_PAT` + `GITHUB_REPOSITORY`),
so local runs never touch secrets.

---

## Deployment (GitHub Actions)

Repository secrets required (**Settings → Secrets and variables → Actions**):

`QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`, `QBO_REALM_ID`,
`QBO_ACCESS_TOKEN`, `QBO_REFRESH_TOKEN`, `QBO_TOKEN_EXPIRY`,
`EMAIL_PROVIDER`, `EMAIL_FROM`, `EMAIL_TO`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, `COGS_VARIANCE_THRESHOLD`, **`ALERT_EMAIL`**, and **`GH_PAT`** (see below).

> **`EMAIL_TO` vs `ALERT_EMAIL`:** `EMAIL_TO` is the **business owner's** report address;
> **`ALERT_EMAIL`** is **your** operator address for failure / hold / PAT-expiry alerts.
> Alerts default to `ALERT_EMAIL`, fall back to `EMAIL_FROM` (the sending account), and are
> **never** sent to `EMAIL_TO` — the owner must not receive technical alerts.

Optional: `HEARTBEAT_URL` (dead-man's-switch ping target — see below),
`RECON_TOLERANCE` (guardrail dollar tolerance, default `1.00`),
`GH_PAT_EXPIRY_WARN_DAYS` (default `30`). All no-op/​default when unset.

The scheduled `monthly_report.yml` respects each report's `trigger_days`; manual
dispatches force-run all reports for testing.

---

## Operations / runbook

### Token lifecycle (how it stays alive unattended)

QBO access tokens last 60 min; the **refresh token rotates on a ~24h window** and
is valid 100 days. Each run refreshes and then **writes the rotated tokens back to
GitHub Secrets** (`auth._persist_to_github_secrets`, via `gh secret set` fed over
stdin). This keeps the stored refresh token current between the 15-day gaps in the
schedule. The writeback needs a PAT because the default `GITHUB_TOKEN` cannot write
secrets.

### ⚠️ GH_PAT expiry — what happens and how to recover

The `GH_PAT` is the one credential that **cannot auto-rotate**. If it expires and is
not replaced:

**Timeline of failure**
1. **First run after expiry:** the report still sends and the run is **GREEN**. The
   writeback fails quietly — the only signal is an ERROR log line
   `Failed to persist QBO_REFRESH_TOKEN to GitHub Secrets`. The stored refresh
   token is now frozen.
2. **Next run (~15 days later):** **hard failure.** Intuit rotated the refresh token
   on the previous run and invalidated the frozen one ~24h later, so the refresh is
   rejected with `invalid_grant` / `Token refresh failed (400)`. You receive the
   failure emails (`QBO reports FATAL ERROR` + the infra-level `workflow FAILED`).
   **Reports stop** until fixed.

**Recovery requires BOTH steps** — a new PAT alone is not enough, because the stored
QBO refresh token is already dead by step 2:
1. **New PAT:** create a fresh fine-grained PAT (Repository → only `qbo-pnl-reporter`;
   permissions **Secrets: Read and write**, **Metadata: Read-only**); update the
   `GH_PAT` secret.
2. **Re-bootstrap QBO tokens:** run the OAuth flow again to mint new QBO tokens, then
   update `QBO_ACCESS_TOKEN`, `QBO_REFRESH_TOKEN`, `QBO_TOKEN_EXPIRY`:
   ```bash
   # locally, with a valid .env (production client id/secret/realm)
   python auth.py                       # browser flow → new tokens in .env
   # push the three values up as secrets:
   gh secret set QBO_ACCESS_TOKEN  --body "$(grep ^QBO_ACCESS_TOKEN= .env  | cut -d= -f2- | tr -d \"'\")"
   gh secret set QBO_REFRESH_TOKEN --body "$(grep ^QBO_REFRESH_TOKEN= .env | cut -d= -f2- | tr -d \"'\")"
   gh secret set QBO_TOKEN_EXPIRY  --body "$(grep ^QBO_TOKEN_EXPIRY= .env  | cut -d= -f2- | tr -d \"'\")"
   ```
   > The initial OAuth flow needs an HTTPS redirect URI for production. The repeatable
   > approach is an ngrok tunnel pointing at the local callback server (see git
   > history / `auth.py` for the `localhost:8080/callback` flow).
3. Trigger any report manually to confirm: look for
   `Rotated tokens persisted to GitHub Secrets` in the logs and a fresh "Updated"
   timestamp on the secrets.

**Prevention (now automated):** every live run checks the PAT's own expiry — read
straight from GitHub's `github-authentication-token-expiration` response header, so
there is no separate expiry value to maintain — and emails a rotation reminder once
the PAT is within **30 days** of expiring (tunable via `GH_PAT_EXPIRY_WARN_DAYS`).
Because the schedule fires on the 1st and 16th, you get a nudge roughly every two
weeks through the window until you swap the token. Rotating *before* expiry avoids the
QBO re-bootstrap entirely — just replace the `GH_PAT` secret. Use a fine-grained PAT
scoped to this repo (Secrets: read/write, Metadata: read-only); a 1-year lifetime plus
these reminders means you only touch it once a year. The reminder is best-effort and
CI-only (it needs `GH_PAT`), so local runs never trigger it.

### Failure alerting

Any failed run emails `ALERT_EMAIL` (the operator; falls back to `EMAIL_FROM`, never the owner):
- **In-process** (`scheduler.py`): partial failures name the failed reports; fatal
  aborts include the traceback.
- **Infra-level** (`monthly_report.yml`, `if: failure()`): catches failures the
  Python process can't self-report (killed process, dependency install, the writeback
  step). May produce a second, generic notification.
- **PAT-expiry reminder** (`scheduler._check_pat_expiry`): a *pre*-failure warning —
  emails when the `GH_PAT` is within `GH_PAT_EXPIRY_WARN_DAYS` (default 30) of expiring,
  so the un-rotatable credential gets swapped before it can cause the failure above.

### Data-quality guardrails (pre-send reconciliation)

Before any email goes out, `guardrails.py` reconciles the pull **statelessly** — no
database, no stored prior run. It checks each pull against QBO's *own* authoritative
totals and the arithmetic of a P&L:

- **Identities:** `Income − COGS = Gross Profit`, `Gross Profit − OpEx = Net Operating Income`.
- **Cross-report tie:** `Σ(per-vendor COGS detail) ≈ COGS summary` (the two QBO endpoints must agree).
- **Per-report sanity:** value present, finite, and within a plausible band of income.

A report that fails is **held back from delivery — the others still send** — and the
operator gets an alert naming the held report(s) and exactly what didn't reconcile
(policy: hold only the affected report). A live run with any hold exits non-zero; a
`--dry-run` only logs what *would* be held and still writes all previews. Tolerance is
`RECON_TOLERANCE` (default `$1.00`).

**Month-to-date:** mid-month, the reporting month is the current, incomplete one — a few
days of income against a near-full month of expenses makes ratios swing wildly. Those
figures are **labelled "Month-to-date (partial)"** in the email and delivered (the ratio
sanity band is relaxed for the current month); the identity and vendor reconciliations
still apply, so genuinely broken data is still held.

**When you get a hold alert:** the data didn't reconcile — usually a QBO sync still in
progress, a mis-mapped account, or (rarely) an extraction change. Re-run after the sync
settles, or fix the account mapping; the held report sends once it ties.

### Heartbeat (dead-man's-switch)

The scariest failure is **silence** — e.g. GitHub auto-disables scheduled workflows
after 60 days of repo inactivity, so the cron just stops with no error and no email. To
catch that, a successful run pings `HEARTBEAT_URL` (and `<url>/fail` on a held/failed
run). Point it at a free monitor (e.g. **healthchecks.io**) configured to expect a ping
on the 1st and 16th; if one never arrives, *it* alerts you. Only the scheduled
all-reports workflow pings, and it no-ops when `HEARTBEAT_URL` is unset.

### Adding a new report

1. Add `reports/<name>.yaml` (copy an existing one; pick `type` + `metric`).
2. Optionally add `.github/workflows/report_<name>.yml` for manual dispatch (copy an
   existing per-report workflow; keep the `GH_PAT` env line).
3. `pytest tests/` and `python scheduler.py --force --dry-run --report "<Name>"`.

### COGS-by-Vendor accuracy

Credit-card COGS charges import without a Payee; the report derives the vendor from
the bank-feed memo (`memo_fallback: true`) and canonicalizes names via the `aliases`
map in `reports/cogs_by_vendor.yaml`. The durable source-side fix is **QBO Bank Rules**
that auto-assign a Payee by descriptor. Add new descriptors to `aliases` as suppliers
appear.

---

## Testing

`pytest` — 205 tests, all pure/mocked (no network): parsing, analytics, anomaly
logic, **report + scorecard + vendor rendering**, **scheduler orchestration**
(trigger days, dry-run, fetch-once, partial-failure alerting), **data-quality
guardrails** (reconciliation holds + heartbeat), token-expiry + refresh + 401-retry +
writeback gating, PAT-expiry parsing + reminder, memo cleanup, and email
routing/alerting. ~87% line coverage.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the full suite on
every push and pull request. For the full regression protocol — including how to
dry-run the whole pipeline against production via GitHub Actions before a deploy —
see [docs/regression-testing.md](docs/regression-testing.md).
