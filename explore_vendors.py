"""
Vendor exploration phase 2 — pull full vendor list and test monthly summarization.
"""
import os
from collections import defaultdict
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

# ── Full vendor list with totals (Jan-May 2026) ──────────────────────────────
print("\n── All Vendors + Monthly Totals (TransactionListByVendor, Jan-May 2026) ──")
data = get_report("TransactionListByVendor", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
})
if data:
    vendor_totals = {}
    rows = data.get("Rows", {}).get("Row", [])
    print(f"Total vendor sections: {len(rows)}")
    for row in rows:
        if row.get("type") == "Section":
            vendor = row.get("Header", {}).get("ColData", [{}])[0].get("value", "")
            if not vendor:
                continue
            total = 0.0
            for tx in row.get("Rows", {}).get("Row", []):
                cols = tx.get("ColData", [])
                if len(cols) >= 7:
                    try:
                        total += float(cols[6].get("value", 0) or 0)
                    except (ValueError, TypeError):
                        pass
            vendor_totals[vendor] = total

    for vendor, total in sorted(vendor_totals.items(), key=lambda x: -x[1]):
        print(f"  {vendor:<45} ${total:>10,.2f}")

# ── Test: monthly breakdown for one vendor (Alcon) ───────────────────────────
print("\n── Monthly breakdown for Alcon (by parsing transaction dates) ──")
if data:
    monthly = defaultdict(float)
    for row in rows:
        if row.get("type") == "Section":
            vendor = row.get("Header", {}).get("ColData", [{}])[0].get("value", "")
            if "alcon" not in vendor.lower():
                continue
            for tx in row.get("Rows", {}).get("Row", []):
                cols = tx.get("ColData", [])
                if len(cols) >= 7:
                    date_str = cols[0].get("value", "")
                    try:
                        month = int(date_str.split("-")[1])
                        amount = float(cols[6].get("value", 0) or 0)
                        monthly[month] += amount
                    except (ValueError, IndexError, TypeError):
                        pass
    for m, amt in sorted(monthly.items()):
        from datetime import datetime
        mn = datetime(2000, m, 1).strftime("%b")
        print(f"  {mn}: ${amt:,.2f}")

# ── Column headers ────────────────────────────────────────────────────────────
print("\n── Column headers from TransactionListByVendor ──")
if data:
    cols = data.get("Columns", {}).get("Column", [])
    for i, c in enumerate(cols):
        print(f"  [{i}] {c.get('ColTitle','?')!r}  type={c.get('ColType','?')}")
