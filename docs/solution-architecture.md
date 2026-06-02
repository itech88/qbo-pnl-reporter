# Solution Architecture

The system is a config-driven, serverless reporting pipeline. There is no
always-on server: a scheduled GitHub Actions job is the only compute, and it runs
to completion and exits. All state between runs lives in GitHub Secrets (tokens)
and the QuickBooks Online ledger (the source of truth).

## Component diagram

```mermaid
flowchart TB
    subgraph Trigger["Trigger layer (GitHub Actions)"]
        CRON["Scheduled cron<br/>0 18 1,16 * *<br/>(monthly_report.yml)"]
        DISP["Manual dispatch<br/>report_*.yml (per report)"]
    end

    subgraph Runner["Ephemeral Ubuntu runner"]
        SCHED["scheduler.py<br/>(entry point / orchestrator)"]

        subgraph Config["Configuration"]
            YAML["reports/*.yaml<br/>10 report definitions"]
        end

        subgraph Auth["auth.py"]
            REFRESH["refresh_tokens()<br/>+ QBOSession (401 retry)"]
            WB["_persist_to_github_secrets()<br/>writeback via gh secret set"]
        end

        subgraph Fetch["Data fetch"]
            F1["fetcher.py<br/>ProfitAndLoss (summary)"]
            F2["vendor_fetcher.py<br/>ProfitAndLossDetail + memo fallback"]
        end

        subgraph Analyze["Analytics"]
            A1["analytics.py<br/>MoM · YoY · anomalies · scorecard stats"]
            A2["vendor_analytics.py<br/>vendor share · matrix · vendor YoY"]
        end

        subgraph Render["Rendering"]
            R1["report.py<br/>Jinja2 + matplotlib (PNG charts)"]
            TPL["templates/*.html"]
        end

        MAIL["mailer.py<br/>SMTP delivery + send_failure_alert"]
        LOG["logger.py<br/>rotating log + intuit_tid capture"]
    end

    subgraph External["External services"]
        QBO["QuickBooks Online<br/>Reports API"]
        GHS["GitHub Secrets<br/>(token store)"]
        SMTP["Gmail SMTP"]
        OWNER["Business owner<br/>inbox"]
    end

    CRON --> SCHED
    DISP --> SCHED
    SCHED --> YAML
    SCHED --> Auth
    SCHED --> Fetch
    Fetch <-->|"Bearer token, HTTPS"| QBO
    REFRESH <-->|"OAuth refresh"| QBO
    GHS -->|"tokens injected as env at job start"| Auth
    WB -->|"rotated tokens (PAT)"| GHS
    Fetch --> Analyze
    Analyze --> Render
    Render --> TPL
    Render --> MAIL
    MAIL -->|"HTML + inline chart"| SMTP --> OWNER
    MAIL -.->|"on failure"| OWNER
    Auth -.-> LOG
    Fetch -.-> LOG
    SCHED -.-> LOG
```

## Key architectural decisions

| Decision | Rationale |
|---|---|
| **Serverless / cron, not a webhook** | The job is a periodic snapshot, not event-driven. No server to host, patch, or keep alive. |
| **Fetch P&L once per run** | One 6-call fetch feeds all summary reports; the vendor report adds its own detail fetch only when needed. Avoids hammering the QBO API. |
| **Config-driven reports** | Adding a metric is a YAML file, not code. Extraction strategy (`section_summary` / `line_item` / `subsection_summary` / `vendor_breakdown`) is declared per report. |
| **Token writeback to GitHub Secrets** | Runners are ephemeral; the rotated refresh token must persist somewhere durable the next run can read. Secrets are the store; a PAT grants write. |
| **Failure alerting in two layers** | In-process alerts give detail (which report, traceback); a workflow-level `if: failure()` net catches what the process can't self-report. |
| **`intuit_tid` on every call** | Captured in logs so any QBO support ticket can reference the exact transaction id. |
| **Per-report manual workflows** | Each report can be re-run on demand without waiting for the schedule; the combined cron run respects `trigger_days`. |

## Trust & secrets boundary

- **QBO OAuth tokens** (access/refresh/expiry) and **SMTP credentials** live only in
  GitHub Secrets, injected as environment variables at job start.
- **`GH_PAT`** (fine-grained, `Secrets: write` + `Metadata: read`) is the only
  credential that cannot auto-rotate; it authorizes the token writeback. See the
  runbook in the README for its expiry/recovery procedure.
- Tokens are fed to `gh secret set` via **stdin**, never on the command line, so they
  never appear in the runner's process arguments.
- `.env` is git-ignored; secrets never enter the repo.
