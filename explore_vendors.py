"""
Vendor exploration phase 4 — confirm ProfitAndLossDetail gives us
vendor-per-transaction under the COGS account. This is the linchpin for
dynamic COGS-by-vendor reporting.
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

print("\n── ProfitAndLossDetail (Jan-May 2026) — columns ──")
data = get_report("ProfitAndLossDetail", {
    "start_date": "2026-01-01",
    "end_date":   "2026-05-31",
})
if data:
    cols = [(i, c.get("ColTitle",""), c.get("ColType","")) for i, c in enumerate(data.get("Columns",{}).get("Column",[]))]
    print(f"  Columns: {cols}")

    # Walk to find COGS section and group transactions by vendor (name column)
    def find_cogs_sections(rows, path=""):
        results = []
        for row in rows:
            if row.get("type") == "Section":
                header = row.get("Header",{}).get("ColData",[{}])[0].get("value","")
                if "cost of goods" in header.lower() or "cogs" in header.lower():
                    results.append((header, row))
                inner = row.get("Rows",{}).get("Row",[])
                results.extend(find_cogs_sections(inner, path + "/" + header))
        return results

    rows = data.get("Rows",{}).get("Row",[])
    cogs_sections = find_cogs_sections(rows)
    print(f"\n  Found {len(cogs_sections)} COGS section(s)")

    # Identify which column holds vendor name and which holds amount
    col_types = [c.get("ColType","") for c in data.get("Columns",{}).get("Column",[])]
    print(f"  ColTypes: {col_types}")

    # Group all data rows under COGS sections by vendor name
    def collect_rows(section_row):
        out = []
        for r in section_row.get("Rows",{}).get("Row",[]):
            if r.get("type") == "Data":
                out.append([c.get("value","") for c in r.get("ColData",[])])
            elif r.get("type") == "Section":
                out.extend(collect_rows(r))
        return out

    vendor_totals = defaultdict(float)
    sample_printed = 0
    for header, sec in cogs_sections:
        for cd in collect_rows(sec):
            if sample_printed < 3:
                print(f"  SAMPLE row under '{header}': {cd}")
                sample_printed += 1
    # Try to identify name + amount column indices from col_types
    name_idx = next((i for i,t in enumerate(col_types) if t in ("name","vend_name","entity")), None)
    amt_idx  = next((i for i,t in enumerate(col_types) if t in ("subt_nat_amount","nat_amount","amount","nat_open_bal")), None)
    print(f"\n  name_idx={name_idx}  amt_idx={amt_idx}")

    if name_idx is not None and amt_idx is not None:
        for header, sec in cogs_sections:
            for cd in collect_rows(sec):
                if len(cd) > max(name_idx, amt_idx):
                    vend = cd[name_idx]
                    try:
                        amt = float(cd[amt_idx] or 0)
                    except (ValueError, TypeError):
                        amt = 0.0
                    if vend:
                        vendor_totals[vend] += amt
        print("\n  ── COGS spend by vendor (Jan-May 2026) ──")
        for v, amt in sorted(vendor_totals.items(), key=lambda x:-x[1]):
            print(f"    {v:<35} ${amt:>10,.2f}")
