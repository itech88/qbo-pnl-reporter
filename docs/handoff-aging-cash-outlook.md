# Handoff — Aging + Cash Outlook (Tier 2 balance-sheet reports)

**For:** the next dev resuming this work
**Branch:** `aging-and-cash-outlook` (pushed to origin; default branch is `main`; **no PR opened yet**)
**Status:** Feature built and dry-run-tested against production. **One adjustment remains**
(see §3) before it's ready to ship to the client.

---

## 1. TL;DR — where things stand

We added three twice-monthly **balance-sheet** reports alongside the existing P&L flow
reports, to answer *"where is my cash?"* (vs. the existing *"am I profitable?"*):

- **A/R Aging** — money owed to the practice, by payer + age bucket, oldest-items worklist, DSO.
- **A/P Aging** — money owed out, by creditor + age bucket. *(Not applicable to this client — see §2.)*
- **Cash Outlook** — a cash position snapshot composed from A/R, payables, and cash on hand.

All of it is built, unit-tested (**261 tests green**), and has been run **3× against production
QuickBooks in dry-run** (no emails). Those dry-runs surfaced real, client-specific facts that
change the design — captured in §2. **The remaining task is in §3.**

---

## 2. What the production dry-runs taught us (critical context)

Run via `gh workflow run "Monthly P&L Reports" --ref aging-and-cash-outlook -f dry_run=true`
(see §4). Three findings, each already handled except the last:

1. **`AgedPayableDetail` persistently returns Intuit fault 130 "Accessing Wrong Cluster"**
   for this realm (the `AgedReceivableDetail` equivalent works fine; failed identically on two
   runs 90 min apart on a fresh token → not transient, not auth).
   **Handled:** the detail fetch is now best-effort (`aging_fetcher.fetch_aging_raw` returns
   `detail=None` on failure → report degrades to summary-only), and `auth.QBOSession` no longer
   refreshes/rotates the token on a wrong-cluster 401 (`auth._is_wrong_cluster`).

2. **The Balance Sheet returned `A/R=None A/P=None`** because QBO served it on the company-default
   (cash) basis, which has no receivables/payables.
   **Handled:** `cash_outlook.fetch_balance_sheet` now forces `accounting_method=Accrual`.

