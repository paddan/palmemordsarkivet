"""Tester för den gemensamma läsningshistoriken (``operations.page_readings``)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402
from operations.context import OperationContext  # noqa: E402
from page_review import WHOLE_FILE_ENGINE  # noqa: E402

STEM = "doc"


class _Sink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.upsert_pdf_file(conn, pdf_stem=STEM, source="palme", pdf_path="x.pdf")
    conn.close()
    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: False, debug=True)
    return tmp_path, ctx, sink


def _record(engine: str, text: str, score: float = 50.0) -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_page(
        conn, pdf_stem=STEM, page_num=1, engine=engine, text=text, score=score,
        markdown=f"# {text}", layout_json="[]", engine_identity=f"{engine}-id",
    )
    conn.close()


def test_comparison_score_uses_normalized_text() -> None:
    from operations.page_readings import comparison_score

    assert comparison_score("ﬁnns det en ﬁn version") == comparison_score(
        "finns det en fin version")


def test_reject_new_reading_restores_old_and_archives_new_as_rejected(env) -> None:
    from operations.page_readings import reject_new_reading

    root, ctx, sink = env
    _record("surya", "god text")
    conn = state_db.connect()
    state_db.init_schema(conn)
    old_id = state_db.archive_page_reading(
        conn, pdf_stem=STEM, page_num=1, engine="surya", text="god text",
        markdown="# god text", layout_json="[]", engine_identity="surya-id",
        score=50.0, reason="rescan",
    )
    conn.close()
    _record("tesseract", "skräp", 5.0)
    conn = state_db.connect()
    old = dict(state_db.get_page_reading(conn, old_id))
    conn.close()

    new_id = reject_new_reading(root, STEM, 1, old, ctx)

    conn = state_db.connect()
    row = state_db.get_page_row(conn, STEM, 1)
    rejected = state_db.get_page_reading(conn, new_id)
    conn.close()
    assert row["engine"] == "surya" and row["text"] == "god text"
    assert row["markdown"] == "# god text" and row["engine_identity"] == "surya-id"
    assert rejected["reason"] == "rejected"
    assert rejected["text"] == "skräp" and rejected["engine"] == "tesseract"
    assert any(level == "debug" and "surya" in msg and "tesseract" in msg
               for level, msg in sink.lines)


def test_reject_whole_file_reading_keeps_a_page_row(env) -> None:
    from operations.page_readings import reject_new_reading

    root, ctx, _ = env
    _record("tesseract", "ny")
    old = {"id": 7, "engine": WHOLE_FILE_ENGINE, "text": "filens sida", "markdown": None,
           "layout_json": None, "engine_identity": None, "score": None}

    reject_new_reading(root, STEM, 1, old, ctx)

    conn = state_db.connect()
    row = state_db.get_page_row(conn, STEM, 1)
    exists = state_db.page_exists(conn, STEM, 1)
    conn.close()
    assert row is not None and row["engine"] == WHOLE_FILE_ENGINE
    assert row["text"] == "filens sida"
    assert exists
