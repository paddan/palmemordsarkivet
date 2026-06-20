# Tidslinje Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygg en fristående tidslinje-flik som extraherar datum ur sparade svar och bokmärkta källor, så användaren kan sortera och exportera händelser.

**Architecture:** Lägg datumextraktion och eventmodell i `src/timeline.py`. Streamlit-sidan `src/pages/5_Tidslinje.py` läser befintliga `casebook_entries` och `source_bookmarks` via `db.py`, visar en sorterbar tabell och erbjuder CSV-download.

**Tech Stack:** Python 3.11+, regex/date parsing med standardbiblioteket, SQLite-state via `src/db.py`, Streamlit, pytest.

## Global Constraints

- Branch/worktree: `codex/tidslinje`, egen Codex worktree-tråd, thinking `medium`.
- Commit/push: gör inga commits och pusha inte; lämna ändringar i worktree:t och rapportera.
- Kommentarer och docstrings i produktionskod ska vara på svenska.
- Lägg inte till externa datumparser-beroenden.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` om sidan/beteendet ändras.
- Kör minst `.venv/bin/pytest tests/test_timeline.py -q`, `python3 -m compileall src/timeline.py src/pages/5_Tidslinje.py` och `git diff --check`.

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
        "branchName": "codex/tidslinje"
      }
    }
  },
  "thinking": "medium",
  "prompt": "Implementera planen docs/superpowers/plans/2026-06-19-tidslinje.md. Arbeta i din worktree-branch codex/tidslinje. Gör inga commits och pusha inte. Följ TDD-stegen, uppdatera dokumentation, kör angivna tester och rapportera ändrade filer samt testresultat."
}
```

## File Structure

- Create: `src/timeline.py` — ren logik för datumextraktion, eventbyggande och CSV-format.
- Create: `src/pages/5_Tidslinje.py` — Streamlit-sida som visar tidslinje från pärm/bokmärken.
- Create: `tests/test_timeline.py` — pytest för datumextraktion, eventdedup och CSV.
- Modify: docs listed above.

## Task 1: Datumextraktion

**Files:**
- Create: `src/timeline.py`
- Test: `tests/test_timeline.py`

**Interfaces:**
- Produces:
  - `TimelineEvent` dataclass with `date: date`, `label: str`, `source_type: str`, `source_id: str`, `snippet: str`
  - `extract_dates(text: str) -> list[date]`

- [ ] **Step 1: Write failing tests**

Create `tests/test_timeline.py`:

```python
from datetime import date

from timeline import extract_dates


def test_extract_dates_handles_iso_and_swedish_numeric_dates() -> None:
    text = "Mötet var 1986-02-28. Förhöret hölls 1986-03-01 och 28/2 1986 nämns."

    assert extract_dates(text) == [
        date(1986, 2, 28),
        date(1986, 3, 1),
    ]


def test_extract_dates_ignores_invalid_dates() -> None:
    assert extract_dates("1986-99-99 och 32/13 1986") == []
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_timeline.py -q`

Expected: fails because module does not exist.

- [ ] **Step 3: Implement extraction**

Support:

- ISO-like `YYYY-MM-DD`
- Swedish numeric `D/M YYYY` and `DD/MM YYYY`

Deduplicate and sort dates. Ignore invalid dates by catching `ValueError`.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_timeline.py -q`

Expected: all tests pass.

## Task 2: Eventbyggare

**Files:**
- Modify: `src/timeline.py`
- Modify: `tests/test_timeline.py`

**Interfaces:**
- Produces:
  - `events_from_casebook(entries: list[dict]) -> list[TimelineEvent]`
  - `events_from_bookmarks(bookmarks: list[dict]) -> list[TimelineEvent]`

- [ ] **Step 1: Write failing tests**

Add:

```python
from datetime import date

from timeline import events_from_bookmarks, events_from_casebook


def test_events_from_casebook_uses_question_note_and_answer_snippet() -> None:
    entries = [{
        "id": 7,
        "question": "Vad hände 1986-02-28?",
        "note": "Mordkvällen",
        "answer": "Svar med detaljer om 1986-02-28 och efterspel.",
    }]

    events = events_from_casebook(entries)

    assert len(events) == 1
    assert events[0].date == date(1986, 2, 28)
    assert events[0].label == "Mordkvällen"
    assert events[0].source_type == "casebook"
    assert events[0].source_id == "7"


def test_events_from_bookmarks_reads_source_and_title() -> None:
    bookmarks = [{
        "id": 3,
        "source": "100 — PM 1986-03-01.txt",
        "title": "PM 1986-03-01",
        "page": 2,
    }]

    events = events_from_bookmarks(bookmarks)

    assert events[0].date == date(1986, 3, 1)
    assert events[0].label == "PM 1986-03-01"
    assert events[0].source_type == "bookmark"
    assert events[0].source_id == "3"
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_timeline.py -q`

Expected: fails because event builders are missing.

- [ ] **Step 3: Implement builders**

For casebook entries, scan `question`, `note`, `answer`, and source titles. For bookmarks, scan `title` and `source`. Use a 160-character whitespace-collapsed snippet.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_timeline.py -q`

Expected: all tests pass.

## Task 3: CSV export and Streamlit page

**Files:**
- Modify: `src/timeline.py`
- Create: `src/pages/5_Tidslinje.py`
- Modify: `tests/test_timeline.py`

**Interfaces:**
- Produces: `events_to_csv(events: list[TimelineEvent]) -> str`

- [ ] **Step 1: Write CSV test**

Add:

```python
from datetime import date

from timeline import TimelineEvent, events_to_csv


def test_events_to_csv_contains_header_and_iso_date() -> None:
    csv_text = events_to_csv([
        TimelineEvent(date(1986, 2, 28), "Mordkvällen", "casebook", "1", "Kort text")
    ])

    assert csv_text.splitlines()[0] == "date,label,source_type,source_id,snippet"
    assert "1986-02-28,Mordkvällen,casebook,1,Kort text" in csv_text
```

- [ ] **Step 2: Implement CSV**

Use `csv.DictWriter` and `io.StringIO` from stdlib.

- [ ] **Step 3: Create page**

`src/pages/5_Tidslinje.py` should:

- open state db with `casebook_ui.state_conn()`
- read `db.list_casebook_entries(conn, limit=500)` and `db.list_source_bookmarks(conn, limit=500)`
- build events, sort by date
- show `st.dataframe` with date, label, source_type, snippet
- add `st.download_button("Ladda ner CSV", events_to_csv(events), file_name="tidslinje.csv", mime="text/csv")`
- show `st.info` when there are no dated events

- [ ] **Step 4: Verify**

Run:

```bash
.venv/bin/pytest tests/test_timeline.py -q
python3 -m compileall src/timeline.py src/pages/5_Tidslinje.py
```

Expected: pass and exit 0.

## Task 4: Docs and final verification

- [ ] **Step 1: Update docs**

Document **Tidslinje** as a helper built from saved casebook entries and bookmarks, not as a full NLP event extractor.

- [ ] **Step 2: Run final checks**

Run:

```bash
.venv/bin/pytest tests/test_timeline.py -q
python3 -m compileall src/timeline.py src/pages/5_Tidslinje.py
git diff --check
```

Expected: tests pass, compileall exits 0, diff-check prints nothing.

- [ ] **Step 3: Report**

Report changed files and test output. Do not commit or push.
