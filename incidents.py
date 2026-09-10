"""
Durable incident log — every failed, held or self-healed report, with reasoning.

The runner is ephemeral: Actions logs age out, and an emailed alert is a
notification, not a record. Neither answers the question you actually ask three
months later — *has this broken before, and did we ever work out why?* So each
run buffers its incidents in memory and appends one dated section to
`docs/incident-log.md`, which CI commits back to the repo. The log is the
system's memory of its own failures.

Every entry carries four fields, because a bare error string is not a record:

    What happened  — the observable signal (status code, guardrail reason, traceback)
    Why            — the mechanism behind that signal, classified where we can
    What we did    — self-heal attempted, report withheld, run aborted
    Outcome        — resolved automatically, or waiting on a human

`explain()` does the classification. It maps signatures we have actually seen in
production to a real explanation, and falls back to an honest "unrecognised"
rather than inventing a cause — a confident wrong diagnosis in a log that
outlives the incident is worse than no diagnosis.

Recording never raises. An incident log that can break the run it is recording
would be worse than no log at all.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from logger import get_logger

log = get_logger(__name__)

LOG_PATH = os.path.join(os.path.dirname(__file__), "docs", "incident-log.md")

# The header a freshly created log opens with. Kept in one place so the append
# path can create the file without a template drifting out of sync.
_LOG_HEADER = """# Incident Log

Every run that fails, holds a report, or self-heals appends a section here. Newest
entries are at the bottom. Written by `incidents.py`; committed by the report
workflow. Do not hand-edit an existing entry — add a follow-up note underneath it
instead, so the record of what the system saw at the time stays intact.

Entry kinds:

| Kind | Meaning |
|---|---|
| `abort` | The pipeline died before delivering anything. No reports went out. |
| `report_failure` | One report raised an exception. The others still delivered. |
| `guardrail_hold` | Numbers failed reconciliation, so the report was withheld rather than sent wrong. |
| `self_heal` | A fault was detected and repaired automatically. The report delivered. |
| `degraded` | The report delivered, but from a reduced data set. |

---
"""

KINDS = ("abort", "report_failure", "guardrail_hold", "self_heal", "degraded")


@dataclass(frozen=True)
class Incident:
    """One thing that went wrong (or was put right) during a run."""

    kind: str
    summary: str
    reasoning: str
    remedy: str
    report: str | None = None
    resolved: bool = False
    occurred_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"Unknown incident kind {self.kind!r}; choose from {KINDS}")


# Module-level buffer. One run writes one section, so incidents accumulate here
# and are flushed once at the end rather than reopening the file per event.
_buffer: list[Incident] = []


def reset() -> None:
    """Clear the buffer. Called at the start of each run so a long-lived process
    (tests, a local REPL) can't leak incidents between runs."""
    _buffer.clear()


def record(incident: Incident) -> None:
    """Buffer an incident. Never raises — see the module docstring."""
    try:
        _buffer.append(incident)
        log.info(
            "Incident recorded — kind=%s report=%s resolved=%s: %s",
            incident.kind, incident.report or "-", incident.resolved, incident.summary,
        )
    except Exception:  # noqa: BLE001 — recording must never break the run
        log.exception("Failed to record an incident (continuing).")


def recorded() -> list[Incident]:
    """The incidents buffered so far, in the order they happened."""
    return list(_buffer)


# ---------------------------------------------------------------------------
# Classification — turning a signal into a cause
# ---------------------------------------------------------------------------

