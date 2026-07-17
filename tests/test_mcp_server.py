"""Tester för mcp_server.get_page — sidhämtning och sökvägsvalidering."""

from __future__ import annotations

from pathlib import Path

import pytest

import ask
import mcp_server


@pytest.fixture()
def text_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "text"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "_text_dir", d)
    return d


def test_get_page_returns_requested_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("sida ett\fsida två\fsida tre",
                                      encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "sida två" in out
    assert "sida ett" not in out
    assert "[doc.txt, sida 2]" in out


def test_get_page_out_of_range(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("bara en sida", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 5)
    assert "finns inte" in out
    assert "har 1 sidor" in out


def test_get_page_missing_file(text_dir: Path) -> None:
    out = mcp_server.get_page("saknas.txt", 1)
    assert "Hittade inte" in out


def test_get_page_empty_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("text\f\ftext", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "är tom" in out


def test_get_page_rejects_path_traversal(text_dir: Path) -> None:
    secret = text_dir.parent / "hemlig.txt"
    secret.write_text("hemligt", encoding="utf-8")
    out = mcp_server.get_page("../hemlig.txt", 1)
    assert "hemligt" not in out


def test_clamp_result_limits_handles_minimums_and_bad_values() -> None:
    assert mcp_server.clamp_result_limits(0, -10) == (5, 1)
    assert mcp_server.clamp_result_limits("många", None) == (20, 6)


def test_search_archive_clamps_large_result_limits(monkeypatch) -> None:
    seen: dict[str, int] = {}
    hits = [
        {"nr": "1", "page": 1, "titel": "Titel", "text": f"träff {i}"}
        for i in range(60)
    ]
    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())

    def fake_search_hybrid(table, model, query: str, top_k: int) -> list[dict]:
        seen["top_k"] = top_k
        return hits

    def fake_rerank(query: str, found: list[dict], top_n: int) -> list[dict]:
        seen["top_n"] = top_n
        return found[:top_n]

    def fake_format_context(found: list[dict]) -> str:
        seen["formatted"] = len(found)
        return "kontext"

    monkeypatch.setattr(ask, "search_hybrid", fake_search_hybrid)
    monkeypatch.setattr(ask, "rerank", fake_rerank)
    monkeypatch.setattr(ask, "format_context", fake_format_context)

    mcp_server.search_archive("fråga", top_k=9999, top_n=9999)

    assert seen == {"top_k": 50, "top_n": 15, "formatted": 15}
