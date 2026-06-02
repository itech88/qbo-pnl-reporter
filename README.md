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
| **Monthly Business Dashboard** | scorecard (1st only) | traffic-light vs 3-yr avg |

Schedule: the 8 metric reports + COGS by Vendor send on the **1st and 16th**; the
scorecard sends on the **1st only**. Cron is `0 18 1,16 * *` (18:00 UTC = 10 AM
PST / 11 AM PDT).

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
| `analytics.py` | MoM, YoY, anomaly flagging, scorecard stats |
| `vendor_analytics.py` | Vendor share breakdown, monthly matrix, vendor YoY |
| `report.py` | Renders HTML + matplotlib charts (report, scorecard, vendor) |
| `mailer.py` | SMTP / SendGrid / SES delivery (CID inline chart); `send_failure_alert` |
| `scheduler.py` | Entry point: load configs, fetch once, run each report, alert on failure |
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
`SMTP_PASSWORD`, `COGS_VARIANCE_THRESHOLD`, and **`GH_PAT`** (see below).

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

**Prevention:** set a calendar reminder ~1 month before the PAT's expiry to rotate it
proactively (rotating before it dies avoids the QBO re-bootstrap entirely — just swap
the PAT). Consider a 1-year PAT and a recurring reminder.

### Failure alerting

Any failed run emails `EMAIL_TO`:
- **In-process** (`scheduler.py`): partial failures name the failed reports; fatal
  aborts include the traceback.
- **Infra-level** (`monthly_report.yml`, `if: failure()`): catches failures the
  Python process can't self-report (killed process, dependency install, the writeback
  step). May produce a second, generic notification.

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

`pytest tests/` — 120 tests, all pure/mocked (no network): parsing, analytics,
anomaly logic, token-expiry + writeback gating, memo cleanup, and alerting.
