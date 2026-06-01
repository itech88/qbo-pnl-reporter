"""
Vendor exploration phase 3 — confirm we can expose the distribution (split)
account per transaction so we can dynamically filter to COGS vendors.
"""
import os
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from auth import get_session
from fetcher import _base_url, _realm_id

session    = get_session()
report_url = f"{_base_url()}/v3/company/{_realm_id()}/reports"

def get_report(name, params=None):
    r = session.get(f"{report_url}/{name}", params=params or {}, timeout=30)
    if not r.ok:
        print(f"  HTTP {r.status_code}: {r.text[:300]}")
        return None
    return r.json()

# ── A. AccountList report — find COGS accounts and their IDs ─────────────────
print("\n── AccountList (looking for COGS accounts) ──")
acc = get_report("AccountList")
if acc:
    cols = [c.get("ColTitle","") for c in acc.get("Columns",{}).get("Column",[])]
    print(f"  Columns: {cols}")
    for row in acc.get("Rows",{}).get("Row",[]):
        cd = [c.get("value","") for c in row.get("ColData",[])]
        if cd and ("cogs" in cd[0].lower() or "cost of goods" in cd[0].lower()
                   or (len(cd) > 1 and "cost of goods" in str(cd).lower())):
            print(f"  COGS account row: {cd}")

# ── B. TransactionListByVendor with split account column exposed ─────────────
print("\n── TransactionListByVendor with columns=tx_date,txn_type,name,split_acc,subt_nat_amount ──")
data = get_report("TransactionListByVendor", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
    "columns":    "tx_date,txn_type,name,split_acc,subt_nat_amount",
})
if data:
    cols = [(c.get("ColTitle",""), c.get("ColType","")) for c in data.get("Columns",{}).get("Column",[])]
    print(f"  Columns returned: {cols}")
    rows = data.get("Rows",{}).get("Row",[])
    shown = 0
    for row in rows:
        if row.get("type") == "Section" and shown < 6:
            vendor = row.get("Header",{}).get("ColData",[{}])[0].get("value","")
            inner = row.get("Rows",{}).get("Row",[])
            if inner:
                sample = [c.get("value","") for c in inner[0].get("ColData",[])]
                print(f"  {vendor:<28} sample row: {sample}")
                shown += 1

# ── C. Try filtering TransactionListByVendor to COGS account by name ──────────
print("\n── Same report, account filter test (account name 'Supplies & materials - COGS') ──")
data2 = get_report("TransactionListByVendor", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
    "columns":    "tx_date,txn_type,name,split_acc,subt_nat_amount",
})
if data2:
    # Group by vendor where split account contains 'cogs'
    vendor_cogs = defaultdict(float)
    col_titles = [c.get("ColType","") for c in data2.get("Columns",{}).get("Column",[])]
    print(f"  ColTypes: {col_titles}")
    for row in data2.get("Rows",{}).get("Row",[]):
        if row.get("type") != "Section":
            continue
        vendor = row.get("Header",{}).get("ColData",[{}])[0].get("value","")
        for tx in row.get("Rows",{}).get("Row",[]):
            cd = [c.get("value","") for c in tx.get("ColData",[])]
            joined = " ".join(cd).lower()
            if "cogs" in joined or "cost of goods" in joined:
                # find numeric amount (last col)
                try:
                    vendor_cogs[vendor] += float(cd[-1] or 0)
                except (ValueError, TypeError):
                    pass
    print("  Vendors with COGS-account transactions:")
    for v, amt in sorted(vendor_cogs.items(), key=lambda x:-x[1]):
        print(f"    {v:<35} ${amt:>10,.2f}")
