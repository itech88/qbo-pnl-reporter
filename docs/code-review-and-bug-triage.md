# QBO P&L Reporter — Code Review, Security Review & Bug Triage

**Reviewer:** Solution Architect
**Date:** 2026-07-12
**Scope:** Full repository — `auth.py`, `fetcher.py`, `vendor_fetcher.py`, `aging_fetcher.py`, `cash_outlook.py`, `analytics.py`, `vendor_analytics.py`, `aging_analytics.py`, `guardrails.py`, `scheduler.py`, `mailer.py`, `report.py`, `logger.py`, `reports/*.yaml`, `templates/*.html`, `.github/workflows/*`, tests, docs.
**System context:** Production, unattended financial-reporting pipeline for an optometry practice. QuickBooks Online is the ledger of record; Revolution EHR and VoIP are out of scope for this repo (no PHI enters this pipeline — it handles financial data only, which is the correct boundary and should stay that way).

---

## 1. Executive summary

The codebase is in better shape than most solo-maintained production integrations: config-driven design, ~87% test coverage with pure/mocked tests, stateless pre-send reconciliation guardrails, a dead-man's-switch heartbeat, and a genuinely well-thought-out token-writeback scheme for ephemeral runners. Jinja2 autoescaping is on, tokens are fed to `gh secret set` via stdin, and the operator/owner alert-routing separation is enforced by tests.

The review surfaced **2 P0 defects, 5 P1 defects, and a set of P2/P3 hardening and optimization items**. The two P0s are both "silent until they hurt" problems characteristic of unattended systems:

1. **The January 1 run produces empty/missing reports** (the year-boundary bug) — the single most important scheduled send of the year (December close + annual wrap) will silently degrade.
2. **A concurrent-run token-rotation race can permanently invalidate the refresh token**, triggering the exact two-stage `invalid_grant` death spiral the docs warn about — and the failed writeback that starts the spiral is currently swallowed as a green run.

Neither has bitten yet only because of timing luck. Both are cheap to fix.

---

## 2. Security review

### 2.1 What's done well (keep)

| Area | Observation |
|---|---|
| Secrets handling | Tokens never on argv (`gh secret set` via stdin); `.env` gitignored; writeback CI-gated on `GH_PAT` + `GITHUB_REPOSITORY` |
| OAuth flow | `state` parameter generated with `secrets.token_urlsafe` and verified (CSRF protection); callback binds to localhost only |
| Template injection | `autoescape=True` in the Jinja2 environment — vendor names derived from untrusted QBO memo text are escaped before rendering into HTML email |
| Wrong-cluster 401 | Fault-code-130 401s correctly do **not** trigger a token refresh, avoiding needless refresh-token rotation |
| Alert routing | Operational alerts can never reach the business owner (`ALERT_EMAIL` → `EMAIL_FROM`, never `EMAIL_TO`), enforced by tests |
| Transport | HTTPS to QBO/GitHub; SMTP with STARTTLS + `ssl.create_default_context()` |

### 2.2 Security findings

**SEC-1 · Job-level env exposes every secret to every step (supply-chain blast radius) — Medium**
`monthly_report.yml` sets `GH_PAT`, `QBO_CLIENT_SECRET`, `QBO_REFRESH_TOKEN`, and `SMTP_PASSWORD` as **job-level** env, so they are visible to `pip install` and every transitive dependency's build/import code, not just the pipeline step. A compromised PyPI package in the dependency tree can exfiltrate a PAT with `secrets:write` — which then lets an attacker rewrite *any* repo secret.
*Remediation:* (a) move the secret env block down to only the "Run pipeline" and "Notify on failure" steps; (b) add `permissions: contents: read` at the workflow level so the default `GITHUB_TOKEN` is minimal; (c) pin dependencies with hashes (`pip install --require-hashes -r requirements.txt` via `pip-compile --generate-hashes`); (d) pin actions to commit SHAs rather than mutable tags (`actions/checkout@<sha>`).

