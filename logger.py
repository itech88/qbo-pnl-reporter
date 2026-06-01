"""
Shared logger for the QBO COGS reporter.

All modules import get_logger() and log against the "qbo" hierarchy.
Output goes to both stdout and a rotating file at logs/qbo_cogs.log.

Log levels:
  DEBUG   — verbose detail (token expiry checks, parse steps)
  INFO    — normal operational events (token refresh, API calls, pipeline steps)
  WARNING — unexpected but recoverable (missing section in P&L response)
  ERROR   — failures that abort a step or the pipeline
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "qbo_cogs.log")
_FMT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)

    root = logging.getLogger("qbo")
    root.setLevel(logging.DEBUG)

    # Rotating file — 5 MB per file, keep 3
    fh = RotatingFileHandler(_LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))

    # Console — INFO and above
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(_FMT, datefmt=_DATE_FMT))

    root.addHandler(fh)
    root.addHandler(ch)
    root.propagate = False

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a child logger under the 'qbo' hierarchy.
    Pass __name__ from the calling module.
    """
    _configure()
    # Strip the package prefix so logs show "qbo.auth", "qbo.fetcher", etc.
    short = name.split(".")[-1] if "." in name else name
    return logging.getLogger(f"qbo.{short}")
