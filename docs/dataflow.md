# Data Flow

How data moves from the QuickBooks ledger to the owner's inbox on each run, and
how the data is shaped at each hop.

## End-to-end sequence

![End-to-end run — sequence diagram](diagrams/dataflow-sequence.svg)

<details>
<summary>Diagram source (Mermaid)</summary>

```mermaid
sequenceDiagram
    autonumber
    participant GA as GitHub Actions (cron)
    participant SC as scheduler.py
    participant AU as auth.py
    participant GH as GitHub Secrets
    participant QBO as QuickBooks Online
    participant FE as fetcher / vendor_fetcher
    participant AN as analytics
    participant RP as report.py
    participant ML as mailer.py
    participant OW as Owner inbox

    GA->>SC: start run (env: tokens, GH_PAT, SMTP)
    SC->>SC: load reports/*.yaml, filter by trigger_days
    SC->>AU: get_session()
    AU->>AU: access token expired?
    alt token expired or near expiry
        AU->>QBO: POST /oauth2/tokens (refresh_token)
        QBO-->>AU: new access + rotated refresh token
        AU->>GH: gh secret set (writeback, via stdin)
    end
    SC->>FE: fetch P&L (once, 6 calls)
    FE->>QBO: GET /reports/ProfitAndLoss (x6 half-years)
    QBO-->>FE: report JSON (Columns + nested Rows)
    FE-->>SC: DataFrame[year, month, income, value]
    loop each eligible report
        SC->>AN: run_all(df, metric)
        AN-->>SC: mom_df, yoy_df, flags_df
        SC->>RP: build_report(...) → html + chart PNG
        SC->>ML: send_report(html, chart)
        ML->>OW: HTML email + inline chart (CID)
    end
    opt COGS by Vendor requested
        SC->>FE: fetch ProfitAndLossDetail (6 calls)
        FE->>QBO: GET /reports/ProfitAndLossDetail
        QBO-->>FE: per-transaction JSON
        FE-->>SC: DataFrame[year, month, vendor, amount] (memo fallback applied)
        SC->>RP: build_vendor_report(...)
        SC->>ML: send_report(...)
        ML->>OW: COGS by Vendor email
    end
    opt scorecard requested (1st only)
        SC->>RP: build_scorecard(current-month stats for all metrics)
        SC->>ML: send_report(...)
        ML->>OW: Monthly Business Dashboard email
    end
    alt any report failed
        SC->>ML: send_failure_alert(failed reports / traceback)
        ML->>OW: failure alert email
        SC->>GA: exit non-zero
    end
```

</details>

## Data transformation pipeline

How a single metric's data is reshaped from raw API JSON to a rendered email:

![Data transformation pipeline](diagrams/dataflow-pipeline.svg)

<details>
<summary>Diagram source (Mermaid)</summary>

```mermaid
flowchart LR
    RAW["QBO Reports API JSON<br/>nested Columns + Rows tree"]
    EXTRACT["Extraction (fetcher)<br/>section_summary / line_item /<br/>subsection_summary / vendor"]
    DF["Tidy DataFrame<br/>year · month · income · value<br/>(or year·month·vendor·amount)"]
    ANALYSIS["Analytics<br/>MoM ratio · YoY pivot ·<br/>anomaly flags · 3-yr avg"]
    CTX["Template context<br/>formatted rows + chart PNG"]
    HTML["Rendered HTML email<br/>(Jinja2) + matplotlib chart"]

    RAW --> EXTRACT --> DF --> ANALYSIS --> CTX --> HTML
```

</details>

## Data shapes at each stage

| Stage | Shape | Notes |
|---|---|---|
| **QBO ProfitAndLoss** | Nested JSON: `Columns[]` (months) + `Rows.Row[]` tree of Sections/Data | Months parsed from column titles (`"Jan 2026"`); the trailing "Total" column is dropped |
| **QBO ProfitAndLossDetail** | Flat transaction rows under each account section | Columns include Date, Payee (`name`), Memo, Split, Amount |
| **Summary DataFrame** | `year, month, income, value` (long) | One row per month per year; 3 years × 12 months |
| **Vendor DataFrame** | `year, month, vendor, amount` (long) | Vendor = Payee, else memo-derived, else "Unattributed"; aliases applied; grouped/summed |
| **MoM frame** | current-year months with `value_pct` | Zero-income months → NaN pct |
| **YoY frame** | one row per month, per-year `income_/value_/value_pct_` columns | Tolerates missing years |
| **Flags frame** | only flagged months: deviation, direction | Empty when within threshold or no history |
| **Email** | `multipart/related` HTML + inline PNG (`cid:monthly_chart`) | CID inline so Gmail renders the chart |

## Why two fetch paths

- **Summary (`ProfitAndLoss`)** drives the 8 metric reports and the scorecard. Section
  totals are read directly — efficient, one shared fetch per run.
- **Detail (`ProfitAndLossDetail`)** is required only for COGS-by-Vendor, because only
  the detail report breaks each transaction out by vendor under its expense account.
  It is fetched lazily — only when a `vendor_breakdown` report is in scope.

## State that survives between runs

Nothing is cached on disk between runs (the runner is ephemeral). The only persisted
state is:

- **GitHub Secrets** — the rotating QBO tokens (refreshed and written back each run).
- **QuickBooks Online** — the ledger itself, always re-read fresh.

This makes every run self-contained and idempotent with respect to reading: a run can
be repeated safely (it re-reads and re-sends), and the token chain is the only mutable
state it maintains.