**SEC-2 · Silent writeback failure leaves a green run holding a dead refresh token — High (also RB-2)**
`_persist_to_github_secrets()` catches `CalledProcessError` / `TimeoutExpired` / `FileNotFoundError`, logs, and **returns**. The run stays green, and — exactly as the docs describe — the *next* run fails with `invalid_grant` and requires a full QBO re-consent. The docs claim the workflow-level `if: failure()` net catches "the writeback step," but the writeback is *inside* the Python process, not a separate step, so the net never sees it.
*Remediation:* on writeback failure, call `send_failure_alert(...)` immediately and exit non-zero (the report was already sent; failing the run afterward is safe and makes the problem visible on run #1, when recovery is still a 2-minute PAT/secret fix instead of a re-bootstrap).

**SEC-3 · Production OAuth bootstrap routes the authorization code through ngrok — Medium**
The documented re-bootstrap procedure tunnels the Intuit redirect (and thus the one-time authorization code) through a third-party ngrok domain. The code is short-lived and the token exchange itself is direct-to-Intuit with the client secret, so exposure is bounded — but a free-tier ngrok URL is guessable/loggable by a third party.
*Remediation:* register a stable HTTPS redirect URI you control (a one-page Cloudflare Pages/Worker or an `https://localhost`-style loopback per Intuit's current policy), or a reserved ngrok domain on a paid plan at minimum. Document it in the runbook.

**SEC-4 · Dry-run preview artifacts contain real production financials — Low/Medium**
`preview_*.html` (full P&L, vendor spend, A/R by patient/payer names) is uploaded as a workflow artifact with 3-day retention. Anyone with read access to the repo (including a future collaborator, or a leaked PAT with repo read) can download them.
*Remediation:* confirm the repo is private and access is owner-only; consider `retention-days: 1` and note in the runbook that previews are sensitive. If A/R aging previews name patients, treat them as containing personal financial information.

**SEC-5 · Plaintext credentials in local `.env` — Low (accepted risk, document it)**
Client secret, refresh token, and the Gmail app password live in plaintext on the operator laptop. Acceptable for a solo operation *if* the disk is encrypted (FileVault/BitLocker) and the machine is not shared.
*Remediation:* document the assumption; optionally move to `direnv` + OS keychain.

**SEC-6 · Token refresh failure logs the full response body — Info**
`refresh_tokens()` logs `response.text` on non-200. Intuit error bodies don't contain secrets today, but logging raw bodies from an auth endpoint is a habit worth breaking.
*Remediation:* log status + `intuit_tid` + the Intuit error `code` only.

**SEC-7 · Manual heartbeat feeding — Info**
A manual dispatch of `monthly_report.yml` also pings `HEARTBEAT_URL`, which can mask a dead cron (the monitor sees a ping even though the schedule never fired). Minor because manual runs are rare.
*Remediation:* gate the ping on `github.event_name == 'schedule'` (pass it as an env flag).

---

## 3. Reliability & correctness review

**RB-1 · Year-boundary bug: the Jan 1 run reports an empty year — HIGH**
`mom_analysis`, `current_month_stats`, and `report.py`'s "current year" logic all key on `datetime.now().year`. On **January 1**, the current year has zero income rows, so: the scorecard's `current_month_stats` returns `None` → the annual dashboard is *skipped*; every metric report renders an empty "Month-by-Month" table ("No data for 2027 yet"); the December close — the most valuable send of the year — is never reported. `report.py` additionally freezes `_CURRENT_YEAR` at *import* time, which is a second, independent source of drift if the reporting basis is ever fixed.
*Remediation:* define the **reporting year** as "the year of the latest month with income," not the calendar year of the run (mirroring the prior-month fallback already used mid-month). Add a regression test that mocks `now = Jan 1` and asserts December is reported.

**RB-2 · Concurrent runs race on refresh-token rotation — HIGH**
There is no `concurrency:` group across `monthly_report.yml` and the ten per-report `report_*.yml` workflows. Two overlapping runs (e.g., a manual dispatch while the cron is running, or two manual dispatches) can each refresh: run A rotates the token, run B refreshes with the now-stale token (Intuit tolerates a ~grace window, then rejects), or B's writeback overwrites A's newer token with an already-retired one. Either way the next scheduled run hits `invalid_grant` and requires a full re-bootstrap.
*Remediation:* add the same `concurrency: { group: qbo-token-chain, cancel-in-progress: false }` block to **every** workflow that can refresh tokens, so runs queue instead of overlapping. One line per file; eliminates the whole class.

**RB-3 · Empty-string secrets crash the pipeline at import — HIGH**
GitHub injects **empty strings** for referenced-but-unset secrets. `analytics.py` executes `float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))` at *module import* — an empty string raises `ValueError` and aborts the entire run before anything executes. Same pattern in the scheduler's scorecard block, `int(os.getenv("SMTP_PORT", "587"))`, and `EMAIL_PROVIDER` (empty → `""` → "Unknown EMAIL_PROVIDER"). `guardrails.tolerance()` already handles this correctly with the `os.getenv(...) or "default"` idiom — the fix is to apply that idiom everywhere.
*Remediation:* a small `env_float/env_int/env_str` helper module using the `or` pattern; replace all direct `float(os.getenv(...))` reads. Also stop reading `_THRESHOLD` at import time (read at call time) so tests and reruns see env changes.

**RB-4 · SES backend silently drops the chart (broken image in every email) — MEDIUM (latent)**
`_send_ses` uses `send_email` with an HTML body only — no attachment support — while the template embeds `cid:monthly_chart`. Any switch to the SES provider ships emails with a broken image. Tests mock the backend and never catch it.
*Remediation:* switch to `send_raw_email` with the same MIME structure as `_build_mime`, or delete the SES backend if it will never be used (dead, wrong code is worse than no code).

**RB-5 · Alerting is SMTP-only regardless of `EMAIL_PROVIDER` — MEDIUM**
`send_failure_alert` requires SMTP credentials. If the deployment ever moves reports to SendGrid/SES and drops the SMTP secrets, *all* failure/hold/PAT alerts silently no-op (returns `False`) — the safety net disappears exactly when the config changed. Today it works because Gmail SMTP is the provider; the coupling is undocumented.
*Remediation:* route alerts through the configured provider, or explicitly document "SMTP credentials are mandatory even if reports use another provider" and fail loudly at startup if absent.

**RB-6 · Naive-datetime `QBO_TOKEN_EXPIRY` crashes the expiry check — LOW**
`_is_token_expired` catches `ValueError` but not `TypeError`: a manually-set expiry string without a timezone offset parses to a naive datetime, and comparing it to `datetime.now(timezone.utc)` raises `TypeError`, aborting the run. Plausible during the documented manual recovery procedure (a human pastes the value).
*Remediation:* normalize (`if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)`) or catch `(ValueError, TypeError)` → treat as expired.

**RB-7 · No retry/backoff for 429 / 5xx from QBO — LOW/MEDIUM**
`QBOSession` handles 401 elegantly but a transient 429 (rate limit) or 502 aborts the whole run via `raise_for_status()`. Twelve calls per run keeps rate-limit risk low, but transient 5xx from Intuit is a known occurrence.
*Remediation:* mount a `urllib3.Retry` adapter (e.g., 3 retries, backoff, on 429/500/502/503/504, respecting `Retry-After`) on `QBOSession`.

**RB-8 · GitHub auto-disables cron after 60 days of repo inactivity — LOW (mitigated)**
The heartbeat catches it after the fact; the miss still costs a reporting cycle.
*Remediation:* add a monthly keepalive workflow, or a step that calls `gh api ... /actions/workflows/monthly_report.yml/enable` — cheap insurance on top of the heartbeat.

**RB-9 · DST drift on the cron — INFO (documented, accepted)**
`0 18 1,16 * *` fires 10 AM PST / 11 AM PDT. Already noted in a comment; no action unless the owner cares about the hour.

---

## 4. Code-quality review

**CQ-1 · Unclosed file handles in config loading — LOW**
`load_report_configs` does `yaml.safe_load(open(p))` in a list comprehension. CPython GC saves you today; it's still the wrong idiom.
*Fix:* `with open(p) as f: yaml.safe_load(f)`.

**CQ-2 · `_build_chart` prior-year column derivation is self-flagged as messy — LOW**
The string-munging over `yoy_df` column names (with an inline "Cleaner: just derive prior years…" comment) is fragile against column renames.
*Fix:* pass the year list explicitly from `yoy_analysis` (it already knows it) instead of re-deriving from column names.

**CQ-3 · Duplicated env plumbing across 11 workflow files — LOW**
The same ~18-line secret env block is copy-pasted into `monthly_report.yml` + 10 `report_*.yml` files; a new secret means 11 edits (and the per-report files already drift — e.g., they omit `HEARTBEAT_URL`, which happens to be correct, but by accident).
*Fix:* convert the per-report workflows to thin wrappers around a single reusable workflow (`workflow_call`) that owns the env block.

**CQ-4 · Doc drift — LOW**
(a) Regression doc says "205 tests" in one section and "179/179" in the acceptance criteria. (b) The claim that the infra-level `failure()` net catches "the writeback step itself" is false (see SEC-2). (c) README recovery snippet's `grep/cut/tr` parsing of `.env` is brittle against quoted values with `=` in them — prefer `python -c "from dotenv import dotenv_values; ..."`.
*Fix:* one doc pass after the P0/P1 fixes land, since several fixes change the documented behavior.

**CQ-5 · `revoke_tokens` ignores the revoke response status — LOW**
A failed revoke looks identical to a successful one. Check status and print/log it.

**CQ-6 · Scorecard threshold read duplicates the analytics default — INFO**
`float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))` appears in both `analytics.py` (import-time) and `scheduler.py` (call-time). Consolidate into one accessor (also resolves half of RB-3).

---

## 5. Optimization opportunities (the stated goal)

**OPT-1 · Halve the vendor-detail fetch.** `fetch_vendor_raw_all` pulls 3 years × 2 halves of `ProfitAndLossDetail` (the heaviest endpoint, 40s timeouts) every run, but vendor YoY only needs the comparison months. Fetching current year + the same-month windows of prior years (or current + 1 prior year) cuts the slowest calls ~50–66% and shrinks the guardrail surface. Low risk; verify vendor-YoY still renders.

**OPT-2 · Skip the P&L fetch on runs that need none of it.** Already gated via `collect_cfgs` — confirmed good; no change. Noted so nobody "optimizes" it into a regression.

**OPT-3 · Reuse one `QBOSession` across fetchers.** `fetcher`, `vendor_fetcher`, and `aging_fetcher` each call `get_session()`; each construction re-checks expiry. Pass a single session from the scheduler — fewer objects, one expiry decision per run, and it makes RB-2's single-writer property easier to reason about in-process.

**OPT-4 · Cache pip more aggressively / prune deps.** `boto3` is imported lazily but `sendgrid` sits in `requirements.txt` while the deployment is SMTP-only. Removing unused providers (see RB-4) shrinks install time and the supply-chain surface (compounds with SEC-1).

**OPT-5 · Chart rendering.** `dpi=150` on a 9×4" figure produces a large inline PNG per email. `dpi=110` is visually equivalent in Gmail at 576px display width and cuts payload ~45%. Cosmetic; test on a real device before shipping.

---

## 6. Bug triage — prioritized backlog

Priorities: **P0** fix before the next scheduled send · **P1** fix this sprint · **P2** schedule within the quarter · **P3** opportunistic.

| ID | Pri | Title | Type | Effort | Fix summary |
|---|---|---|---|---|---|
| RB-2 | **P0** | Token-rotation race across concurrent workflows | Reliability | XS (1 line × 11 files) | `concurrency: qbo-token-chain` in every token-touching workflow |
| SEC-2 | **P0** | Writeback failure is swallowed → delayed `invalid_grant` lockout | Security/Reliability | S | Alert + exit non-zero on writeback failure; fix doc claim |
| RB-1 | **P1** | Jan 1 run reports empty year; December close never sent | Correctness | M | Reporting year = latest month with income; regression test w/ mocked Jan 1 |
| RB-3 | **P1** | Empty-string secrets crash at import (`float("")`) | Reliability | S | `env_*` helpers with `or`-default idiom; kill import-time reads |
| SEC-1 | **P1** | Job-level secrets + unpinned deps/actions = supply-chain blast radius | Security | M | Step-scoped env, `permissions:` block, hash-pinned deps, SHA-pinned actions |
| RB-5 | **P1** | Alerts silently vanish if SMTP creds ever dropped | Reliability | S | Startup check or provider-routed alerts; document coupling |
| RB-4 | **P1** | SES backend ships broken chart image | Correctness | S | `send_raw_email` w/ MIME, or delete SES backend |
| SEC-3 | **P2** | OAuth bootstrap via ngrok tunnel | Security | S | Controlled HTTPS redirect URI; update runbook |
| RB-6 | **P2** | Naive expiry datetime raises `TypeError` | Reliability | XS | Normalize tz / catch `TypeError` → expired |
| RB-7 | **P2** | No retry/backoff on 429/5xx | Reliability | S | `urllib3.Retry` adapter on `QBOSession` |
| SEC-4 | **P2** | Preview artifacts hold real financials | Security | XS | 1-day retention; access review; runbook note |
| RB-8 | **P2** | Cron auto-disable after 60-day inactivity | Reliability | S | Keepalive workflow / re-enable API call |
| OPT-1 | **P2** | Vendor detail over-fetch (slowest calls) | Performance | S | Narrow fetch window; verify YoY render |
| CQ-3 | **P2** | 11× duplicated workflow env blocks | Maintainability | M | Reusable `workflow_call` workflow |
| SEC-7 | **P3** | Manual runs feed the dead-man's switch | Reliability | XS | Ping only on `schedule` events |
| CQ-1 | **P3** | Unclosed file handles in config loader | Quality | XS | Context manager |
| CQ-2 | **P3** | Fragile prior-year derivation in chart builder | Quality | S | Pass year list explicitly |
| CQ-4 | **P3** | Doc drift (test counts, writeback-net claim, `.env` grep) | Docs | S | Doc pass after P0/P1 land |
| CQ-5 | **P3** | `revoke_tokens` ignores response status | Quality | XS | Check + log status |
| SEC-5 | **P3** | Plaintext local `.env` | Security | XS | Document disk-encryption assumption |
| SEC-6 | **P3** | Auth error bodies logged raw | Security | XS | Log status/tid/error-code only |
| OPT-3 | **P3** | One shared `QBOSession` per run | Performance | S | Inject session from scheduler |
| OPT-4 | **P3** | Unused sendgrid dep / provider pruning | Maintainability | XS | Trim requirements (pair with RB-4) |
| OPT-5 | **P3** | Chart PNG payload size | Performance | XS | dpi 150 → ~110; visual QA |
| RB-9 | — | DST hour drift (accepted) | Info | — | No action |
| SEC-4b| — | No PHI in pipeline (verified boundary) | Info | — | Keep Revolution EHR data out of this repo |

### Suggested sequencing

1. **Before the 16th send (this week):** RB-2, SEC-2. Both are tiny and remove the only failure modes that require a full QBO re-bootstrap to recover from.
2. **Sprint 1:** RB-1 (with the mocked-Jan-1 regression test), RB-3, SEC-1, RB-5, RB-4. Run the full pre-deploy checklist (pytest → CI → dry-run → preview verification) after each.
3. **Sprint 2:** the P2 block, leading with SEC-3 and RB-7 (both reduce operator toil), then the workflow-consolidation refactor (CQ-3) *last* — it touches all 11 files and should land on an otherwise-quiet base.
4. **Opportunistic:** P3 items as ride-alongs on related changes; never as standalone deploys the day before a 1st/16th.

### Verification gate (unchanged, reaffirmed)

Every fix above must pass the existing protocol before it's trusted: local `pytest` green → CI green on the pushed commit → `gh workflow run monthly_report.yml -f dry_run=true` succeeds → previews spot-checked against QuickBooks. RB-1 and RB-3 additionally require new unit tests (mocked Jan-1 date; empty-string env vars) so these classes of regression are permanently caught by Layer 1.