# Ordered most-specific first: the first pattern that matches wins, so a generic
# "500" rule must never sit above a rule that recognises *which* 500 this is.
_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    (
        r"aging (?:total|summary).*(?:≠|!=).*Balance Sheet",
        "The aging summary endpoint returned a total that disagrees with the "
        "Balance Sheet's own A/R or A/P line for the same date. The Balance Sheet "
        "is the ledger of record, so the summary is the side that is wrong — "
        "usually because the summary report returned no per-party rows at all, "
        "not because it returned wrong amounts.",
    ),
    (
        r"detail total.*(?:≠|!=).*aging summary",
        "The per-document detail and the bucketed summary are two endpoints over "
        "the same receivables. When they disagree, one of them was parsed wrongly "
        "or the two were read mid-sync, so neither can be trusted on its own.",
    ),
    (
        r"vendor detail total.*(?:≠|!=).*COGS summary",
        "The per-vendor breakdown does not add up to the COGS total from the "
        "summary P&L. Typically an expense posted without a vendor, so it lands in "
        "the summary but not in any vendor's column.",
    ),
    (
        r"no reconciliation anchor",
        "Both independent checks were unavailable at once: the Balance Sheet line "
        "and the detail endpoint. With nothing to reconcile against, the number "
        "cannot be verified, so it is withheld rather than sent unchecked.",
    ),
    (
        r"Income − COGS ≠ Gross Profit|Gross Profit − Operating Expenses ≠",
        "The P&L does not cross-foot: the section totals QuickBooks returned are "
        "not internally consistent for the reporting month. That points at a "
        "parsing or extraction break rather than at the business's actual numbers.",
    ),
    (
        r"outside the -200%\.\.200% sanity band",
        "The reported value is an implausible multiple of income for a completed "
        "month, which is the signature of a broken line-item extraction rather "
        "than a genuinely extreme month.",
    ),
    (
        r"no data row for \d{4}-\d{2}",
        "The reporting month is missing from the built data frame entirely, so "
        "there is nothing to report on. Usually an account renamed in the "
        "QuickBooks chart of accounts, so the configured line item no longer matches.",
    ),
    (
        r"\b(?:429|Retry-After)\b",
        "QuickBooks throttled the request. This is a rate limit, not a fault in "
        "the data, and it clears on its own within seconds.",
    ),
    (
        r"\b50[0234]\b.*(?:Server Error|errorCode.?.?20001|Something went wrong)",
        "Intuit returned a generic server error. It answers both genuine backend "
        "faults and throttled requests this way, and a response that arrives in "
        "well under a second is almost always a throttle rather than a real "
        "failure to compute the report.",
    ),
    (
        r"invalid_grant",
        "The refresh token was rejected. The token chain is broken, which happens "
        "when a rotated token failed to persist or two runs rotated concurrently. "
        "This does not clear on its own and needs a re-consent.",
    ),
    (
        r"Accessing Wrong Cluster|code=.?130",
        "Intuit routed the request to the wrong cluster. It is a routing fault on "
        "their side, not an authentication problem, and it affects particular "
        "report endpoints for a given realm.",
    ),
    (
        r"ConnectionError|Timeout|timed out",
        "The network call did not complete. Indistinguishable from a transient "
        "Intuit blip from this side, and treated the same way.",
    ),
    (
        r"not found in P&L response",
        "A configured line item does not match any account name in the P&L that "
        "QuickBooks returned. The account was most likely renamed or "
        "re-parented in the chart of accounts.",
    ),
)


def explain(signal: str) -> str:
    """The mechanism behind a raw failure signal, or an honest admission that we
    do not recognise it. Never guesses: an unrecognised signature says so."""
    text = signal or ""
    for pattern, explanation in _EXPLANATIONS:
        if re.search(pattern, text, re.IGNORECASE):
            return explanation
    return (
        "Not a signature this log recognises yet. The raw signal is recorded "
        "above verbatim; classify it here once the cause is known."
    )


# ---------------------------------------------------------------------------
# Rendering and persistence
# ---------------------------------------------------------------------------

def _render_entry(incident: Incident) -> str:
    scope = incident.report or "pipeline"
    outcome = (
        "Resolved automatically. No action needed."
        if incident.resolved
        else "Unresolved. Needs an operator."
    )
    return (
        f"### `{incident.kind}` · {scope}\n\n"
        f"- **What happened:** {incident.summary}\n"
        f"- **Why:** {incident.reasoning}\n"
        f"- **What the system did:** {incident.remedy}\n"
        f"- **Outcome:** {outcome}\n"
    )


def render(run_id: str, env: str, incidents: list[Incident],
           run_url: str | None = None) -> str:
    """The full markdown section for one run. Empty string when nothing happened,
    so a clean run adds nothing to the log."""
    if not incidents:
        return ""

    stamp = incidents[0].occurred_at.strftime("%Y-%m-%d %H:%M UTC")
    healed = sum(1 for i in incidents if i.resolved)
    counts = f"{len(incidents)} incident{'s' if len(incidents) != 1 else ''}"
    if healed:
        counts += f", {healed} self-healed"

    head = f"## {stamp} — run `{run_id}` ({env}) — {counts}\n"
    if run_url:
        head += f"\n[Actions run]({run_url})\n"

    body = "\n".join(_render_entry(i) for i in incidents)
    return f"\n{head}\n{body}\n---\n"


def flush(run_id: str, env: str, path: str | None = None,
          run_url: str | None = None) -> bool:
    """Append this run's incidents to the log. Returns True when something was
    written. Never raises: a log that can break the run is worse than no log."""
    path = path or LOG_PATH
    try:
        section = render(run_id, env, recorded(), run_url=run_url)
        if not section:
            return False

        os.makedirs(os.path.dirname(path), exist_ok=True)
        exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8") as fh:
            if not exists:
                fh.write(_LOG_HEADER)
            fh.write(section)

        log.info("Incident log updated (%d entries) → %s", len(recorded()), path)
        return True
    except Exception:  # noqa: BLE001 — logging must never break the run
        log.exception("Could not write the incident log (continuing).")
        return False
