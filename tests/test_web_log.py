"""Tester för webbsidornas logg (src/web_log.py)."""

from __future__ import annotations

import json
import logging

import pytest

import web_log


@pytest.fixture
def web_logger(tmp_path, monkeypatch):
    """En nyinstallerad webblogg i tmp_path, utan debug i miljön."""
    monkeypatch.delenv("PALME_DEBUG_LOG", raising=False)
    monkeypatch.setattr(web_log, "SETTINGS_FILE", tmp_path / "admin_settings.json")
    root = logging.getLogger(web_log.LOGGER_NAME)
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    web_log.install(tmp_path / "web.log")
    yield tmp_path / "web.log"
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    for handler in saved:
        root.addHandler(handler)


def _read(path) -> str:
    for handler in logging.getLogger(web_log.LOGGER_NAME).handlers:
        handler.flush()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_lines_use_the_job_log_format(web_logger) -> None:
    web_log.get_logger("citations").info("avkortat id löst")

    text = _read(web_logger)
    assert "[info] citations: avkortat id löst" in text
    # Samma prefix som jobbloggarna, så Admin → Logg kan filtrera på nivå.
    assert text.split(" ", 1)[0].count("T") == 1


def test_debug_lines_follow_the_admin_toggle(web_logger, monkeypatch) -> None:
    log = web_log.get_logger("citations")
    log.debug("dold")
    assert "dold" not in _read(web_logger)

    # Växlingen i Admin → Logg sparas i admin_settings.json.
    web_log.SETTINGS_FILE.write_text(json.dumps({"debug_log": "1"}), encoding="utf-8")
    log.debug("synlig via inställningen")
    assert "[debug] citations: synlig via inställningen" in _read(web_logger)

    web_log.SETTINGS_FILE.write_text(json.dumps({"debug_log": ""}), encoding="utf-8")
    monkeypatch.setenv("PALME_DEBUG_LOG", "1")
    log.debug("synlig via miljön")
    assert "synlig via miljön" in _read(web_logger)


def test_install_is_idempotent(web_logger) -> None:
    web_log.install(web_logger)
    web_log.get_logger("x").warning("en gång")
    assert _read(web_logger).count("en gång") == 1


def test_messages_do_not_reach_the_root_logger(web_logger, caplog) -> None:
    with caplog.at_level(logging.DEBUG):
        web_log.get_logger("x").warning("bara i webbloggen")
    assert "bara i webbloggen" not in caplog.text
