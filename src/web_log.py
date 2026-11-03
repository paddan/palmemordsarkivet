"""Logg för Streamlit-sidorna (Utredning, Sidgranskning, citatlänkar).

Bakgrundsjobb loggar till sin jobblogg via ``OperationContext.log``, men
webbsidorna körs i Streamlit-processen och hade bara Pythons ``logging`` —
deras debugrader hamnade ingenstans. Den här modulen skriver dem till
``generated/web.log`` i samma format som jobbloggarna
(``<ISO-tidsstämpel> [nivå] <logger>: meddelande``), så att Admin → Logg kan
visa och nivåfiltrera dem som källan *Webbsidorna*.

Debugrader skrivs bara när debugloggning är på: växlingen i Admin → Logg
(``debug_log`` i ``generated/admin_settings.json``) eller ``PALME_DEBUG_LOG``
i miljön. Inställningen läses vid varje debugrad (med mtime-cache), så
växlingen gäller direkt utan omstart av Streamlit.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path

from operations.context import debug_logging_enabled, debug_value_enabled

ROOT = Path(__file__).resolve().parents[1]
WEB_LOG_PATH = ROOT / "generated" / "web.log"
SETTINGS_FILE = ROOT / "generated" / "admin_settings.json"
LOGGER_NAME = "palme.web"

_MAX_BYTES = 5_000_000
_BACKUP_COUNT = 3

# ((mtime_ns, storlek), påslagen) för SETTINGS_FILE — läses om bara när filen ändrats.
_settings_cache: tuple[tuple[int, int], bool] | None = None


def _debug_from_settings() -> bool:
    global _settings_cache
    try:
        stat = SETTINGS_FILE.stat()
    except OSError:
        return False
    stamp = (stat.st_mtime_ns, stat.st_size)
    if _settings_cache is None or _settings_cache[0] != stamp:
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        enabled = isinstance(data, dict) and debug_value_enabled(data.get("debug_log"))
        _settings_cache = (stamp, enabled)
    return _settings_cache[1]


def debug_enabled() -> bool:
    """Sant när webbsidornas debugrader ska skrivas."""
    return debug_logging_enabled() or _debug_from_settings()


class _DebugGate(logging.Filter):
    """Släpper igenom debugrader bara när debugloggning är på."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno > logging.DEBUG or debug_enabled()


class _JobLogFormatter(logging.Formatter):
    """Samma radformat som jobbloggarna (se operations.worker.JobSink)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).replace(microsecond=0).isoformat()
        name = record.name.removeprefix(f"{LOGGER_NAME}.")
        message = record.getMessage().replace("\r", " ").replace("\n", " | ")
        return f"{ts} [{record.levelname.lower()}] {name}: {message}"


def install(path: Path | None = None) -> logging.Logger:
    """Koppla webbloggens filhanterare (en gång per process)."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    target = Path(path or WEB_LOG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(target), maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
        encoding="utf-8", delay=True,
    )
    handler.setFormatter(_JobLogFormatter())
    handler.addFilter(_DebugGate())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    """Logger för en webbsida eller modul, t.ex. ``get_logger("citations")``."""
    install()
    return logging.getLogger(f"{LOGGER_NAME}.{name}")
