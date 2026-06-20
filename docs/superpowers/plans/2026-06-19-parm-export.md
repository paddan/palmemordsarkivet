# Pärm-Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lägg till export av Utredningspärmen till Markdown och JSON så sparade svar, källor och bokmärken kan användas utanför appen.

**Architecture:** Skapa `src/casebook_export.py` som ren formatteringsmodul. Integrera exportknappar i den befintliga Utredningspärm-sidan via `src/casebook_ui.py` utan schemaändring.

**Tech Stack:** Python 3.11+, Streamlit `download_button`, JSON/Markdown via standardbiblioteket, pytest.

## Global Constraints

- Branch/worktree: `codex/parm-export`, egen Codex worktree-tråd, thinking `medium`.
- Commit/push: gör inga commits och pusha inte; lämna ändringar i worktree:t och rapportera.
- Kommentarer och docstrings i produktionskod ska vara på svenska.
- Ingen ny databas-migrering och inga nya externa beroenden.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` om exportfunktionen dokumenteras där.
- Kör minst `.venv/bin/pytest tests/test_casebook_export.py tests/test_casebook_ui.py -q`, `python3 -m compileall src/casebook_export.py src/casebook_ui.py` och `git diff --check`.

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
        "branchName": "codex/parm-export"
      }
    }
  },
  "thinking": "medium",
  "prompt": "Implementera planen docs/superpowers/plans/2026-06-19-parm-export.md. Arbeta i din worktree-branch codex/parm-export. Gör inga commits och pusha inte. Följ TDD-stegen, uppdatera dokumentation, kör angivna tester och rapportera ändrade filer samt testresultat."
}
```

## File Structure

- Create: `src/casebook_export.py` — Markdown/JSON-export för sparade svar och bokmärken.
- Create: `tests/test_casebook_export.py` — pytest för exportformat.
- Modify: `src/casebook_ui.py` — lägg exportknappar på Utredningspärm-sidan.
- Modify: docs listed above.

## Task 1: Markdown-export

**Files:**
- Create: `src/casebook_export.py`
- Test: `tests/test_casebook_export.py`

**Interfaces:**
- Produces:
  - `casebook_to_markdown(entries: list[dict], bookmarks: list[dict]) -> str`

- [ ] **Step 1: Write failing tests**

Create `tests/test_casebook_export.py`:

```python
from casebook_export import casebook_to_markdown


def test_casebook_to_markdown_contains_entries_sources_and_bookmarks() -> None:
    md = casebook_to_markdown(
        entries=[{
            "question": "Vem nämner Skandia?",
            "answer": "Skandia nämns i svaret.",
            "mode": "rag",
            "backend": "Claude",
            "model": "claude-sonnet-4-6",
            "note": "Skandiaspåret",
            "sources": [{"source": "100 — Skandia.txt", "page": 28, "title": "Skandiaförhör"}],
            "entities": [{"namn": "Skandia", "label": "Organisation"}],
            "created_at": "2026-06-19T10:00:00+00:00",
        }],
        bookmarks=[{
            "source": "865 — Brev.txt",
            "page": 1,
            "title": "Brev",
            "note": "Läs igen",
        }],
    )

    assert md.startswith("# Utredningspärm")
    assert "## Skandiaspåret" in md
    assert "**Fråga:** Vem nämner Skandia?" in md
    assert "- Skandiaförhör, sida 28 (`100 — Skandia.txt`)" in md
    assert "- Skandia (Organisation)" in md
    assert "## Bokmärkta källor" in md
    assert "- Brev, sida 1 (`865 — Brev.txt`) — Läs igen" in md
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_casebook_export.py -q`

Expected: fails because module does not exist.

- [ ] **Step 3: Implement markdown formatter**

Create deterministic Markdown. Use note as entry heading when present, otherwise question. Escape no Markdown beyond replacing newlines in titles with spaces; this is a trusted local export.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_casebook_export.py -q`

Expected: all tests pass.

## Task 2: JSON-export

**Files:**
- Modify: `src/casebook_export.py`
- Modify: `tests/test_casebook_export.py`

**Interfaces:**
- Produces:
  - `casebook_to_json(entries: list[dict], bookmarks: list[dict]) -> str`

- [ ] **Step 1: Write failing test**

Add:

```python
import json

from casebook_export import casebook_to_json


def test_casebook_to_json_is_pretty_utf8_json() -> None:
    raw = casebook_to_json(
        entries=[{"question": "Fråga", "answer": "Svar", "sources": [], "entities": []}],
        bookmarks=[],
    )

    data = json.loads(raw)
    assert data["entries"][0]["question"] == "Fråga"
    assert data["bookmarks"] == []
    assert "\n  " in raw
```

- [ ] **Step 2: Implement JSON formatter**

Use `json.dumps({"entries": entries, "bookmarks": bookmarks}, ensure_ascii=False, indent=2, default=str)`.

- [ ] **Step 3: Verify**

Run: `.venv/bin/pytest tests/test_casebook_export.py -q`

Expected: all tests pass.

## Task 3: Exportknappar i Utredningspärmen

**Files:**
- Modify: `src/casebook_ui.py`

**Interfaces:**
- Consumes: `casebook_to_markdown`, `casebook_to_json`.

- [ ] **Step 1: Integrate imports**

Add `import casebook_export as _casebook_export` near other local imports in `src/casebook_ui.py`.

- [ ] **Step 2: Add export section**

In `render_casebook_page`, after the count caption and before tabs, add a compact export row:

```python
if entries or bookmarks:
    export_cols = st.columns([1, 1, 4])
    with export_cols[0]:
        st.download_button(
            "Ladda ner Markdown",
            data=_casebook_export.casebook_to_markdown(entries, bookmarks),
            file_name="utredningsparm.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with export_cols[1]:
        st.download_button(
            "Ladda ner JSON",
            data=_casebook_export.casebook_to_json(entries, bookmarks),
            file_name="utredningsparm.json",
            mime="application/json",
            use_container_width=True,
        )
```

- [ ] **Step 3: Verify compile**

Run: `python3 -m compileall src/casebook_export.py src/casebook_ui.py`

Expected: exit 0.

## Task 4: Docs and final verification

- [ ] **Step 1: Update docs**

Document that Utredningspärmen can export Markdown/JSON, useful for sharing notes or continuing work outside Streamlit.

- [ ] **Step 2: Run final checks**

Run:

```bash
.venv/bin/pytest tests/test_casebook_export.py tests/test_casebook_ui.py -q
python3 -m compileall src/casebook_export.py src/casebook_ui.py
git diff --check
```

Expected: tests pass, compileall exits 0, diff-check prints nothing.

- [ ] **Step 3: Report**

Report changed files and test output. Do not commit or push.
