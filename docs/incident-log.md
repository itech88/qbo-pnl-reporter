# Incident Log

Every run that fails, holds a report, or self-heals appends a section here. Newest
entries are at the bottom. Written by `incidents.py`; committed by the report
workflow. Do not hand-edit an existing entry — add a follow-up note underneath it
instead, so the record of what the system saw at the time stays intact.

Entry kinds:

| Kind | Meaning |
|---|---|
| `abort` | The pipeline died before delivering anything. No reports went out. |
| `report_failure` | One report raised an exception. The others still delivered. |
| `guardrail_hold` | Numbers failed reconciliation, so the report was withheld rather than sent wrong. |
| `self_heal` | A fault was detected and repaired automatically. The report delivered. |
| `degraded` | The report delivered, but from a reduced data set. |

---

## 2026-08-16 18:30 UTC — run `2dcebc05` (production) — 2 incidents

Backfilled by hand on 2026-09-09 from the Actions log, before automatic recording
existed. [Actions run](https://github.com/itech88/qbo-pnl-reporter/actions/runs/31964766805)

### `guardrail_hold` · A/R Aging

- **What happened:** Withheld from delivery: A/R aging total 0.00 ≠ Balance Sheet A/R 50.00 (off by -50.00); A/R Aging detail total 50.00 ≠ aging summary 0.00 (off by +50.00).
- **Why:** The `AgedReceivables` summary endpoint returned HTTP 200 but yielded no per-party rows, so the parsed total was zero. The `AgedReceivableDetail` endpoint and the Balance Sheet both independently reported 50.00 for the same date, which rules out a data problem and points squarely at the summary response. No "no Money/bucket columns" warning was logged, so the report did carry its bucket columns — it was the data rows the parser found nothing to read.
- **What the system did:** Held the report. The remaining ten reports delivered normally.
- **Outcome:** Resolved on 2026-09-09 by the self-heal described below. Not resolved at the time.

### `guardrail_hold` · Cash Outlook

- **What happened:** Withheld from delivery: upstream A/R aging did not reconcile.
- **Why:** Cash Outlook composes a reconciled A/R summary with cash on hand and current liabilities. With A/R held, there was no trustworthy receivables figure to build a cash position from, so the hold propagated by design.
- **What the system did:** Held the report. This is intended behaviour: a downstream report inherits the hold rather than quietly substituting an unverified input.
- **Outcome:** Resolved on 2026-09-09 as a consequence of fixing A/R Aging. Not resolved at the time.

---

## 2026-09-01 20:41 UTC — run `213a666d` (production) — 1 incident

Backfilled by hand on 2026-09-09 from the Actions log, before automatic recording
existed. [Actions run](https://github.com/itech88/qbo-pnl-reporter/actions/runs/33556741038)

### `abort` · pipeline

- **What happened:** The pipeline aborted before delivering anything. The second Profit and Loss call, covering 2024-07-01 to 2024-12-31, returned `500 {"errorCode":20001,"errorMessage":"Something went wrong while processing the request"}` and the resulting `HTTPError` propagated out of `scheduler.run()`.
- **Why:** Intuit returns this generic 500 for both genuine backend faults and throttled requests. The response arrived 80 milliseconds after the request, far too fast for a real six-month report computation, so this was almost certainly a throttle. The first half-year call had succeeded moments earlier against the same endpoint. The pipeline had no retry anywhere in its call stack, so a sub-second blip on one of roughly ten API calls destroyed the entire delivery cycle rather than just the call that failed.
- **What the system did:** Nothing. It emailed the failure alert and exited non-zero. All twelve reports were lost, including the ten that had no dependency on the failed call.
- **Outcome:** Resolved on 2026-09-09. Retry with exponential backoff now lives in the shared authenticated session, so every fetcher inherits it.

#### Follow-up

This failure mode was predicted. `docs/code-review-and-bug-triage.md`, written on
2026-07-12, records it as **RB-7 · No retry/backoff for 429 / 5xx from QBO** and
rates it LOW/MEDIUM. It went to production unfixed and took down the September 1
send seven weeks later.

The severity rating was the mistake, not the diagnosis. RB-7 was scored on the
likelihood of a transient error, which is genuinely low per call, rather than on
the blast radius when one lands, which was total. A fault that turns one failed
API call into zero delivered reports is an availability defect regardless of how
rarely it fires.

The missed September 1 reports were sent manually on 2026-09-09 once the retry
was deployed.

---

## 2026-09-10 04:09 UTC — run `9e5f72a6` (production, dry run) — 1 incident, 1 self-healed

### `self_heal` · A/R Aging

- **What happened:** The A/R aging summary endpoint returned 0.00 while the Balance Sheet showed 50.00 for the same date.
- **Why:** The aging summary endpoint returned a total that disagrees with the Balance Sheet's own A/R or A/P line for the same date. The Balance Sheet is the ledger of record, so the summary is the side that is wrong — usually because the summary report returned no per-party rows at all, not because it returned wrong amounts.
- **What the system did:** Rebuilt the aging buckets from the per-document detail report, which the Balance Sheet independently corroborates, then re-ran the same reconciliation against the Balance Sheet before sending. The report was delivered rather than held.
- **Outcome:** Resolved automatically. No action needed.

---
