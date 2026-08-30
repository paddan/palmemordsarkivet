"""Tester för normalize_text: normalize-funktionen och db-baserad inkrementell logik."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from normalize_text import normalize, process_file

# ---------------------------------------------------------------------------
# normalize() — textomvandlingar
# ---------------------------------------------------------------------------

def test_normalize_ligatures():
    assert normalize("ﬁnns ﬀlera ﬂöde") == "finns fflera flöde"

def test_normalize_soft_hyphen_removed():
    assert "\xad" not in normalize("ord\xadet")

def test_normalize_multiple_blank_lines_collapsed():
    result = normalize("rad1\n\n\n\n\nrad2")
    assert result == "rad1\n\nrad2"

def test_normalize_idempotent():
    text = "ﬁnns\n\n\n\ntext   "
    assert normalize(normalize(text)) == normalize(text)

def test_normalize_pipe_only_lines_removed():
    result = normalize("text\n|   |   |\nmer text")
    assert "|" not in result

def test_normalize_preserves_form_feed():
    result = normalize("sida1\fsida2")
    assert "\f" in result


# ---------------------------------------------------------------------------
# process_file() — läser/skriver fil
# ---------------------------------------------------------------------------

def test_process_file_writes_changes(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("ﬁnns text", encoding="utf-8")
    changed = process_file(f)
    assert changed is True
    assert f.read_text(encoding="utf-8") == "finns text"

def test_process_file_no_change_returns_false(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("ren text utan ligaturer", encoding="utf-8")
    changed = process_file(f)
    assert changed is False

def test_process_file_dry_run_does_not_write(tmp_path):
    original = "ﬁnns text"
    f = tmp_path / "test.txt"
    f.write_text(original, encoding="utf-8")
    process_file(f, dry_run=True)
    assert f.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Inkrementell logik via state.db
# ---------------------------------------------------------------------------

def _run_main(args: list[str], txt_dir: Path, db_path: Path) -> str:
    """Kör normalize_text.main() via subprocess med given state.db."""
    import os
    import subprocess
    import sys
    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    result = subprocess.run(
        [sys.executable, "-m", "normalize_text", "--txt", str(txt_dir)] + args,
        capture_output=True, text=True, env=env,
    )
    return result.stdout + result.stderr


@pytest.fixture()
def txt_dir(tmp_path):
    d = tmp_path / "text"
    d.mkdir()
    return d


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "state.db"


def _connect(db_path: Path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db
    return db.connect(db_path)


def test_mark_normalized_after_run(txt_dir, db_path):
    """Efter en lyckad körning ska pdf_files.normalized_at vara satt."""
    (txt_dir / "a.txt").write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir, db_path)
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT normalized_at FROM pdf_files WHERE pdf_stem='a'"
    ).fetchone()
    assert row is not None
    assert row["normalized_at"] is not None


def test_unchanged_file_skipped_on_rerun(txt_dir, db_path):
    """Andra körningen utan filändringar ska hoppa över filen."""
    (txt_dir / "a.txt").write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir, db_path)
    # Inget filändras → andra körningen ska inte ha något att göra.
    out = _run_main([], txt_dir, db_path)
    assert "oförändrade" in out or "0/0" in out or "Inga" in out


def test_modified_file_reprocessed(txt_dir, db_path):
    """Om en fil ändras (text_mtime uppdateras av merge_pages) ska
    den re-normaliseras nästa körning."""
    f = txt_dir / "a.txt"
    f.write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir, db_path)
    # Simulera att merge_pages uppdaterat text_mtime till ett värde som
    # ligger garanterat efter normalized_at (sekundprecision). Vi använder
    # ett explicit framtida timestamp istället för att vänta på wall-clock.
    f.write_text("ﬁnns ny text", encoding="utf-8")
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db
    conn = db.connect(db_path)
    future = time.time() + 3600  # 1h framåt — garanterat > normalized_at
    db.mark_merged(conn, "a", text_mtime=future)
    conn.close()
    _run_main([], txt_dir, db_path)
    assert f.read_text(encoding="utf-8") == "finns ny text"


def test_rebuild_processes_all(txt_dir, db_path):
    f = txt_dir / "a.txt"
    f.write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir, db_path)
    out = _run_main(["--rebuild"], txt_dir, db_path)
    assert "0/0" not in out  # något ska processas


def test_dry_run_does_not_mark_normalized(txt_dir, db_path):
    """Dry-run ska inte sätta normalized_at i db."""
    (txt_dir / "a.txt").write_text("ﬁnns text", encoding="utf-8")
    _run_main(["--dry-run"], txt_dir, db_path)
    if not db_path.exists():
        return  # om db inte ens skapades är dry-run helt no-op — ok
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT normalized_at FROM pdf_files WHERE pdf_stem='a'"
    ).fetchone()
    if row is not None:
        assert row["normalized_at"] is None


def test_files_from_only_processes_listed(tmp_path, txt_dir, db_path):
    (txt_dir / "a.txt").write_text("ﬁnns text", encoding="utf-8")
    (txt_dir / "b.txt").write_text("ﬂöde text", encoding="utf-8")
    (txt_dir / "c.txt").write_text("ﬀlera ord", encoding="utf-8")

    files_list = tmp_path / "lista.txt"
    files_list.write_text("a.txt\nb.txt\n", encoding="utf-8")

    _run_main(["--files-from", str(files_list)], txt_dir, db_path)

    assert (txt_dir / "a.txt").read_text(encoding="utf-8") == "finns text"
    assert (txt_dir / "b.txt").read_text(encoding="utf-8") == "flöde text"
    assert (txt_dir / "c.txt").read_text(encoding="utf-8") == "ﬀlera ord"


def test_tesseract_only_row_is_not_skipped(txt_dir, db_path):
    """Regression: en rad skapad av mark_tesseract_done har text_mtime NULL.
    Sådana filer hoppades tidigare över av delta-urvalet trots att .txt
    aldrig normaliserats."""
    f = txt_dir / "a.txt"
    f.write_text("text\x00med styrtecken", encoding="utf-8")
    conn = _connect(db_path)
    import db
    db.init_schema(conn)
    db.mark_tesseract_done(conn, "a", pdf_path="downloaded/files/a.pdf",
                           source="files")
    conn.close()
    _run_main([], txt_dir, db_path)
    conn = _connect(db_path)
    row = conn.execute(
        "SELECT normalized_at, text_mtime FROM pdf_files WHERE pdf_stem='a'"
    ).fetchone()
    assert row["normalized_at"] is not None
    assert row["text_mtime"] is not None
    assert "\x00" not in f.read_text(encoding="utf-8")
