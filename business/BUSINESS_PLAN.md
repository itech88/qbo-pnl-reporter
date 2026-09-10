# Business Plan — Automated QBO Reporting for Optometry-Focused Accounting Firms

**Working name:** *LensLedger* (placeholder — a vertical-specific brand beats a generic one)
**Model:** Boutique managed service / white-label B2B2B
**Document owner:** Founder
**Status:** v1 strategy draft
**Date:** 2026-06-07

---

## 0. Executive Summary

We sell a **done-for-you, white-labelled financial-reporting service** to bookkeepers and
accountants who serve **optometry practices**. They already hold the client relationship,
the QuickBooks Online (QBO) access, and the trust. What they lack is the time and the
engineering to turn QBO data into **timely, reconciled, branded reports** at scale across a
book of 10–40 clients.

We are not selling software they have to run. We run it. They put their logo on the output
and look more sophisticated to their clients. The wedge is a proven pipeline (already live
for one optometry practice) whose differentiator is **trustworthy delivery** — pre-send
reconciliation that refuses to email wrong numbers — not dashboards.

- **Beachhead:** independent bookkeeping/accounting firms with 5–40 optometry clients.
- **Pricing:** firm pays per active client location, **$25–$60/client/month**, billed to
  the firm (not the practice). Firm marks it up or bundles it into their advisory fee.
- **Year-1 target:** 8 firms × ~15 clients avg = ~120 client-locations ≈ **$4.5k–$7k MRR**
  on nights/weekends; one founder.
- **Why now:** Intuit is pushing accountants toward "advisory services" but gives them no
  good automated, reconciled, branded reporting layer. We fill that gap in one vertical.

---

## 1. The Problem (and whose problem it is)

### 1.1 The end-client's problem (optometry practice owner)
- Owner is a clinician, not a CFO. Logs into QBO rarely; can't read a P&L fluently.
- Wants 3 answers monthly: *Am I making money? Where is it leaking? Is this month normal?*
- Optometry has a **distinct cost structure** the generic tools ignore: frames vs. lenses
  vs. contact-lens COGS, lab fees, doctor payroll vs. optical-staff payroll, insurance/VSP
  reimbursement timing, and a high-COGS optical dispensary bolted onto a service business.

### 1.2 The firm's problem (our actual buyer)
- A bookkeeper with 20 optometry clients **cannot hand-build 20 branded report packs each
  month** — it's hours per client in Excel or QBO's clumsy custom reports.
- QBO's native scheduled reports are ugly, un-branded, not reconciled, and not
  optometry-aware. Sending them makes the firm look like a passthrough, not an advisor.
