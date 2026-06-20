# Källor-Bläddrare Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygg en fristående Streamlit-flik för att bläddra, söka och öppna arkivdokument utan att först ställa en AI-fråga.

**Architecture:** Lägg all testbar logik i en ny ren modul `src/archive_browser.py`. Streamlit-sidan `src/pages/1_Källor.py` använder modulen för listning/filter och återanvänder `casebook_ui` för PDF/text-sökvägar och bokmärken.

**Tech Stack:** Python 3.11+, Streamlit, SQLite-state via `src/db.py`, lokala filer i `generated/text`, `generated/ocr`, `downloaded/files`, `downloaded/wpu_files`, pytest.

## Global Constraints

- Branch/worktree: `codex/kallor-bladdrare`, egen Codex worktree-tråd, thinking `medium`.
- Commit/push: gör inga commits och pusha inte; lämna ändringar i worktree:t och rapportera.
- Kommentarer och docstrings i produktionskod ska vara på svenska.
- Håll ändringen avgränsad till `src/archive_browser.py`, `src/pages/1_Källor.py`, tester och användarvändad dokumentation.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` om sidan/beteendet ändras.
- Kör minst `.venv/bin/pytest tests/test_archive_browser.py -q`, `python3 -m compileall src/archive_browser.py src/pages/1_Källor.py` och `git diff --check`.

---

## Worktree Thread Start

Coordinator creates the worker thread with:

```json
{
  "target": {
    "type": "project",
    "projectId": "/Users/patrik/projects/palmemordsarkivet",
    "environment": {
      "type": "worktree",
      "startingState": {
        "type": "branch",
        "branchName": "codex/kallor-bladdrare"
      }
    }
  },
  "thinking": "medium",
  "prompt": "Implementera planen docs/superpowers/plans/2026-06-19-kallor-bladdrare.md. Arbeta i din worktree-branch codex/kallor-bladdrare. Gör inga commits och pusha inte. Följ TDD-stegen, uppdatera dokumentation, kör angivna tester och rapportera ändrade filer samt testresultat."
}
```

## File Structure

- Create: `src/archive_browser.py` — ren logik för dokumentlistning, metadata, filter och preview.
- Create: `src/pages/1_Källor.py` — Streamlit-sida: sökfält, filter, dokumentlista, preview, PDF/text-knappar, bokmärkning.
- Create: `tests/test_archive_browser.py` — pytest för metadata, filter, preview och sortering.
- Modify: `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`, `CLAUDE.md` — dokumentera ny flik.

## Task 1: Dokumentindex i ren modul

**Files:**
- Create: `src/archive_browser.py`
- Test: `tests/test_archive_browser.py`

**Interfaces:**
- Produces:
  - `DocumentRecord` dataclass with fields `source: str`, `stem: str`, `title: str`, `nr: str | None`, `text_path: Path`, `pdf_path: Path | None`, `source_kind: str`.
  - `parse_document_source(path: Path, root: Path) -> DocumentRecord`
  - `iter_documents(root: Path) -> list[DocumentRecord]`

- [ ] **Step 1: Write failing tests**

Add `tests/test_archive_browser.py`:

```python
from pathlib import Path

from archive_browser import iter_documents, parse_document_source


def test_parse_document_source_extracts_nr_title_and_pdf(tmp_path: Path) -> None:
    root = tmp_path
    text_dir = root / "generated" / "text"
    pdf_dir = root / "generated" / "ocr"
    text_dir.mkdir(parents=True)
    pdf_dir.mkdir(parents=True)
    text = text_dir / "100 — Skandiaförhör.txt"
    pdf = pdf_dir / "100 — Skandiaförhör.pdf"
    text.write_text("sida 1", encoding="utf-8")
    pdf.write_bytes(b"%PDF-test")

    record = parse_document_source(text, root)

    assert record.source == "100 — Skandiaförhör.txt"
    assert record.stem == "100 — Skandiaförhör"
    assert record.nr == "100"
    assert record.title == "Skandiaförhör"
    assert record.pdf_path == pdf
    assert record.source_kind == "palme"


def test_iter_documents_sorts_by_nr_then_title(tmp_path: Path) -> None:
    text_dir = tmp_path / "generated" / "text"
    text_dir.mkdir(parents=True)
    (text_dir / "200 — B.txt").write_text("b", encoding="utf-8")
    (text_dir / "100 — A.txt").write_text("a", encoding="utf-8")

    assert [d.source for d in iter_documents(tmp_path)] == [
        "100 — A.txt",
        "200 — B.txt",
    ]
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_archive_browser.py -q`

Expected: fails because `archive_browser` does not exist.

- [ ] **Step 3: Implement minimal module**

Create `src/archive_browser.py` with the dataclass and functions. Use `root / "generated" / "text"` as the source of indexed documents. Resolve PDFs by trying `generated/ocr`, `downloaded/files`, `downloaded/wpu_files`, in that order. `source_kind` is `"wpu"` when the matching PDF is under `downloaded/wpu_files`, otherwise `"palme"`.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_archive_browser.py -q`

