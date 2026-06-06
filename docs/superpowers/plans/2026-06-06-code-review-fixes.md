# Code Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Åtgärda samtliga verifierade korrekthets- och dokumentationsproblem från projektgranskningen.

**Architecture:** Behåll nuvarande pipeline och state-modell, men stärk gränserna mellan SQLite, LanceDB och filsystemet. Varje beteendefix låses med ett regressionstest innan produktionskoden ändras.

**Tech Stack:** Python 3.11, pytest, SQLite, LanceDB, Bash, PyMuPDF.

---

### Task 1: Säker LanceDB-synk

**Files:**
- Modify: `src/rag/ingest.py`
- Modify: `tests/test_reingest.py`

- [ ] Lägg till tester för befintlig LanceDB-source utan ingest-state och för re-indexering till noll chunks.
- [ ] Kör `pytest tests/test_reingest.py -q` och verifiera att de nya testerna fallerar.
- [ ] Implementera source-inventering och cleanup av oanvändbara re-indexeringar.
- [ ] Kör `pytest tests/test_reingest.py -q` och verifiera grönt.

### Task 2: Återupptagning och blacklist

**Files:**
- Modify: `run_pipeline.sh`
- Modify: `ocr_tesseract.sh`
- Modify: `src/db.py`
- Modify: `src/ocr_db_helper.py`
- Modify: `tests/test_db.py`

- [ ] Lägg till test som verifierar att blacklist-retry även nollställer failed.
- [ ] Kör testet och verifiera att det fallerar.
- [ ] Implementera en atomisk DB-operation och använd den från wrappern.
- [ ] Ta bort tidig exit från `run_pipeline.sh`.
- [ ] Kör riktade tester och `bash -n`.

### Task 3: Atomisk PDF-patchning

**Files:**
- Modify: `src/ocr_pages.py`
- Modify: `tests/test_detect_redactions.py`

- [ ] Lägg till test där `insert_textbox` aldrig lyckas och originalfilen måste bevaras.
- [ ] Kör testet och verifiera att det fallerar.
- [ ] Kasta fel vid misslyckad radinmatning innan temporärfilen ersätter originalet.
- [ ] Kör riktat test och verifiera grönt.

### Task 4: Stabil WPU-cleanup

**Files:**
- Modify: `src/merge_wpu.py`
- Modify: `tests/test_merge_wpu.py`

- [ ] Lägg till test för avsiktligt borttagen WPU-förlorare.
- [ ] Kör testet och verifiera att det fallerar.
- [ ] Begränsa phantom-cleanup till rader utan avslutad Tesseract-status.
- [ ] Kör riktade tester och verifiera grönt.

### Task 5: Dokumentation och full verifiering

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] Synka dokumentation för pipeline-resume, ingest-återställning och faktiska flaggor/dataflöden.
- [ ] Kör `pytest tests/ -q`, `bash -n *.sh`, `shellcheck *.sh`, `git diff --check` och `python -m compileall`.
- [ ] Granska slutdiffen efter regressionsrisker.
