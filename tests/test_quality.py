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


def test_top_shows_worst_when_nothing_to_score(tmp_path) -> None:
    """Regression: --top visade ingenting när alla filer redan var aktuella —
    run_quality returnerade innan rapporten skrevs."""
    import os
    import subprocess
    import sys

    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (txt_dir / "bra.txt").write_text(GOOD_TEXT, encoding="utf-8")
    (txt_dir / "skrap.txt").write_text(JUNK_TEXT, encoding="utf-8")

    env = os.environ.copy()
    env["STATE_DB"] = str(tmp_path / "state.db")
    argv = [sys.executable, "-m", "quality",
            "--text-dir", str(txt_dir), "--files-dir", str(files_dir)]
    first = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr

    second = subprocess.run(argv + ["--top", "1"], capture_output=True, text=True, env=env)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert "inget att göra" in out
    assert "Värsta 1 (från quality-tabellen)" in out, out
    assert "skrap.txt" in out and "bra.txt" not in out.split("Värsta 1")[1], out


def test_histogram_counts_documents_and_pages(tmp_path) -> None:
    """--histogram skriver fördelningen i 10-poängsintervall för dokument och
    sidor (bildsidor räknas separat), även när inget behöver poängsättas."""
    import os
    import subprocess
    import sys

    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    files_dir = tmp_path / "files"
    files_dir.mkdir()
    (txt_dir / "bra.txt").write_text(GOOD_TEXT, encoding="utf-8")
    (txt_dir / "skrap.txt").write_text(JUNK_TEXT + "\f" + GOOD_TEXT + "\f", encoding="utf-8")

    env = os.environ.copy()
    env["STATE_DB"] = str(tmp_path / "state.db")
    argv = [sys.executable, "-m", "quality",
            "--text-dir", str(txt_dir), "--files-dir", str(files_dir)]
    first = subprocess.run(argv + ["--per-page"], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr

    second = subprocess.run(argv + ["--histogram"], capture_output=True, text=True, env=env)
    out = second.stdout + second.stderr
    assert second.returncode == 0, out
    assert "Kvalitetsfördelning — dokument (2 st)" in out, out
    # 3 textsidor (skräp, bra, bra) + 1 tom sida = bildsida
    assert "Kvalitetsfördelning — sidor (3 st, 1 bildsidor utelämnade)" in out, out
    rows = [line.split() for line in out.splitlines() if line.strip().startswith("0–9")]
    assert rows and rows[0][1] == "1", out  # skräpdokumentet resp. skräpsidan
    assert any(line.strip().startswith("90–100") for line in out.splitlines()), out


def test_histogram_shows_scores_per_engine(tmp_path) -> None:
    """--histogram visar även poäng per OCR-motor: sidor med egen pdf_pages-rad
    får radens motor (surya → surya-lines), övriga lästes av Tesseract som hel
    fil (eller kommer från PDF:ens textlager)."""
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
    (txt_dir / "a.txt").write_text(JUNK_TEXT + "\f" + GOOD_TEXT, encoding="utf-8")

    db_path = tmp_path / "state.db"
    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    argv = [sys.executable, "-m", "quality",
            "--text-dir", str(txt_dir), "--files-dir", str(files_dir)]
    first = subprocess.run(argv + ["--per-page"], capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = db.connect(db_path)
    db.init_schema(conn)
    db.record_page(conn, pdf_stem="a", page_num=1, engine="surya", text=JUNK_TEXT, score=None)
    conn.close()

    out = subprocess.run(argv + ["--histogram"], capture_output=True, text=True, env=env)
    text = out.stdout + out.stderr
    assert out.returncode == 0, text
    assert "Poäng per OCR-motor — sidor" in text, text
    rows = {line.split()[0]: line.split() for line in text.splitlines()
            if line.strip().startswith(("surya", "tesseract", "hel-fils"))
            and line.split()[1].isdigit()}
    assert "surya" not in rows and "hel-fils-OCR" not in rows, text
    assert rows["surya-lines"][1] == "1", text  # skräpsidan har egen rad
    assert rows["tesseract"][1] == "1", text    # den bra sidan har ingen
    assert "Poäng per källtyp — dokument" in text, text
