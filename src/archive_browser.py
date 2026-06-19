"""Ren logik för att bläddra bland arkivets textdokument."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DocumentRecord:
    """Metadata för ett sökbart arkivdokument."""

    source: str
    stem: str
    title: str
    nr: str | None
    text_path: Path
    pdf_path: Path | None
    source_kind: str


_NUMBERED_STEM_RE = re.compile(r"^\s*(?P<nr>\d+)\s+[—-]\s+(?P<title>.+?)\s*$")


def parse_document_source(path: Path, root: Path) -> DocumentRecord:
    """Tolka en textfil som dokumentkälla och koppla den till bästa PDF."""
    stem = path.stem
    match = _NUMBERED_STEM_RE.match(stem)
    nr = match.group("nr") if match else None
    title = match.group("title").strip() if match else stem
    pdf_path, source_kind = _find_pdf(root, stem)

    return DocumentRecord(
        source=path.name,
        stem=stem,
        title=title,
        nr=nr,
        text_path=path,
        pdf_path=pdf_path,
        source_kind=source_kind,
    )


def iter_documents(root: Path) -> list[DocumentRecord]:
    """Lista alla OCR-textdokument sorterade för mänsklig bläddring."""
    text_root = root / "generated" / "text"
    if not text_root.is_dir():
        return []
    records = [
        parse_document_source(path, root)
        for path in text_root.glob("*.txt")
        if path.is_file()
    ]
    return sorted(records, key=_sort_key)


def filter_documents(documents: list[DocumentRecord], query: str) -> list[DocumentRecord]:
    """Filtrera dokument på nummer, titel eller filnamn."""
    needle = query.casefold().strip()
    if not needle:
        return documents
    return [
        record
        for record in documents
        if needle in _search_text(record)
    ]


def read_preview(path: Path, max_chars: int = 900) -> str:
    """Läs en kort, whitespace-normaliserad textförhandsvisning."""
    text = path.read_text(encoding="utf-8", errors="replace")
    preview = re.sub(r"\s+", " ", text).strip()
    if len(preview) <= max_chars:
        return preview

    cut = preview[:max_chars].rsplit(" ", 1)[0].rstrip(".,;: ")
    if not cut:
        cut = preview[:max_chars].rstrip(".,;: ")
    return f"{cut}..."


def _find_pdf(root: Path, stem: str) -> tuple[Path | None, str]:
    for relative_dir in ("generated/ocr", "downloaded/files", "downloaded/wpu_files"):
        path = root / relative_dir / f"{stem}.pdf"
        if path.is_file():
            source_kind = "wpu" if relative_dir == "downloaded/wpu_files" else "palme"
            return path, source_kind
    return None, "palme"


def _sort_key(record: DocumentRecord) -> tuple[int, int, str]:
    if record.nr is not None:
        return (0, int(record.nr), record.title.casefold())
    return (1, 0, record.title.casefold())


def _search_text(record: DocumentRecord) -> str:
    fields = (record.nr or "", record.title, record.source)
    return " ".join(fields).casefold()
