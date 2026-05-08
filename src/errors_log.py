"""Hjälpmodul för att skriva strukturerade fel till errors.log i projekt-roten."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOG_PATH = ROOT / "errors.log"


def log_error(component: str, item: str, message: str) -> None:
    """Skriv tidsstämplad rad till errors.log. Append-only, idempotent.

    Format: ``ISO8601\tcomponent\titem\tmessage`` (en rad, tab-separerat).
    Nylinjer i message ersätts med " | ".
    """
    ts = datetime.now().isoformat(timespec="seconds")
    msg = (message or "").replace("\r", " ").replace("\n", " | ")
    line = f"{ts}\t{component}\t{item}\t{msg}\n"
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line)
    except OSError:
        pass
