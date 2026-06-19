"""
Aging analytics — pure views over the buckets/detail DataFrames from aging_fetcher.

Aging is a point-in-time snapshot, so (unlike the P&L analytics) there is no MoM/YoY
here — every view describes "what is outstanding right now."

Key views:
  aging_summary(buckets_df)        — total, current vs overdue, per-bucket totals
  party_breakdown(buckets_df)      — per payer/creditor: bucket row, total, share
  oldest_items(detail_df, top_n)   — the oldest open documents (the worklist)
  days_outstanding(total, daily)   — DSO (A/R) / DPO (A/P) from a trailing daily rate
"""

from datetime import date

import pandas as pd


def _is_current(bucket_label: str) -> bool:
    """A bucket is 'current' (not yet overdue) only if it is literally 'Current'."""
    return bucket_label.strip().lower() == "current"


# ---------------------------------------------------------------------------
# Totals
# ---------------------------------------------------------------------------

def aging_summary(buckets_df: pd.DataFrame) -> dict:
    """Headline totals: grand total, current vs overdue, and per-bucket breakdown."""
    bucket_order = buckets_df.attrs.get("bucket_order", [])
    if buckets_df.empty:
        return {"total": 0.0, "current": 0.0, "overdue": 0.0,
                "overdue_pct": 0.0, "buckets": []}

    by_bucket = buckets_df.groupby("bucket")["amount"].sum()
    total = float(by_bucket.sum())
    current = float(sum(v for b, v in by_bucket.items() if _is_current(b)))
    overdue = total - current

    # Preserve QBO's column order; append any unexpected buckets at the end.
    ordered = bucket_order + [b for b in by_bucket.index if b not in bucket_order]
    buckets = [
        {
            "label":   b,
            "amount":  float(by_bucket.get(b, 0.0)),
            "share":   (float(by_bucket.get(b, 0.0)) / total) if total else 0.0,
            "overdue": not _is_current(b),
        }
        for b in ordered
    ]
    return {
        "total":       total,
        "current":     current,
        "overdue":     overdue,
        "overdue_pct": (overdue / total) if total else 0.0,
        "buckets":     buckets,
    }


# ---------------------------------------------------------------------------
# Per-party breakdown
# ---------------------------------------------------------------------------

def party_breakdown(buckets_df: pd.DataFrame, top_n: int = 8) -> dict:
    """
    Per-party (payer/creditor) bucket row + total + share, sorted by total desc.
    Parties beyond `top_n` fold into one 'Other' row.

    Returns {bucket_order, grand_total, rows: [{party, buckets{label:amt}, total, share, overdue}]}.
    """
    bucket_order = buckets_df.attrs.get("bucket_order", [])
    if buckets_df.empty:
        return {"bucket_order": bucket_order, "grand_total": 0.0, "rows": []}

    pivot = buckets_df.pivot_table(
        index="party", columns="bucket", values="amount", aggfunc="sum", fill_value=0.0,
    )
    ordered_cols = [b for b in bucket_order if b in pivot.columns] + \
                   [b for b in pivot.columns if b not in bucket_order]
    pivot = pivot[ordered_cols]
    pivot["__total"] = pivot.sum(axis=1)
    grand = float(pivot["__total"].sum())
    pivot = pivot.sort_values("__total", ascending=False)

    def _row(party: str, series: pd.Series) -> dict:
        bucket_vals = {b: float(series.get(b, 0.0)) for b in ordered_cols}
        total = float(series["__total"])
        overdue = sum(v for b, v in bucket_vals.items() if not _is_current(b))
        return {
            "party":   party,
            "buckets": bucket_vals,
            "total":   total,
            "overdue": overdue,
            "share":   (total / grand) if grand else 0.0,
        }

    head = pivot.head(top_n)
    rows = [_row(p, r) for p, r in head.iterrows()]

    if len(pivot) > top_n:
        tail = pivot.iloc[top_n:]
        other = tail.sum()
        other["__total"] = float(tail["__total"].sum())
        rows.append(_row("Other", other))

    return {"bucket_order": ordered_cols, "grand_total": grand, "rows": rows}


# ---------------------------------------------------------------------------
# Oldest open items (the worklist)
# ---------------------------------------------------------------------------

def oldest_items(detail_df: pd.DataFrame, top_n: int = 10) -> list[dict]:
    """The oldest open documents by days overdue — the chase-this-week list."""
    if detail_df.empty:
        return []
    items = detail_df[detail_df["open_balance"] > 0].copy()
    if items.empty:
        return []
    # Sort oldest first; unknown ages (NaN days) sink to the bottom.
    items = items.sort_values("days_overdue", ascending=False, na_position="last").head(top_n)

    out = []
    for _, r in items.iterrows():
        days = r["days_overdue"]
        out.append({
            "party":        r["party"],
            "doc_num":      r["doc_num"] or "—",
            "due_date":     r["due_date"].isoformat() if isinstance(r["due_date"], date) else "—",
            "open_balance": float(r["open_balance"]),
            "days_overdue": int(days) if pd.notna(days) else None,
        })
    return out


# ---------------------------------------------------------------------------
# Days outstanding (DSO for A/R, DPO for A/P)
# ---------------------------------------------------------------------------

def days_outstanding(total: float, trailing_daily: float | None) -> float | None:
    """
    Average days money sits before settling: total ÷ trailing daily flow.

    For A/R, `trailing_daily` is daily revenue (→ DSO); for A/P, daily COGS/spend
    (→ DPO). Returns None when the daily rate is unknown or non-positive, so the
    report shows '—' rather than a divide-by-zero artifact.
    """
    if not trailing_daily or trailing_daily <= 0:
        return None
    return total / trailing_daily
