"""
Environment-variable readers that tolerate *empty* values.

GitHub Actions injects an **empty string** for any secret that is referenced in a
workflow but not actually set. That makes the obvious idiom a landmine:

    float(os.getenv("COGS_VARIANCE_THRESHOLD", "0.05"))   # ValueError on ""

The default only applies when the variable is *absent* — an empty string sails
past it and blows up in the converter, aborting the whole unattended run before a
single report is built (RB-3). These helpers use the `os.getenv(...) or default`
idiom instead (the one guardrails.tolerance() already used correctly), so a
missing *or empty* value falls back to the default.

They read at call time, not import time, so tests and reruns see env changes and
a bad value can never kill the process at import.

A non-empty but unparseable value (a typo like SMTP_PORT="58a") still raises —
that is a real misconfiguration and should fail loudly rather than be silently
swallowed by a default.
"""

import os


def env_str(name: str, default: str) -> str:
    """Value of `name`; `default` when unset or empty."""
    return os.getenv(name) or default


def env_float(name: str, default: float) -> float:
    """Float value of `name`; `default` when unset or empty."""
    return float(os.getenv(name) or default)


def env_int(name: str, default: int) -> int:
    """Int value of `name`; `default` when unset or empty."""
    return int(os.getenv(name) or default)
