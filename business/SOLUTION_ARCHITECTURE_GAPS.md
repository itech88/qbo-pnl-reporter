# Solution Architecture — Gap Analysis: Single Deployment → Sellable Single-Client Product

**Author role:** Solution Architect
**Scope:** What it takes to turn today's `qbo-pnl-reporter` (one hard-wired deployment for
one optometry practice) into a **locally-hosted, single-client product** an accounting firm
can buy, run for *one* of their clients, and look better doing it.
**Companion:** `BUSINESS_PLAN.md`
**Date:** 2026-06-07

> **Framing.** This document deliberately stops at *single-client, locally-hosted, sold to a
> firm*. That is the smallest sellable unit and the right first commercial step — it
> validates willingness-to-pay before we pay for multi-tenant SaaS engineering. Where a gap's
> "right" answer is really a multi-tenant decision, it's flagged but de-scoped to the
> minimum that ships a clean single-client product.

---

## 1. Where the system is single-tenant today (the honest baseline)

The codebase is well-built but **structurally bound to exactly one practice and one
operator**. Concretely, from the current code:

| Coupling point | Evidence in code | Why it blocks productization |
|---|---|---|
| **Credentials live in one repo's GitHub Secrets** | `auth._persist_to_github_secrets()` writes `QBO_*` back via `gh secret set --repo $GITHUB_REPOSITORY` | Every client needs its own secret store; a buyer can't be handed a GitHub repo + PAT. This is the deepest coupling. |
| **Runtime *is* GitHub Actions** | README architecture; `monthly_report.yml`; writeback gated on `GH_PAT`+`GITHUB_REPOSITORY` | "Locally hosted for a firm" means it can't depend on the operator's GitHub account as the execution + secret-storage substrate. |
| **One QBO realm, one set of tokens** | `auth.py` reads single `QBO_REALM_ID`, `QBO_ACCESS_TOKEN`, `QBO_REFRESH_TOKEN` from `.env`/env | No notion of "client A vs client B." Adding a client today = cloning the whole repo. |
| **Recipient + account mapping hard-coded per report** | `reports/cogs.yaml`: `email_to: mtbpalmdale@gmail.com`, `extraction.group: COGS` | Reports encode *this* practice's chart-of-accounts names and *this* owner's email. Another practice's COA won't match. |
| **One OAuth client / redirect URI** | `auth.initial_auth_flow()` uses single `QBO_CLIENT_ID`/`QBO_REDIRECT_URI`; ngrok for HTTPS | Onboarding a non-technical firm can't require an ngrok tunnel + browser dance per client. |
| **One operator alert channel** | `ALERT_EMAIL` / `EMAIL_FROM` single operator identity | A firm reselling under its brand needs *its* alert + *its* sending identity, distinct from ours. |
| **Branding is fixed** | `templates/*.html`, matplotlib palette in `report.py` (`#2563eb` etc.) | No firm logo / color / sending-domain story. White-label is a sales requirement. |

**Net:** the *analytics, rendering, and guardrails are product-ready*. The *identity,
credential, runtime, and configuration layers are single-tenant scaffolding.* The work is
almost entirely in those four layers, **not** in the financial logic.

---

## 2. Target architecture (single-client, locally hosted)

A self-contained unit a firm runs for one client, with no dependency on *our* GitHub:

```
┌─ Firm's machine / a small VM (Docker) ────────────────────────────┐
│                                                                    │
│  scheduler (cron OR a tiny scheduler loop)                         │
│     │  loads client_profile.yaml  (one client)                     │
│     ├─ auth.py  ──── token store: local encrypted file / OS keyring │
│     │                refresh + rotation handled in-process         │
│     ├─ fetcher / vendor_fetcher ── QBO API (our production app)    │
│     ├─ analytics / vendor_analytics                                │
│     ├─ guardrails  (unchanged — the crown jewel)                   │
│     ├─ report.py (branding from client_profile: logo, colors)      │
│     └─ mailer ── firm's SMTP / sending domain ── client inbox      │
│                                                                    │
│  health: local heartbeat → healthchecks.io (unchanged pattern)    │
└────────────────────────────────────────────────────────────────────┘
```