- The firm wants to **move up the value chain** (Intuit's whole "advisory" push) and bill
  more, but has no leverage to deliver advisory-grade output at scale.
- **This is the gap we monetize:** we are the firm's invisible reporting department.

### 1.3 Why "sell to tiny businesses directly" is the wrong wedge
Direct-to-practice means lowest LTV, highest price sensitivity, highest support burden, and
a brutal one-at-a-time OAuth onboarding. Selling **through the firm** inverts every one of
those: one sale lands 15 clients, the firm absorbs tier-1 support, and the firm already
holds QBO credentials. **The firm is the channel and the moat.**

---

## 2. Solution & Value Proposition

A managed pipeline that, per client location, on a fixed schedule:
1. Pulls QBO P&L (summary + vendor detail) via the Intuit API.
2. Computes MoM / YoY / 3-year-average metrics, flags anomalies.
3. **Reconciles before sending** — P&L identities (`Income − COGS = Gross Profit`),
   cross-foots vendor detail to the COGS summary, sanity-bands every figure. A report that
   doesn't tie is **held, not sent**, and the firm is alerted. *No client ever receives a
   wrong number with the firm's logo on it.*
4. Emails a branded HTML report pack — **in the firm's name**, to the practice or to the
   firm for review-then-forward.

**Three crisp value props to the firm:**
- **Look bigger than you are.** Advisory-grade, optometry-specific reporting under your
  brand, with zero monthly labor.
- **Never get embarrassed.** Reconciliation guardrails mean you don't send a client a
  number that's wrong because a QBO sync was mid-flight.
- **Bill more.** Bundle into a higher advisory tier; our cost is a line item, the markup is
  yours.

**Positioning statement:** *For accounting firms with optometry clients, LensLedger is the
white-label reporting department that delivers reconciled, optometry-aware financial reports
under your brand — so you deliver advisory value at scale without building software or
spending a single evening in Excel.*

---

## 3. Market Analysis

### 3.1 Sizing (top-down → bottom-up)
- ~**~40,000** optometry practice locations in the US (independent + small chains).
- Most independents use an outside bookkeeper/accountant. Estimate **8,000–12,000 firms**
  touch at least one optometry practice; a meaningful subset have a *cluster* (vertical
  specialization is common in accounting — firms that "get" one industry attract more of it).
- **SAM (firms with optometry clusters, our real target):** ~1,000–2,000 firms.
- **SOM (year 1–3, realistically reachable solo/small):** 20–60 firms.

Bottom-up revenue math (per firm/month at $40/client blended):

| Firms | Avg clients/firm | Client-locations | MRR @ $40 | ARR |
|------:|-----------------:|-----------------:|----------:|----:|
| 8     | 15               | 120              | $4,800    | $57.6k |
| 25    | 18               | 450              | $18,000   | $216k |
| 60    | 20               | 1,200            | $48,000   | $576k |

This is a **lifestyle-to-small-business** ceiling, not a venture outcome — but it's real,
defensible, and reachable without outside capital.

### 3.2 Competitive landscape

| Competitor | What they do | Why we win in this niche |
|---|---|---|
| **Fathom / Spotlight / Syft** | Polished multi-client reporting for accountants | Horizontal & dashboard-first ("log into our app"). Not optometry-aware, no optometry COGS model, and **no pre-send reconciliation hold** — they'll happily render bad data. We are vertical + email-native + trust-first. |
| **QBO native scheduled reports** | Free, built-in | Ugly, un-branded, un-reconciled, generic. The firm looks like a passthrough. |
| **Excel / Google Sheets by hand** | The status quo | Hours/client/month of skilled labor. We remove the labor. |
| **Bench / Pilot / outsourced bookkeeping** | Full bookkeeping | Different job (they *do the books*); they're a potential **partner/channel**, not a competitor. |
| **A practice's own IT / a freelancer** | Bespoke scripts | No reconciliation discipline, bus-factor of one, breaks on token expiry. We productize the ops. |

**Key insight:** the incumbents compete on *dashboards*. We compete on *trust + vertical
fit + zero-effort delivery*. Different axis, defensible in the niche.

### 3.3 Why optometry first
- Tight, identifiable vertical with trade associations, conferences (e.g. Vision Expo),
  and practice-management consultants → cheap, targeted distribution.
- Homogeneous chart-of-accounts patterns → our config templates generalize *within* the
  vertical far better than across all SMBs.
- A real, ignored cost-structure nuance (optical COGS, doctor vs. staff payroll) → a
  credible "we understand your business" wedge a horizontal tool can't claim.
- **Land-and-expand to adjacent verticals later:** dental, veterinary, chiropractic,
  physical therapy — all owner-operated, dispensary-or-procedure-heavy, same playbook.

---

## 4. Go-to-Market

### 4.1 Beachhead motion (first 5 firms — founder-led, high-touch)
1. **Anchor case study:** the existing live optometry deployment is the proof. Turn it into
   a one-page "before/after" with sample report pack (sanitized).
2. **Direct outreach** to bookkeeping firms that advertise optometry/medical-practice
   specialization (LinkedIn, QuickBooks ProAdvisor directory filtered by industry).
3. **Pilot offer:** "We'll run this for **3 of your optometry clients free for 60 days**,
   fully branded to you. If your clients love it, you pay for the rest." Zero-risk land.
4. **Partner with optometry practice-management consultants** — they advise dozens of
   practices and are trusted referrers; revenue-share or flat referral fee.

### 4.2 Scaling motion (firms 5–60)
- **ProAdvisor & accounting communities:** content marketing ("How to deliver advisory
  reporting to optometry clients without hiring"), webinars, niche podcasts.
- **Vision Expo / optometry trade shows:** present to firms *and* practices; firms are the
  buyer, practice demand pulls firms in.
- **Referral loop:** each firm that's happy refers peers; accounting is a tight,
  word-of-mouth profession. Build a formal referral incentive.

### 4.3 Channel economics
- One firm sale = 10–40 client-locations. **CAC amortizes across the whole book**, which is
  what makes a solo operator's unit economics work.
- The firm does tier-1 client hand-holding (they own the relationship); we do tier-2
  technical ops. Support cost per client stays low.

---

## 5. Pricing & Packaging

**Bill the firm, per active client location, monthly.** Tiered to reward consolidation:

| Tier | Clients | Price/client/mo | Notes |
|---|---|---|---|
| Starter | 1–5 | $60 | Pilot graduates; covers onboarding cost |
| Growth | 6–20 | $45 | The sweet spot |
| Scale | 21+ | $30 | Volume; locks in the firm, raises switching cost |

**Add-ons (margin expanders):**
- **White-label branding** (firm logo, colors, sending domain): +$100–$250/mo flat per firm.
- **Custom report** beyond the standard pack: one-time setup fee ($150–$400) + small monthly.
- **Quarterly advisory commentary** (auto-generated narrative + light human review): premium tier.

**Why per-client, billed to the firm:**
- Aligns our revenue with the firm's growth (they add clients → we grow automatically).
- Predictable MRR; trivial to forecast.
- The firm marks it up into their advisory fee — **our price is their COGS, not their
  retail** — so price sensitivity is low.

**Annual prepay** at ~2 months free to improve cash flow and cut churn.

---

## 6. Operations & Delivery

- **Onboarding (per client):** firm authorizes QBO once per client (OAuth), we map the
  chart of accounts to our optometry template, send a sample pack for sign-off, go live.
  Target: **< 30 min of our time per client** once tooling matures (see Architecture doc).
- **Run cadence:** automated on the 1st/16th (existing). Reconciliation holds route to an
  ops queue, not the client.
- **Support model:** firm is tier-1 for "what does this number mean"; we are tier-2 for
  "the report didn't arrive / didn't reconcile." A shared ops inbox + runbook (already
  written) handles the long tail.
- **The real operational risk is credential/token lifecycle at scale** — covered in depth
  in the Solution Architecture gap analysis. This is the single biggest thing standing
  between "1 client" and "120 clients" and must be engineered *before* aggressive sales.

---

## 7. Financial Model (illustrative, Year 1, solo)

**Assumptions:** ramp to 8 firms / 120 client-locations by month 12; blended $40/client.

| | Per month at scale (mo 12) |
|---|---:|
| Revenue (120 × $40) | $4,800 |
| + white-label add-ons (8 × $150) | $1,200 |
| **Gross revenue** | **$6,000** |
| Infra (hosting, monitoring, email) | ~$150 |
| Intuit/API, domains, tools | ~$100 |
| **Gross margin** | **~95%** |

- **Costs are almost entirely founder time**, front-loaded into onboarding + the
  multi-tenant engineering. Marginal cost per added client is near zero — *if* the token
  lifecycle is automated. If it isn't, support time per client becomes the hidden tax that
  caps the business (the core risk).
- **Break-even is trivial** on cash; the binding constraint is **founder hours**, which is
  why the architecture must drive per-client onboarding + ops toward zero-touch before
  scaling past ~30 clients.

---

## 8. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **Token/credential ops don't scale** (per-client OAuth + refresh-token rotation) | **High** | This is the make-or-break. Centralize credential storage + automate rotation *before* selling past a handful of clients. See Architecture gap doc — it's the hardest integration and the top engineering priority. |
| Intuit production app review / API terms | Med | Required to exceed dev-tier company limits. Budget weeks for review; build to Intuit security requirements from day one. |
| Firm churns and takes its whole book | High concentration | Diversify firms; annual prepay; embed via branding + custom reports to raise switching cost; deliver visible monthly value the firm's clients ask for by name. |
| Intuit ships native "good" reporting | Med | They've under-invested here for years and won't go vertical (optometry COGS, reconciliation hold). Stay ahead on trust + vertical depth, not dashboards. |
| Liability if a wrong number ships | Med | The reconciliation guardrail *is* the mitigation and the differentiator. Add clear "advisory, not assurance" terms; firm reviews before forwarding on sensitive accounts. |
| Founder bus-factor / solo capacity | High | Productize ops (runbooks, monitoring, dead-man's-switch already exist); document everything; keep onboarding sub-30-min so a part-time contractor can run it. |
| Data security / PII handling | High | Financial data demands real security posture: least-privilege tokens, encryption at rest, audit logging. Table-stakes for selling to accountants. |

---

## 9. Roadmap & Milestones

| Phase | Window | Goal | Exit criterion |
|---|---|---|---|
| **0 — Productize for 1 firm** | Months 0–2 | Multi-client on one firm; automate token lifecycle | 1 firm, 3 clients, zero-touch runs for 2 cycles |
| **1 — Beachhead** | Months 2–6 | 3–5 firms via pilots | $2k MRR; onboarding < 30 min/client |
| **2 — Repeatable GTM** | Months 6–12 | 8 firms, 120 clients | $5k+ MRR; one repeatable acquisition channel proven |
| **3 — Vertical depth + add-ons** | Months 12–18 | White-label, custom reports, narrative tier | $15k+ MRR; net revenue retention > 100% |
| **4 — Adjacent verticals** | 18+ | Dental/vet templates; consider light hire | Second vertical generating revenue |

---

## 10. Why This Can Win (and the honest ceiling)

**Wins because:** narrow vertical + sell-through-the-channel + a genuine, hard-to-copy
differentiator (reconciliation-before-send) + near-zero marginal cost + an incumbent set
that competes on a different axis (dashboards, horizontal).

**Honest ceiling:** this is a **high-margin small business**, not a venture rocket. The
binding constraints are founder hours and the token-lifecycle engineering. Cross those, stay
disciplined on the niche, and a one-person operation realistically reaches **$15k–$50k MRR**
before needing to decide whether to hire and broaden — at which point the optometry beachhead
becomes the proof for the multi-vertical version.

> Companion document: **`SOLUTION_ARCHITECTURE_GAPS.md`** — the concrete engineering gap
> analysis for turning today's single-tenant deployment into a sellable, locally-hosted,
> single-client product for an accounting firm, with effort estimates and the
> hardest-integration risk that could bite clients post-sale.
