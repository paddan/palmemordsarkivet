"""Hjälpmodul för att skriva strukturerade fel till errors.log i projekt-roten."""

from __future__ import annotations

import logging
import logging.handlers
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "errors.log"

_MAX_BYTES = 10_000_000  # 10 MB per fil
_BACKUP_COUNT = 5


class _RawFormatter(logging.Formatter):
    """Skriver bara meddelandet — ingen extra timestamp/nivå från logging."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _make_logger() -> logging.Logger:
    logger = logging.getLogger("palme.errors")
    if logger.handlers:
        return logger
    handler = logging.handlers.RotatingFileHandler(
        str(LOG_PATH),
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(_RawFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    return logger


_logger = _make_logger()


def log_error(component: str, item: str, message: str) -> None:
    """Skriv tidsstämplad rad till errors.log. Roteras automatiskt vid 10 MB.

    Format: ``ISO8601\\tcomponent\\titem\\tmessage`` (en rad, tab-separerat).
    Nylinjer i message ersätts med " | ".
    """
    ts = datetime.now().isoformat(timespec="seconds")
    msg = (message or "").replace("\r", " ").replace("\n", " | ")
    _logger.error("%s\t%s\t%s\t%s", ts, component, item, msg)