3. **This client has NO Accounts Payable** — they pay vendors by **credit card**, never enter
   bills. The accrual Balance Sheet has no A/P account; liabilities are **credit cards** (AXOS,
   Chase, US Bank) + loans. Also **A/R is currently $0** (they collect fast). Evidence: the BS
   labels (logged by `cash_outlook.extract_balance_sheet`'s diagnostic warning) include
   `Total Bank Accounts`, `Total Accounts Receivable`/`Accounts Receivable (A/R)`,
   `Total Current Liabilities`, `Total Credit Cards` — **but no Accounts Payable line**.
   This is why A/P Aging is empty/meaningless here and Cash Outlook as `Cash + A/R − A/P` would
   be misleading (A/P=$0 ignores their real credit-card debt).
   **NOT yet handled → this is the remaining task.**

---

## 3. ⏭️ THE REMAINING TASK — "Cash Outlook on current liabilities"

**Decision made with the user:** drop A/P Aging for this client and base Cash Outlook on the
Balance Sheet's **Total Current Liabilities** instead of A/P:

> **Net position = Cash on hand + A/R − Total Current Liabilities** (all from the accrual BS).

Keep the A/P-side code (it's client-agnostic and useful for future clients who *do* enter bills);
only this deployment's *config* drops it.

**Concrete steps:**

1. **`cash_outlook.py`**
   - `extract_balance_sheet()` — add `"current_liabilities"` to the returned dict, matched on the
     label **`"total current liabilities"`** (confirmed present). Keep `ap` for other clients.
   - `build_outlook()` — change to compute "owed" from current liabilities, not A/P. Suggested:
     `build_outlook(ar_summary, cash, current_liabilities)` → `net = cash + ar_total − current_liabilities`.
     Drop the `ap_summary` argument. (Optionally also surface `Total Credit Cards` as a sub-line.)

2. **`scheduler.py`** (Step 4.6, Cash Outlook)
   - Gate changes from `{"receivable","payable"} <= aging_summaries` to **`"receivable" in aging_summaries`**.
   - Stop adding `"payable"` to `needed_sides` for the outlook (Step 4.5): change
     `if send_outlook: needed_sides |= {"receivable","payable"}` → `… |= {"receivable"}`.
   - Build with the new signature: `build_outlook(aging_summaries["receivable"], bs.get("cash"), bs.get("current_liabilities"))`.
   - **Hold** the outlook if `bs.get("current_liabilities") is None` (no trustworthy "owed" figure).

3. **`reports/ap_aging.yaml`** — **delete it** (A/P Aging not deployed for this client). Leave a
   note in the handoff/README that the A/P code path is retained for future bill-using clients.

4. **`templates/cash_outlook.html`** — replace the "− Payables (you owe)" A/P row (with its
   current/overdue split) with a single **"− Current liabilities (you owe)"** figure. Keep the A/R
   current/overdue split. Update the explanatory note to "Net position = Cash + Receivables −
   Current liabilities."

5. **`report.py` `build_cash_outlook()` + `_outlook_chart()`** — update context keys to the new
   `outlook` dict (remove `ap_*`, add `current_liabilities`); chart bars become
   `["Cash on hand", "+ Receivables", "− Current liabilities", "= Net position"]`.

6. **Tests**
   - `tests/fixtures/balance_sheet.json` — add a `Total Current Liabilities` summary line.
   - `tests/test_cash_outlook.py` — assert `extract_balance_sheet()["current_liabilities"]`; update
     `build_outlook` tests to the new signature/net formula.

7. **Verify** — re-run the production dry-run (§4); confirm `Cash Outlook` ships with real numbers
   (cash ≈ $117,256.88, A/R $0, current liabilities = a real figure, net computed). Download
   `preview_cash_outlook.html` and eyeball. A/R Aging should still ship (empty, "No open
   receivables"). Then open a PR `aging-and-cash-outlook → main`.

---

## 4. How to run & verify (no emails are sent in dry-run)

```bash
# Local: full suite (always do this first)
.venv/bin/python -m pytest tests/ -q                 # 261 passing

# Local: render any one report to a preview_*.html (mocked, no network) — see the
# bottom-of-file CLI blocks, or drive scheduler with monkeypatched fetches.

# Production dry-run in GitHub Actions (read-only QBO, writes preview artifacts, NO email):
gh workflow run "Monthly P&L Reports" --ref aging-and-cash-outlook -f dry_run=true
gh run list --workflow="Monthly P&L Reports" --branch aging-and-cash-outlook --limit 1
gh run watch <RUN_ID> --exit-status
gh run download <RUN_ID> --dir /tmp/qbo_artifacts     # preview_*.html artifacts
```

Notes:
- `workflow_dispatch` only works on workflows registered on the **default branch** (`main`).
  `Monthly P&L Reports` (`monthly_report.yml`) is there, so dispatching it `--ref` the feature
  branch runs the feature branch's code. The per-report workflows we added
  (`report_ar_aging.yml`, etc.) won't be dispatchable until merged to `main`.
- Each failed/early dry-run **rotated the QBO token and persisted it to GitHub Secrets** — that's
  expected and fine (tokens stay valid).

---

## 5. The reconciliation contract (the safety bar — don't weaken it)

Every report is **held, not sent**, unless it reconciles. For aging:
- **A/R / A/P summary total** is anchored to the **Balance Sheet's own A/R / A/P line**
  (`guardrails.reconcile_balance_sheet`) — the authoritative number.
- **When the detail report is available**, the open-item detail must also tie to the summary
  (`guardrails.reconcile_aging`).
- **If neither anchor is available**, the report is held (`"no reconciliation anchor"`).
- **Cash Outlook** is held if its inputs didn't reconcile. (After §3, it depends on A/R reconciling
  + BS current-liabilities being present.)

This was verified both ways in dry-runs and unit tests: good data ships, mismatched/anchorless
data is held and never emailed, and the run exits non-zero so the operator is alerted.

---

## 6. File map (what we added/changed this session)

| Area | Files |
|---|---|
| Fetch | `aging_fetcher.py` (A/R+A/P, side-parameterized; best-effort detail), `cash_outlook.py` (BS cash + extract + compose) |
| Analytics | `aging_analytics.py` (buckets, party breakdown, oldest items, DSO/DPO) |
| Reconcile | `guardrails.py` — added `reconcile_aging`, `reconcile_balance_sheet` |
| Render | `report.py` — `build_aging_report`/`_aging_chart`, `build_cash_outlook`/`_outlook_chart`; `templates/aging_report.html`, `templates/cash_outlook.html` |
| Orchestrate | `scheduler.py` — Step 4.5 (aging, BS-anchored) + Step 4.6 (Cash Outlook); `_trailing_daily_*` helpers |
| Auth | `auth.py` — `_is_wrong_cluster` + skip token refresh on wrong-cluster 401 |
| Config | `reports/ar_aging.yaml`, `reports/ap_aging.yaml` (**delete per §3**), `reports/cash_outlook.yaml` |
| CI | `.github/workflows/report_ar_aging.yml`, `report_ap_aging.yml`, `report_cash_outlook.yml` |
| Tests | `tests/test_aging_fetcher.py`, `test_aging_analytics.py`, `test_cash_outlook.py`, fixtures `aged_receivable_*.json` + `balance_sheet.json`; additions to `test_guardrails.py`, `test_auth.py` |
| Plan | `/Users/itech88/.claude/plans/let-s-plan-out-the-woolly-spindle.md` (original approved plan) |

Housekeeping this session: removed unused `SQLAlchemy`/`Flask` from `requirements.txt` and the
orphaned `data/` dir (was the never-built SQLAlchemy DB store).

---

## 7. Known quirks / gotchas

- **`AgedPayableDetail` will keep wrong-clustering** for this realm — expected; handled by
  summary-only degradation. Don't "fix" it by retrying harder.
- **A/R is $0 today** — the empty A/R report ("No open receivables") is correct, not a bug. Its
  value shows when receivables build up.
- **A/R alias map (`reports/ar_aging.yaml: aliases`) is empty** — seed it from real payer names
  once A/R is non-zero (run a dry-run, read the payer names, add `lowercased: Clean Name`), exactly
  like the COGS-by-Vendor alias map.
- **DSO/DPO are best-effort** — only populated when the P&L is also fetched that run (1st/16th or
  `--force`). On a single `--report` run they show "—".

---

## 8. Environment note (a machine change happened this session)

**Anaconda was uninstalled** from this Mac mid-session. Audit result: the `.venv` is built on
**Homebrew Python 3.13** (not Anaconda), so it's unaffected — `pip check` clean, full suite green.
Shell rc files and the repo are clean of conda references. **But** the *currently running* VS Code
process still carries stale `CONDA_*` env vars + dead `~/anaconda3/bin` PATH entries inherited from
its launch shell.
- **Action when convenient:** fully quit & reopen VS Code to clear the stale env (a fresh login
  shell is already conda-free).
- **Bare `python` no longer exists** (Anaconda provided it); use `python3` or activate the venv
  (`source .venv/bin/activate`, which provides `python`). The README's local-dev flow assumes an
  activated venv, so it still works.

---

## 9. Lower-priority follow-ups (not blocking)

- Add a "% A/R overdue" tile to the Monthly Business Dashboard scorecard (fast follow once A/R is
  non-zero).
- Consider breaking Cash Outlook's "owed" into sub-lines (credit cards vs other current
  liabilities) for more detail.
- The clinician-facing business briefing for these reports lives in the plan file (§Business
  briefing) — hand it to the practice owner when introducing the new emails.
