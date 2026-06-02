"""
One-off investigation: why is COGS 'Unattributed' (blank vendor) so high in
May 2026 and YoY? Pulls ProfitAndLossDetail and dissects the blank-name COGS
rows by transaction type, source account, memo, amount, and date.
Run via GitHub Actions (investigate_unattributed.yml) so it uses prod secrets.
"""
import os
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from auth import get_session
from fetcher import _base_url, _realm_id
from vendor_fetcher import (
    _col_indices, _find_cogs_sections, _collect_data_rows, _UNATTRIBUTED,
)

session = get_session()
url = f"{_base_url()}/v3/company/{_realm_id()}/reports/ProfitAndLossDetail"


def pull(start, end):
    r = session.get(url, params={"start_date": start, "end_date": end}, timeout=40)
    r.raise_for_status()
    return r.json()


def col_map(data):
    """Full ColType->index map (we want memo, txn_type, doc_num, split_acc too)."""
    idx = {}
    for i, c in enumerate(data.get("Columns", {}).get("Column", [])):
        idx[c.get("ColType", f"col{i}")] = i
    return idx


def blank_cogs_rows(data):
    """Return list of dicts for COGS rows with a blank vendor name."""
    idx = col_map(data)
    name_i = idx.get("name")
    out = []
    for sec in _find_cogs_sections(data.get("Rows", {}).get("Row", []), "cost of goods"):
        for cd in _collect_data_rows(sec):
            name = (cd[name_i].strip() if name_i is not None and name_i < len(cd) else "")
            if name:
                continue
            def g(key):
                i = idx.get(key)
                return cd[i] if i is not None and i < len(cd) else ""
            out.append({
                "date":     g("tx_date"),
                "type":     g("txn_type"),
                "num":      g("doc_num"),
                "memo":     g("memo"),
                "split":    g("split_acc"),
                "amount":   g("subt_nat_amount"),
            })
    return out


def to_f(s):
    try:
        return float(str(s).replace(",", "") or 0)
    except ValueError:
        return 0.0


print("=" * 70)
print("UNATTRIBUTED COGS INVESTIGATION")
print("=" * 70)

# ── 1. YoY blank-vendor COGS totals, by month ───────────────────────────────
print("\n── Blank-vendor COGS totals by year/month ──")
yearly = {}
for year in (2024, 2025, 2026):
    rows = []
    for s, e in [(f"{year}-01-01", f"{year}-06-30"), (f"{year}-07-01", f"{year}-12-31")]:
        rows += blank_cogs_rows(pull(s, e))
    yearly[year] = rows
    by_month = defaultdict(float)
    for r in rows:
        try:
            m = int(r["date"].split("-")[1])
        except (ValueError, IndexError):
            continue
        by_month[m] += to_f(r["amount"])
    line = "  ".join(f"{datetime(2000,m,1):%b}:{by_month.get(m,0):>8,.0f}" for m in range(1, 13))
    print(f"  {year}: {line}")

# ── 2. May 2026 deep dive ───────────────────────────────────────────────────
print("\n── May 2026 blank-vendor COGS — every transaction ──")
may = [r for r in yearly[2026] if r["date"].startswith("2026-05")]
may.sort(key=lambda r: -to_f(r["amount"]))
total = sum(to_f(r["amount"]) for r in may)
print(f"  {len(may)} transactions, total ${total:,.2f}\n")
for r in may:
    print(f"  {r['date']}  {r['type']:<14} ${to_f(r['amount']):>9,.2f}  "
          f"split={r['split'][:22]:<22} memo={r['memo'][:40]}")

# ── 3. Group May by transaction type and by source/split account ────────────
print("\n── May 2026 blank COGS grouped by Transaction Type ──")
by_type = defaultdict(lambda: [0, 0.0])
for r in may:
    by_type[r["type"]][0] += 1
    by_type[r["type"]][1] += to_f(r["amount"])
for t, (n, amt) in sorted(by_type.items(), key=lambda x: -x[1][1]):
    print(f"  {t:<18} count={n:<3} ${amt:>10,.2f}")

print("\n── May 2026 blank COGS grouped by Split (source) account ──")
by_split = defaultdict(lambda: [0, 0.0])
for r in may:
    by_split[r["split"]][0] += 1
    by_split[r["split"]][1] += to_f(r["amount"])
for s, (n, amt) in sorted(by_split.items(), key=lambda x: -x[1][1]):
    print(f"  {s[:34]:<34} count={n:<3} ${amt:>10,.2f}")

# ── 4. Memo keyword frequency (hints at the real vendor) ────────────────────
print("\n── May 2026 blank COGS — memo first-token frequency ──")
tokens = defaultdict(lambda: [0, 0.0])
for r in may:
    key = (r["memo"].strip().split(" ")[0] if r["memo"].strip() else "(no memo)")
    tokens[key][0] += 1
    tokens[key][1] += to_f(r["amount"])
for k, (n, amt) in sorted(tokens.items(), key=lambda x: -x[1][1])[:15]:
    print(f"  {k[:30]:<30} count={n:<3} ${amt:>10,.2f}")
