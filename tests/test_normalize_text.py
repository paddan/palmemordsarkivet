"""Tester för normalize_text: normalize-funktionen och stamp-fillogiken."""

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
# Stamp-fillogik via CLI (subprocess)
# ---------------------------------------------------------------------------

def _run_main(args: list[str], txt_dir: Path) -> str:
    """Kör normalize_text.main() och returnerar stdout."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "normalize_text", "--txt", str(txt_dir)] + args,
        capture_output=True, text=True,
    )
    return result.stdout + result.stderr


@pytest.fixture()
def txt_dir(tmp_path):
    d = tmp_path / "text"
    d.mkdir()
    return d


def test_stamp_created_after_run(txt_dir):
    (txt_dir / "a.txt").write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir)
    assert (txt_dir / ".normalize_stamp").exists()


def test_stamp_skips_old_files(txt_dir):
    f = txt_dir / "a.txt"
    f.write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir)  # skapar stamp
    stamp = txt_dir / ".normalize_stamp"
    first_mtime = stamp.stat().st_mtime

    # Kör igen utan att ändra filen — stamp ska INTE uppdateras (inga filer att processa)
    time.sleep(0.05)
    out = _run_main([], txt_dir)
    assert "oförändrade sedan senaste körning" in out
    # stamp-mtime ska vara oförändrad (inga filer processades → stamp touches inte)
    assert stamp.stat().st_mtime == first_mtime


def test_stamp_processes_new_file(txt_dir):
    (txt_dir / "a.txt").write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir)
    stamp = txt_dir / ".normalize_stamp"

    time.sleep(0.05)
    new_file = txt_dir / "b.txt"
    new_file.write_text("ﬁnns ny text", encoding="utf-8")

    out = _run_main([], txt_dir)
    assert "1 filer" in out or "1/" in out
    assert new_file.read_text(encoding="utf-8") == "finns ny text"
    # stamp uppdateras efter lyckad körning
    assert stamp.stat().st_mtime > 0


def test_rebuild_ignores_stamp(txt_dir):
    f = txt_dir / "a.txt"
    f.write_text("ren text", encoding="utf-8")
    _run_main([], txt_dir)  # skapar stamp

    time.sleep(0.05)
    out = _run_main(["--rebuild"], txt_dir)
    assert "oförändrade" not in out
    assert "1 filer" in out or "1/" in out


def test_dry_run_does_not_create_stamp(txt_dir):
    (txt_dir / "a.txt").write_text("ﬁnns text", encoding="utf-8")
    _run_main(["--dry-run"], txt_dir)
    assert not (txt_dir / ".normalize_stamp").exists()
