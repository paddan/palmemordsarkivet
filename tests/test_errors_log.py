"""Tester för errors_log.log_error (format, nylinjeersättning, rotation)."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import errors_log


def _fresh_logger(tmp_path: Path, monkeypatch, *, max_bytes: int = 10_000_000):
    """Byt ut modulens _logger mot en helt egen, isolerad logger för testet.

    OBS: vi rör ALDRIG den riktiga "palme.errors"-loggerns handlers-lista —
    den är en processglobal singleton (logging.getLogger cachar på namn) och
    delas av hela test-sviten. Att rensa/mutera dess handlers skulle permanent
    förstöra riktig felloggning för resten av testkörningen. Vi skapar därför
    en NY logger med ett unikt namn (baserat på tmp_path) i stället.
    """
    log_path = tmp_path / "errors.log"
    test_logger = logging.getLogger(f"palme.errors.test.{tmp_path.name}")
    test_logger.handlers.clear()
    handler = logging.handlers.RotatingFileHandler(
        str(log_path), maxBytes=max_bytes, backupCount=5,
        encoding="utf-8", delay=True,
    )
    handler.setFormatter(errors_log._RawFormatter())
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False
    monkeypatch.setattr(errors_log, "_logger", test_logger)
    return log_path


def test_log_error_writes_tab_separated_line(tmp_path, monkeypatch) -> None:
    log_path = _fresh_logger(tmp_path, monkeypatch)
    errors_log.log_error("merge_pages", "doc.pdf", "något gick fel")

    rad = log_path.read_text(encoding="utf-8").strip()
    delar = rad.split("\t")
    assert len(delar) == 4
    ts, component, item, message = delar
    assert component == "merge_pages"
    assert item == "doc.pdf"
    assert message == "något gick fel"
    assert "T" in ts and len(ts) == 19  # ISO8601, sekundprecision


def test_log_error_replaces_newlines_with_pipe(tmp_path, monkeypatch) -> None:
    log_path = _fresh_logger(tmp_path, monkeypatch)
    errors_log.log_error("ocr", "x.pdf", "rad ett\nrad två\r\nrad tre")

    rader = log_path.read_text(encoding="utf-8").splitlines()
    assert len(rader) == 1  # ska bli EN rad i loggfilen, inte tre
    # \r\n blir "  | " : \r ersätts med mellanslag, sedan \n med " | ".
    assert "rad ett | rad två  | rad tre" in rader[0]


def test_log_error_appends_multiple_calls(tmp_path, monkeypatch) -> None:
    log_path = _fresh_logger(tmp_path, monkeypatch)
    errors_log.log_error("a", "1", "första")
    errors_log.log_error("b", "2", "andra")

    rader = log_path.read_text(encoding="utf-8").splitlines()
    assert len(rader) == 2
    assert "första" in rader[0]
    assert "andra" in rader[1]


def test_log_error_rotates_at_max_bytes(tmp_path, monkeypatch) -> None:
    log_path = _fresh_logger(tmp_path, monkeypatch, max_bytes=500)
    for i in range(50):
        errors_log.log_error("komponent", f"item{i}", "x" * 50)

    assert log_path.exists()
    rotated = log_path.with_name(log_path.name + ".1")
    assert rotated.exists(), "ingen rotation skedde trots att taket passerades"
