"""Test-fixtures."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "rag"))


@pytest.fixture(autouse=True)
def _isolate_project_state(tmp_path_factory, monkeypatch):
    """Peka state.db och jobbloggarna på en temporär katalog i varje test.

    ``db.connect()``, ``job_service.default_db_path()`` och
    ``job_service.default_log_root()`` läser ``STATE_DB`` respektive
    ``ADMIN_JOB_LOG_ROOT`` vid anropstillfället, så ett test som glömmer att sätta
    dem skulle annars skriva i projektets riktiga ``generated/``. Tester som sätter
    egna värden gör det senare (i testet eller i en egen fixture) och vinner.
    Variablerna ärvs av underprocesser som testerna startar.
    """
    state_dir = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("STATE_DB", str(state_dir / "state.db"))
    monkeypatch.setenv("ADMIN_JOB_LOG_ROOT", str(state_dir / "admin_jobs"))

    # errors.log: modulens logger öppnar generated/errors.log vid import, så den
    # byts mot en egen logger per test (den riktiga loggerns handlers rörs aldrig).
    import errors_log

    log_path = state_dir / "errors.log"
    test_logger = logging.getLogger(f"palme.errors.pytest.{state_dir.name}")
    test_logger.handlers.clear()
    handler = logging.FileHandler(str(log_path), encoding="utf-8", delay=True)
    handler.setFormatter(errors_log._RawFormatter())
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False
    monkeypatch.setattr(errors_log, "_logger", test_logger)
    monkeypatch.setattr(errors_log, "LOG_PATH", log_path)
    yield
    handler.close()
    test_logger.removeHandler(handler)


@pytest.fixture
def tiny_pdf(tmp_path: Path) -> Path:
    """Skapa en enkel PDF med pymupdf. Skipas om pymupdf saknas."""
    pymupdf = pytest.importorskip("pymupdf")
    pdf_path = tmp_path / "tiny.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hej världen från testet")
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


def _guard_empty(session) -> None:
    """Vägra köra Neo4j-tester mot en databas som redan innehåller noder."""
    antal = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    if antal:
        pytest.fail("Testdatabasen är inte tom — vägrar köra Neo4j-tester mot den.")


@pytest.fixture
def neo4j_session():
    """Session mot en tom testdatabas (NEO4J_TEST_URI); rensas efter testet.

    Använder aldrig standardadressen: saknas NEO4J_TEST_URI hoppas testet över
    (eller faller med PALME_REQUIRE_NEO4J_TESTS=1, som CI sätter).
    """
    import os

    uri = os.environ.get("NEO4J_TEST_URI")
    if not uri:
        if os.environ.get("PALME_REQUIRE_NEO4J_TESTS") == "1":
            pytest.fail("NEO4J_TEST_URI saknas men PALME_REQUIRE_NEO4J_TESTS=1.")
        pytest.skip("NEO4J_TEST_URI saknas — hoppar över Neo4j-test")
    from neo4j import GraphDatabase

    user = os.environ.get("NEO4J_TEST_USER", "neo4j")
    password = os.environ.get("NEO4J_TEST_PASSWORD", "")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as session:
            _guard_empty(session)
            try:
                yield session
            finally:
                session.run("MATCH (n) DETACH DELETE n").consume()
    finally:
        driver.close()