Key shifts from today:
1. **Decouple secret storage from GitHub.** Replace `_persist_to_github_secrets` with a
   pluggable token store (local encrypted file via `cryptography.fernet`, or OS keyring).
   The *logic* (refresh, rotate, write-back-the-rotated-token) is already correct — only the
   **sink** changes.
2. **Decouple runtime from GitHub Actions.** Ship as a **Docker container** with an internal
   scheduler (or a documented host cron). The firm runs `docker run`, not "fork our repo."
3. **One `client_profile.yaml`** that carries everything practice-specific: realm, email
   recipient, COA account-name mappings, branding, schedule. The 10 `reports/*.yaml` become
   *templates* parameterized by the profile, not per-practice files.
4. **Guided OAuth onboarding** that a non-engineer can complete (see §5, the hard part).

---

## 3. Gap inventory with effort estimates

Effort = focused solo engineering days for someone fluent in this codebase. Ranges reflect
"make it work" → "make it sellable/robust."

| # | Gap | Work | Effort | Priority |
|--:|---|---|---:|---|
| G1 | **Token store decoupling** | Abstract a `TokenStore` interface; implement local-encrypted-file + keyring backends; replace `_persist_to_github_secrets` sink; keep refresh/rotation logic | **3–5 d** | P0 |
| G2 | **Runtime packaging** | Dockerfile; internal scheduler (APScheduler) or documented cron; config + secrets mounted as volume; healthcheck wiring | **3–4 d** | P0 |
| G3 | **Client profile abstraction** | Collapse practice-specific fields out of `reports/*.yaml` into one `client_profile.yaml`; parameterize report templates; remove hard-coded `email_to`/account names | **3–5 d** | P0 |
| G4 | **Guided OAuth onboarding** | Replace ngrok dance with a small local web onboarding flow (localhost callback wrapped in a friendly UI) + step-by-step doc; handle realm capture | **4–7 d** | P0 (the risk — §5) |
| G5 | **Chart-of-accounts mapping tool** | A discovery step that lists the client's actual QBO accounts and maps them to our report templates (their "COGS" may be "Cost of Goods Sold — Optical") | **4–6 d** | P0 |
| G6 | **Branding / white-label** | Firm logo + color tokens + sending-domain config flowing into Jinja templates and matplotlib palette | **2–4 d** | P1 |
| G7 | **Operator/alert identity split** | Firm-level alert + sending identity distinct from report recipient (partially present via `ALERT_EMAIL`); generalize | **1–2 d** | P1 |
| G8 | **Install & ops UX for non-engineers** | One-command install, `.env`/profile validation with friendly errors, "test run" mode (the existing `--dry-run` is a great base), upgrade path | **3–5 d** | P1 |
| G9 | **Security hardening** | Encryption at rest for tokens, least-privilege QBO scopes, no-secrets-in-logs audit (logging already scrubs PAT), secret-file permissions | **2–4 d** | P1 |
| G10 | **Intuit production app approval** | Submit app for production (exceed dev-tier 25-company limit), meet security questionnaire, privacy policy, branding assets | **2–5 d work + weeks of review latency** | P0 (long pole, mostly waiting) |
| G11 | **Licensing / update / telemetry** | License key or simple entitlement; opt-in failure telemetry so we can support remotely | **2–4 d** | P2 |
| G12 | **Onboarding automation polish** | Drive G4+G5 toward < 30 min/client (templated COA maps per common optometry QBO setups) | **3–5 d** | P2 |

**Totals**
- **Minimum sellable single-client product (P0 only, excl. Intuit latency):** **≈ 20–32
  engineering days** → ~**4–7 calendar weeks** solo, part-time realistic **8–12 weeks**.
