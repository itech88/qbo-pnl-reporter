"""
One-off exploration — queries QBO for Bill and Purchase transactions to
confirm vendor names and COGS account coding before building the report.
Run: python explore_vendors.py
"""
import json
import os
from collections import defaultdict
from dotenv import load_dotenv
load_dotenv()
from auth import get_session

base  = "https://quickbooks.api.intuit.com"
realm = os.environ["QBO_REALM_ID"]
session = get_session()

def query(sql):
    r = session.get(
        f"{base}/v3/company/{realm}/query",
        params={"query": sql},
        timeout=20,
    )
    if not r.ok:
        print(f"HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()

# ── 1. Unique vendors from Bills in 2026 ────────────────────────────────────
print("\n── Vendors with Bills in 2026 ──")
result = query(
    "SELECT VendorRef.name, TotalAmt FROM Bill "
    "WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-12-31' "
    "MAXRESULTS 100"
)
bills = result.get("QueryResponse", {}).get("Bill", [])
vendor_totals = defaultdict(float)
for b in bills:
    name = b.get("VendorRef", {}).get("name", "Unknown")
    vendor_totals[name] += float(b.get("TotalAmt", 0))
for name, total in sorted(vendor_totals.items(), key=lambda x: -x[1]):
    print(f"  {name:<40} ${total:>10,.2f}")

# ── 2. Sample a Bill to see its line-item structure ──────────────────────────
if bills:
    print("\n── Sample Bill line items (first bill) ──")
    for line in bills[0].get("Line", []):
        detail = line.get("AccountBasedExpenseLineDetail", {})
        acct   = detail.get("AccountRef", {}).get("name", "—")
        amt    = line.get("Amount", 0)
        print(f"  Account: {acct:<40} Amount: ${amt:>10,.2f}")

# ── 3. Unique vendors from Purchases (credit card / cash) in 2026 ────────────
print("\n── Vendors with Purchases in 2026 ──")
result2 = query(
    "SELECT EntityRef.name, TotalAmt FROM Purchase "
    "WHERE TxnDate >= '2026-01-01' AND TxnDate <= '2026-12-31' "
    "MAXRESULTS 100"
)
purchases = result2.get("QueryResponse", {}).get("Purchase", [])
purch_totals = defaultdict(float)
for p in purchases:
    name = p.get("EntityRef", {}).get("name", "Unknown/No Vendor")
    purch_totals[name] += float(p.get("TotalAmt", 0))
for name, total in sorted(purch_totals.items(), key=lambda x: -x[1]):
    print(f"  {name:<40} ${total:>10,.2f}")

print(f"\nTotal Bills: {len(bills)}  |  Total Purchases: {len(purchases)}")
