"""
Vendor exploration — uses the Reports API (same path as P&L) to pull
TransactionListByVendor data. Avoids the Query API cluster routing issue.
Run via GitHub Actions workflow: explore_vendors.yml
"""
import os
from dotenv import load_dotenv
load_dotenv()
from auth import get_session
from fetcher import _base_url, _realm_id

session    = get_session()
report_url = f"{_base_url()}/v3/company/{_realm_id()}/reports"

def get_report(name, params=None):
    r = session.get(f"{report_url}/{name}", params=params or {}, timeout=30)
    if not r.ok:
        print(f"  HTTP {r.status_code} on {name}: {r.text[:300]}")
        return None
    return r.json()

# ── 1. TransactionListByVendor ──────────────────────────────────────────────
print("\n── TransactionListByVendor (Jan-May 2026) ──")
data = get_report("TransactionListByVendor", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
})
if data:
    rows = data.get("Rows", {}).get("Row", [])
    print(f"  Top-level rows: {len(rows)}")
    for row in rows[:5]:
        rtype = row.get("type", "?")
        if rtype == "Section":
            header = row.get("Header", {}).get("ColData", [{}])[0].get("value", "")
            print(f"  SECTION: {header}")
            inner = row.get("Rows", {}).get("Row", [])
            for ir in inner[:3]:
                cols = [c.get("value","") for c in ir.get("ColData", [])]
                print(f"    {cols}")

# ── 2. VendorBalance ────────────────────────────────────────────────────────
print("\n── VendorBalance ──")
data2 = get_report("VendorBalance", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
})
if data2:
    rows2 = data2.get("Rows", {}).get("Row", [])
    print(f"  Top-level rows: {len(rows2)}")
    for row in rows2[:10]:
        cols = [c.get("value","") for c in row.get("ColData", [])]
        if cols and cols[0]:
            print(f"  {cols}")

# ── 3. AgedPayables ─────────────────────────────────────────────────────────
print("\n── AgedPayables (to see vendor names) ──")
data3 = get_report("AgedPayables", {"report_date": "2026-05-31"})
if data3:
    rows3 = data3.get("Rows", {}).get("Row", [])
    print(f"  Top-level rows: {len(rows3)}")
    for row in rows3[:10]:
        rtype = row.get("type","")
        if rtype == "Data":
            cols = [c.get("value","") for c in row.get("ColData",[])]
            if cols and cols[0]:
                print(f"  {cols}")