- **Polished, low-support sellable product (P0+P1):** **≈ 28–46 days** → **6–10 weeks**
  focused.
- **Intuit production review** runs *in parallel* but gates go-live: start it **first**; its
  calendar latency (often **2–6+ weeks**) usually dominates the schedule.

**Critical path:** G10 (start immediately, it's mostly waiting) ∥ G1 → G3 → G4 → G5 → G2.

---

## 4. What does *not* need to change (and why that's the good news)

- **`guardrails.py`** — the pre-send reconciliation is the differentiator and is already
  stateless and client-agnostic. **Zero changes.** This is the moat; it ships as-is.
- **`analytics.py` / `vendor_analytics.py`** — pure pandas, config-driven. No client coupling.
- **`report.py` rendering** — needs only branding parameterization (G6), not restructuring.
- **`fetcher.py` / `vendor_fetcher.py`** — already realm-agnostic given correct tokens.
- **The refresh / rotation *logic* in `auth.py`** — correct and battle-tested; only the
  *persistence sink* (`_persist_to_github_secrets`) is GitHub-specific and must be swapped.
- **Test suite (205 tests, mocked)** — a strong safety net for the refactor; the abstraction
  work (G1, G3) is exactly the kind of change a good mocked suite de-risks.

**Implication:** ~70% of the codebase by value (logic, rendering, reconciliation, tests) is
reusable untouched. The productization cost is concentrated in **identity, credentials,
runtime packaging, and onboarding** — scaffolding, not science.

---

## 5. The hardest integration gap — and the post-sale headache it creates

### 5.1 The hard part: **QBO OAuth onboarding + refresh-token lifecycle, in a place we don't control**

Everything else is ordinary refactoring. **This one is genuinely hard and it's where a sale
goes sour months later, silently.** Three compounding problems:

**(a) Onboarding a non-engineer through OAuth.**
Today's flow (`auth.initial_auth_flow`) opens a browser, runs a localhost callback server,
and — for production HTTPS redirect — *requires an ngrok tunnel*. That is fine for the
builder and **impossible to hand to a bookkeeper**. The redirect URI is registered on **our**
Intuit app, so we either:
- host a hosted callback endpoint (re-introduces a server we must run — drifts toward
  multi-tenant), or
- ship a guided local flow (localhost callback + clear UI) and register `http://localhost`
  redirect on the production app.

The localhost route is the right single-client answer, but it's fiddly across the firm's OS,
browser, firewall, and port conflicts. **Budget real time for cross-environment edge cases.**

**(b) The refresh-token rotation trap — the post-sale time bomb.**
This is the headache that bites *after* the sale, exactly as the existing README's GH_PAT
section documents for the current deployment. Intuit's model:
- Access token: ~60 min.
- **Refresh token rotates on a ~24h rolling window and is only valid 100 days.**

The whole reason `_persist_to_github_secrets` exists is that **the rotated refresh token must
be durably written back after every run, or a later run fails with `invalid_grant`.** In the
current GitHub deployment, if the write-back credential (the PAT) lapses, the **first failure
is silent and green** — the report still sends — and the *hard* failure arrives ~15 days
later. (See README "Timeline of failure.")

Now relocate that exact failure mode into a firm's environment:
- If the local token store isn't durably persisted (container without a mounted volume, a
  machine that's reimaged, a "I copied the folder but not the hidden `.env`" move, a backup/
  restore that rolls the token file back, two copies of the container both refreshing and
  **racing to rotate the same refresh token** — one invalidates the other), then:
  - **It works in the demo and the first few runs.**
  - **Then, ~15–100 days later, it dies with `invalid_grant` and reporting stops** — with no
    obvious cause the firm can diagnose, long after onboarding, blamed on us.

This is the single most likely **post-sale support nightmare**: *intermittent, delayed,
environment-dependent, and silent until it's a hard stop.* It is exactly the class of bug the
current README spends 60 lines on for one deployment — and we'd be multiplying it across every
firm's uncontrolled machine.

