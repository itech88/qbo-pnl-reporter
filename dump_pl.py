"""One-off script — dumps raw P&L JSON for inspection. Not part of the pipeline."""
import json
import os
from dotenv import load_dotenv
load_dotenv()
from auth import get_session

base = "https://quickbooks.api.intuit.com"
realm = os.environ["QBO_REALM_ID"]
url = f"{base}/v3/company/{realm}/reports/ProfitAndLoss"

session = get_session()
r = session.get(url, params={
    "start_date": "2026-01-01",
    "end_date":   "2026-06-30",
    "summarize_column_by": "Month",
    "accounting_method": "Accrual",
}, timeout=20)
r.raise_for_status()

with open("raw_pl.json", "w") as f:
    json.dump(r.json(), f, indent=2)
print(f"Saved raw_pl.json — {len(r.text):,} bytes")