Expected: all tests pass.

## Task 2: Filter och preview

**Files:**
- Modify: `src/archive_browser.py`
- Modify: `tests/test_archive_browser.py`

**Interfaces:**
- Produces:
  - `filter_documents(documents: list[DocumentRecord], query: str) -> list[DocumentRecord]`
  - `read_preview(path: Path, max_chars: int = 900) -> str`

- [ ] **Step 1: Write failing tests**

Add tests:

```python
from archive_browser import DocumentRecord, filter_documents, read_preview


def test_filter_documents_matches_nr_title_and_source(tmp_path: Path) -> None:
    docs = [
        DocumentRecord("100 — Skandia.txt", "100 — Skandia", "Skandia", "100", tmp_path / "a.txt", None, "palme"),
        DocumentRecord("200 — Grand.txt", "200 — Grand", "Grand", "200", tmp_path / "b.txt", None, "palme"),
    ]

    assert [d.nr for d in filter_documents(docs, "skandia")] == ["100"]
    assert [d.nr for d in filter_documents(docs, "200")] == ["200"]
    assert filter_documents(docs, "") == docs


def test_read_preview_collapses_whitespace_and_truncates(tmp_path: Path) -> None:
    path = tmp_path / "x.txt"
    path.write_text("Rad 1\n\nRad 2 " + ("ord " * 300), encoding="utf-8")

    preview = read_preview(path, max_chars=40)

    assert preview.startswith("Rad 1 Rad 2")
    assert preview.endswith("...")
    assert len(preview) <= 40
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_archive_browser.py -q`

Expected: fails because helpers are missing.

- [ ] **Step 3: Implement helpers**

Implement case-insensitive substring matching over `nr`, `title`, `source`. Collapse whitespace in previews and truncate on word boundary when possible.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_archive_browser.py -q`

Expected: all tests pass.

## Task 3: Streamlit-sidan

**Files:**
- Create: `src/pages/1_Källor.py`
- Modify: `pyproject.toml` only if a new top-level module must be packaged.

**Interfaces:**
- Consumes: `iter_documents`, `filter_documents`, `read_preview`, `casebook_ui.render_source_cards`.

- [ ] **Step 1: Create page skeleton**

Build `src/pages/1_Källor.py`:

```python
"""Streamlit-sida: bläddra bland källmaterialet."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import archive_browser as _archive_browser  # noqa: E402
import casebook_ui as _casebook_ui  # noqa: E402

st.set_page_config(page_title="Palmemordsarkivet — Källor", layout="wide")
st.title("Källor")
st.caption("Bläddra, sök och bokmärk dokument utan att fråga AI först.")
```

- [ ] **Step 2: Add cached data and controls**

Use `@st.cache_data(ttl=30, show_spinner=False)` for `iter_documents(ROOT)`. Add:

- `st.text_input("Sök dokument", placeholder="Nr, titel eller filnamn")`
- `st.slider("Antal träffar", 10, 200, 50, step=10)`
- result caption with total and filtered count

- [ ] **Step 3: Render document cards**

For each filtered document up to limit:

- Show `record.title` as heading and `record.source` as caption.
- Show `read_preview(record.text_path)`.
- Call `_casebook_ui.render_source_cards(ROOT, [{"source": record.source, "page": None, "nr": record.nr, "title": record.title}], conn, key_prefix=f"kallor_{i}")`.

- [ ] **Step 4: Manual smoke**

Run: `./web.sh -- --server.headless true --server.port 8502`

Open `/Källor` and verify:

- page renders without traceback
- search narrows visible documents
- PDF/text/bookmark buttons are visible for documents with files

Stop the server after verification.

## Task 4: Docs and final verification

**Files:**
- Modify: `README.md`
- Modify: `docs/kom-igang.md`
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update docs**

Mention the **Källor** tab in the web UI overview and file overview. Keep wording concise: it is for browsing, previewing and bookmarking source documents.

- [ ] **Step 2: Run verification**

Run:

```bash
.venv/bin/pytest tests/test_archive_browser.py -q
python3 -m compileall src/archive_browser.py src/pages/1_Källor.py
git diff --check
```

Expected: tests pass, compileall exits 0, diff-check prints nothing.

- [ ] **Step 3: Report**

Report changed files and test output. Do not commit or push.