**(c) Re-auth recovery in someone else's hands.**
When tokens *do* die (PAT-equivalent lapse, 100-day expiry with a long reporting gap,
user deauthorizes the app in QBO), recovery requires re-running the OAuth flow — i.e. the
hard onboarding step again, **performed by a non-technical firm under failure pressure.**

### 5.2 Why this specifically endangers clients post-sale
- The failure is **time-delayed and silent** (green runs that quietly freeze the token),
  so it escapes acceptance testing and the demo entirely.
- It is **environment-dependent** (volumes, backups, machine moves, duplicate runners) — i.e.
  it depends on *the firm's* infrastructure habits, which we don't control.
- It manifests as **"the reports just stopped"** weeks later — the worst support ticket:
  high-stakes, low-information, reputationally damaging, and easy to misattribute.

### 5.3 Mitigations (must-haves before selling, not nice-to-haves)
1. **Make the token store the single durable source of truth, and verify it.** On every run,
   assert the rotated refresh token was persisted *and re-read it back*; abort loudly if not
   (mirror the discipline already applied to the GitHub write-back).
2. **Single-writer guarantee.** A lock / single-instance check so two containers can't race
   to rotate. Document "exactly one runner per client" as a hard constraint.
3. **Proactive expiry + staleness alarms.** Reuse the existing PAT-expiry-reminder pattern
   (`_check_pat_expiry`) for the QBO refresh token: warn the operator *before* the 100-day
   wall and on any failed write-back — turn the silent freeze into an early, loud nudge.
4. **Dead-man's-switch by default.** The heartbeat (`_ping_heartbeat` → healthchecks.io)
   already catches "it silently stopped." Make it **mandatory and pre-configured** at
   onboarding, not optional. This is the backstop that converts a silent multi-week outage
   into a same-day alert.
5. **One-click re-auth.** A `reauth` command that re-runs the guided flow and overwrites the
   token store, written for a non-engineer, with a printable recovery doc.
6. **Strongly prefer we host the credential layer.** The cleanest long-term answer is that
   *we* hold tokens server-side and the firm's local runner calls *our* token broker — but
   that crosses into the multi-tenant build this phase intentionally defers. For the
   single-client product, items 1–5 are the pragmatic guardrails.

> **Architect's bottom line:** the analytics and the reconciliation guardrails are sellable
> today. The thing that will actually determine whether clients stay happy six months after
> the sale is **the QBO refresh-token lifecycle running durably in an environment we don't
> control.** Engineer that — durable verified token store, single-writer, proactive expiry
> alarms, mandatory heartbeat, one-click re-auth — *before* the first paid install, or the
> first few sales will each turn into a silent, delayed, hard-to-diagnose outage that lands
> as a support fire weeks later.

---

## 6. Recommended sequencing

1. **Day 0:** submit the Intuit production app (G10) — it's the long pole and pure latency.
2. **Weeks 1–3:** G1 (token store) + G3 (client profile) + G2 (Docker) — the decoupling spine.
3. **Weeks 3–5:** G4 + G5 (onboarding + COA mapping) — the hard, demo-critical, client-facing part.
4. **Weeks 4–6:** §5.3 mitigations (durable verified token store, single-writer, expiry
   alarms, mandatory heartbeat, re-auth) — **gate the first sale on these.**
5. **Weeks 5–7:** G6–G9 (branding, identity split, install UX, security) — sellability polish.
6. **Then:** one paid pilot install with a friendly firm; watch a full token-rotation cycle
   (≥ 2 reporting periods) before declaring it shippable.

**Reuse credit:** because guardrails, analytics, rendering, and the 205-test suite carry over
untouched, this is a **scaffolding-and-onboarding project, not a rewrite** — the estimate is
weeks, and the risk is concentrated almost entirely in §5.
