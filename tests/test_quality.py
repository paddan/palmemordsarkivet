"""Tester för quality.score_text."""

from __future__ import annotations

from quality import score_text


GOOD_TEXT = (
    "Detta är en helt vanlig svensk text som ska få en hög poäng. "
    "Den innehåller meningar med rimliga ord, kommatecken och punkter. "
    "Förhöret hölls i Stockholm den tjugoåttonde februari nittonhundraåttiosex. "
    "Vittnet berättade lugnt och sammanhängande om vad som hade hänt. "
    "Polisen antecknade noggrant alla detaljer som framkom under samtalet."
)

JUNK_TEXT = (
    "@#$%^&*() ~~~ |||| 1a 2b 3c 4d 5e 6f 7g 8h 9i 0j x y z q w "
    "ababab1 cd2 ef3 gh4 ij5 kl6 mn7 op8 qr9 st0 §§§ ¤¤¤ ¶¶¶"
)


def test_perfect_text_high_score() -> None:
    s = score_text(GOOD_TEXT, use_hunspell=False)
    assert s["score"] > 80, s


def test_junk_text_low_score() -> None:
    s = score_text(JUNK_TEXT, use_hunspell=False)
    assert s["score"] < 20, s


def test_empty_text() -> None:
    s = score_text("", use_hunspell=False)
    assert s["score"] == 0.0


# ---------------------------------------------------------------------------
# Inkrementellt urval via state.db (subprocess-körning av main)
# ---------------------------------------------------------------------------

def test_tesseract_only_row_gets_quality_score(tmp_path) -> None:
    """Regression: rad skapad av mark_tesseract_done (text_mtime NULL)
    hoppades tidigare över av quality-deltat — filen fick aldrig poäng."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db

    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (txt_dir / "a.txt").write_text(GOOD_TEXT, encoding="utf-8")

    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.mark_tesseract_done(conn, "a", pdf_path=str(files_dir / "a.pdf"),
                           source="files")
    conn.close()

    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-m", "quality",
         "--text-dir", str(txt_dir), "--files-dir", str(files_dir)],
        capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    conn = db.connect(db_path)
    row = conn.execute("SELECT score FROM quality WHERE pdf_stem='a'").fetchone()
    assert row is not None, "quality-rad saknas för tesseract-only-filen"
    mt = conn.execute(
        "SELECT text_mtime FROM pdf_files WHERE pdf_stem='a'"
    ).fetchone()
    assert mt["text_mtime"] is not None, "text_mtime ska stämplas så delta-logiken fungerar framåt"
