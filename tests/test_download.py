"""Tester för download.extract_drive_id och sniff_extension."""

from __future__ import annotations

from pathlib import Path

from download import extract_drive_id, sniff_extension


def test_extract_drive_id_from_d_url() -> None:
    url = "https://drive.google.com/file/d/1ABCxyz_-defGHI/view"
    assert extract_drive_id(url) == "1ABCxyz_-defGHI"


def test_extract_drive_id_from_id_query() -> None:
    url = "https://drive.google.com/open?id=ZZZ123-_xyz"
    assert extract_drive_id(url) == "ZZZ123-_xyz"


def test_extract_drive_id_none() -> None:
    assert extract_drive_id("") is None
    assert extract_drive_id("https://example.com") is None
    assert extract_drive_id(None) is None  # type: ignore[arg-type]


def test_sniff_extension_pdf(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.write_bytes(b"%PDF-1.7\n...")
    assert sniff_extension(p) == ".pdf"


def test_sniff_extension_jpg(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")
    assert sniff_extension(p) in {".jpg", ".jpeg"}


def test_sniff_extension_unknown(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.write_bytes(b"random bytes here")
    # filetype kan returnera "" för okänt
    ext = sniff_extension(p)
    assert ext == "" or ext.startswith(".")
