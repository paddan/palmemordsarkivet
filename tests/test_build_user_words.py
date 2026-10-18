"""Tester för run_build_user_words (inkrementell cache, felvägar)."""

from __future__ import annotations

from pathlib import Path

import build_user_words
from operations.context import ensure_terminal_context


def _run(text_dir: Path, out: Path, user_words: Path, *, rebuild: bool = False,
         min_freq: int = 30) -> int:
    return build_user_words.run_build_user_words(
        text_dir=text_dir, out=out, user_words=user_words,
        min_freq=min_freq, rebuild=rebuild, context=ensure_terminal_context(None),
    )


def test_happy_path_writes_words_above_threshold(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("mordet " * 35 + "enstaka ", encoding="utf-8")
    out = tmp_path / "out.txt"
    user_words = tmp_path / "missing.user-words"

    rc = _run(text_dir, out, user_words)

    assert rc == 0
    words = set(out.read_text(encoding="utf-8").split())
    assert "mordet" in words
    assert "enstaka" not in words  # under tröskeln 30


def test_missing_text_dir_returns_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    rc = _run(tmp_path / "saknas", tmp_path / "out.txt", tmp_path / "uw.txt")
    assert rc == 1


def test_incremental_skips_unchanged_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("alfabet " * 31, encoding="utf-8")
    out = tmp_path / "out.txt"
    uw = tmp_path / "uw.txt"

    _run(text_dir, out, uw)
    state_after_first = build_user_words._load_state(tmp_path / "state.json")
    assert state_after_first["mtimes"]["a.txt"] is not None

    rc = _run(text_dir, out, uw)
    assert rc == 0
    words = set(out.read_text(encoding="utf-8").split())
    assert "alfabet" in words


def test_removed_file_only_loses_its_own_words_not_whole_cache(
    tmp_path: Path, monkeypatch,
) -> None:
    # Regression: en borttagen fil ska inte tvinga en fullständig omläsning av
    # alla andra filer eller tappa deras ord ur cachen.
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("alfabet " * 31, encoding="utf-8")
    (text_dir / "b.txt").write_text("bokstav " * 31, encoding="utf-8")
    out = tmp_path / "out.txt"
    uw = tmp_path / "uw.txt"

    _run(text_dir, out, uw)
    (text_dir / "b.txt").unlink()

    rc = _run(text_dir, out, uw)
    assert rc == 0

    state = build_user_words._load_state(tmp_path / "state.json")
    assert "a.txt" in state["mtimes"]
    assert "b.txt" not in state["mtimes"]
    assert "b.txt" not in state["file_words"]
    assert "a.txt" in state["file_words"]

    words = set(out.read_text(encoding="utf-8").split())
    assert "alfabet" in words
    assert "bokstav" not in words


def test_rebuild_ignores_cache(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("alfabet " * 31, encoding="utf-8")
    out = tmp_path / "out.txt"
    uw = tmp_path / "uw.txt"

    _run(text_dir, out, uw)
    rc = _run(text_dir, out, uw, rebuild=True)
    assert rc == 0


def test_merges_with_existing_user_words(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_user_words, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("alfabet " * 31, encoding="utf-8")
    out = tmp_path / "out.txt"
    uw = tmp_path / "uw.txt"
    uw.write_text("befintligtord\n", encoding="utf-8")

    _run(text_dir, out, uw)

    words = set(out.read_text(encoding="utf-8").split())
    assert "befintligtord" in words
    assert "alfabet" in words


def test_corrupt_state_cache_is_logged_and_rebuilt(tmp_path: Path, monkeypatch) -> None:
    # Trasig cache-JSON ska inte krascha körningen; cachen byggs om och felet loggas.
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(build_user_words, "STATE_FILE", state_file)
    monkeypatch.setattr(build_user_words, "has_hunspell_swe", lambda: False)
    rader: list[tuple] = []
    monkeypatch.setattr(build_user_words, "log_error", lambda *a: rader.append(a))
    state_file.write_text("{ inte giltig json", encoding="utf-8")

    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("alfabet " * 31, encoding="utf-8")
    out = tmp_path / "out.txt"

    rc = _run(text_dir, out, tmp_path / "uw.txt")

    assert rc == 0
    assert "alfabet" in set(out.read_text(encoding="utf-8").split())
    assert len(rader) == 1
    assert rader[0][0] == "build_user_words"
    assert rader[0][1] == "state.json"
    assert "JSONDecodeError" in rader[0][2]
