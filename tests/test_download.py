"""Tester för download.extract_drive_id och sniff_extension."""

from __future__ import annotations

from pathlib import Path

import requests

from download import (
    _is_retryable_failure_note,
    _permanent_failure_note,
    extract_drive_id,
    sniff_extension,
)


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


def test_html_runtime_error_is_retryable_not_permanent() -> None:
    err = RuntimeError("fick HTML i andra svaret också (rate limit eller borttagen fil)")
    assert _permanent_failure_note(err) is None


def test_non_transient_http_error_is_permanent() -> None:
    response = requests.Response()
    response.status_code = 404
    err = requests.HTTPError("HTTP 404", response=response)
    assert _permanent_failure_note(err) == "failed:404"


def test_transient_http_error_is_retryable_not_permanent() -> None:
    response = requests.Response()
    response.status_code = 503
    err = requests.HTTPError("HTTP 503", response=response)
    assert _permanent_failure_note(err) is None


def test_legacy_html_failure_note_is_retryable() -> None:
    assert _is_retryable_failure_note("failed:html-response")
    assert not _is_retryable_failure_note("failed:404")
    assert not _is_retryable_failure_note(None)


def test_forbidden_http_error_is_transient_not_permanent() -> None:
    """Regression: Drive svarar 403 både vid throttling och nekad åtkomst.

    Ett 403 får aldrig stämplas som permanent — då hoppas dokumentet över i
    alla framtida körningar och försvinner ur arkivet.
    """
    response = requests.Response()
    response.status_code = 403
    err = requests.HTTPError("HTTP 403", response=response)
    assert _permanent_failure_note(err) is None


def test_legacy_forbidden_failure_note_is_retryable() -> None:
    assert _is_retryable_failure_note("failed:403")


class _FakeSheet:
    """Minimal requests-svar för kalkylbladet."""

    encoding = "utf-8"

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


_SHEET_CSV = (
    "Nummer,Titel,Beställt,Upplagt/uppdaterat,\"Länk till kopia\"\n"
    "1,Förhör med vittnet,2021-01-01,2021-01-02,"
    "https://drive.google.com/file/d/AAA111/view\n"
    "2,PM om spaning,2021-02-01,2021-02-02,"
    "https://drive.google.com/file/d/BBB222/view\n"
)


def _patch_sheet(monkeypatch, calls: list[str]) -> None:
    """Låtsas nätverket: kalkylblad + filnedladdning."""
    import download

    monkeypatch.setattr(
        download.requests, "get", lambda *a, **k: _FakeSheet(_SHEET_CSV)
    )
    monkeypatch.setattr(download.requests, "Session", lambda: object())

    def fake_drive_download(file_id: str, dest, session) -> str:
        calls.append(file_id)
        dest.write_bytes(b"%PDF-1.7 " + file_id.encode())
        return file_id.lower() * 8

    monkeypatch.setattr(download, "drive_download", fake_drive_download)


def _download_env(tmp_path, monkeypatch):
    import download

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    calls: list[str] = []
    _patch_sheet(monkeypatch, calls)
    out = tmp_path / "files"
    return download, out, calls


def test_download_dry_run_lists_without_fetching_or_writing_state(tmp_path, monkeypatch) -> None:
    download, out, calls = _download_env(tmp_path, monkeypatch)

    assert download.run_download(out=out, sheet_id="x", limit=0, dry_run=True) == 0

    assert calls == []
    assert not out.exists()
    import db

    with db.connect(tmp_path / "state.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == 0


def test_download_rebuild_fetches_again_and_ignores_the_manifest(tmp_path, monkeypatch) -> None:
    download, out, calls = _download_env(tmp_path, monkeypatch)

    assert download.run_download(out=out, sheet_id="x", limit=0) == 0
    assert calls == ["AAA111", "BBB222"]

    # Andra körningen är idempotent: manifestet hoppar över allt.
    assert download.run_download(out=out, sheet_id="x", limit=0) == 0
    assert calls == ["AAA111", "BBB222"]

    # --rebuild hämtar om även det som ligger på disk och i manifestet.
    assert download.run_download(out=out, sheet_id="x", limit=0, rebuild=True) == 0
    assert calls == ["AAA111", "BBB222", "AAA111", "BBB222"]
