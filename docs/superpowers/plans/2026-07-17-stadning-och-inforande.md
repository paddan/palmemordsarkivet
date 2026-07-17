# Stadning och Inforande Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ta bort brus och missvisande legacy-referenser samt dela upp de foreslagna forbattrringarna i sakra, testbara batchar.

**Architecture:** Stadning ar textuell och ska inte andra pipeline-beteende. Forbattrringarna inforas senare som fristaende batchar med egna tester och dokumentationsuppdateringar.

**Tech Stack:** Bash wrappers, Python 3.11+, pytest, Streamlit, SQLite, LanceDB, Claude/OpenAI tool-calling.

## Global Constraints

- Commita eller pusha inte; anvandaren kor `/cap` nar hen ar redo.
- Kommentarer och docstrings i projektkod ar pa svenska.
- Uppdatera anvandarvand dokumentation och AGENTS.md om en andring paverkar dem.
- Radera inte `generated/`, `downloaded/`, `.venv`, `graphify-out/`, Neo4j-data eller tessdata i stadningssteg.

---

### Task 1: Lokal brusrensning

**Files:**
- Remove ignored local artifacts only.

**Interfaces:**
- Consumes: `.gitignore`.
- Produces: renare arbetskatalog utan cachefiler.

- [x] **Step 1: Rensa cache och OS-brus**

Run:

```bash
find . -name '__pycache__' -type d -prune -exec rm -rf {} +
find . -name '*.pyc' -type f -delete
find . -name '*.pyo' -type f -delete
find . -name '.DS_Store' -type f -delete
rm -rf .pytest_cache src/palmemordsarkivet.egg-info
```

- [x] **Step 2: Kontrollera att inget viktigt ingick**

Run:

```bash
find . -maxdepth 3 \( -name '__pycache__' -o -name '.pytest_cache' -o -name '*.pyc' -o -name '*.pyo' -o -name '*.egg-info' -o -name '.DS_Store' \) -print
```

Expected: no output.

### Task 2: Aktiv text- och kommentarstadning

**Files:**
- Modify: `ocr_pages.sh`
- Modify: `src/download_wpu.py`
- Modify: `src/merge_wpu.py`
- Modify: `ocr.sh`

**Interfaces:**
- Consumes: current state-db architecture.
- Produces: aktiva hjalptexter och docstrings som beskriver `generated/db/state.db` och `downloaded/wpu_files`.

- [x] **Step 1: Uppdatera `ocr_pages.sh`**

Byt beskrivningen fran `page-NNN.txt/json` till att sidtext och metadata lagras
i `state.db` och mergas via `merge_pages`.

- [x] **Step 2: Uppdatera wpu-texter**

Byt `files_wpu` i anvandarvand text till `downloaded/wpu_files`.

### Task 3: Historiska superpowers-dokument

**Files:**
- Modify: `docs/superpowers/specs/*.md`
- Modify: `docs/superpowers/plans/*.md`

**Interfaces:**
- Consumes: historiska plan-/specfiler.
- Produces: battre sokbarhet och tydlig markering dar planer ar historiska.

- [x] **Step 1: Mekanisk sokvagsstadning**

Run:

```bash
perl -pi -e 's#src/webui\.py#src/Utredning.py#g; s#\bwebui\.py\b#Utredning.py#g; s#generated/state\.db#generated/db/state.db#g; s#files_wpu#downloaded/wpu_files#g' docs/superpowers/specs/*.md docs/superpowers/plans/*.md
```

- [x] **Step 2: Markera SQLite-planen som historisk**

Lagg notis i `2026-05-17-sqlite-state-design.md` och
`2026-05-17-sqlite-state.md` om att migreringsskripten senare togs bort och
att `src/db.py` ar aktuell atkomstyta.

### Task 4: Forbattringsbatch 1 - saker HTML-rendering

**Files:**
- Modify: `src/citations.py`
- Modify: `src/Utredning.py`
- Modify: `src/pages/6_Jämförelse.py`
- Modify: `src/casebook_ui.py`
- Test: `tests/test_citations.py`

**Interfaces:**
- Produces: `linkify_citations(text, mapping, known_sources=None)` som HTML-escapar all okand text och bara slapper igenom egna ankare.

- [ ] **Step 1: Skriv regressionstest**

Lagg test som verifierar att `linkify_citations('<img src=x onerror=alert(1)> [Nr 281, sida 4]', mapping)` inte innehaller raw `<img`.

- [ ] **Step 2: Implementera escaping**

Escape:a textsegment mellan citatmatchningar med `html.escape`, men behall
`pdf_anchor`-HTML for egna lankar.

- [ ] **Step 3: Verifiera**

Run:

```bash
.venv/bin/pytest tests/test_citations.py tests/test_casebook_ui.py tests/test_compare.py
```

### Task 5: Forbattringsbatch 2 - MCP-parametergransning

**Files:**
- Modify: `src/rag/mcp_server.py`
- Modify: `src/Utredning.py`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: gemensam clamp/validation for `top_k` 5-50 och `top_n` 1-15.

- [ ] **Step 1: Testa out-of-range**

Lagg test for att `search_archive(..., top_k=9999, top_n=9999)` aldrig skickar
vidare mer an tillaten grans.

- [ ] **Step 2: Implementera validation**

Infor liten helper i `mcp_server.py` och ateranvand den i OpenAI-tool-loopen i
`Utredning.py`.

- [ ] **Step 3: Verifiera**

Run:

```bash
.venv/bin/pytest tests/test_mcp_server.py tests/test_ask.py
```

### Task 6: Forbattringsbatch 3 - dev-verifiering

**Files:**
- Modify: `install.sh`
- Modify: `docs/teknisk-referens.md`
- Create: `test.sh`
- Optional create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: ett reproducerbart kommando for pytest/ruff/mypy nar dev-extra ar installerad.

- [ ] **Step 1: Lagga till testscript**

`test.sh` ska kora pytest och, nar `ruff`/`mypy` finns, aven statisk kontroll.

- [ ] **Step 2: Dokumentera dev-install**

Uppdatera testavsnittet till `pip install -e '.[dev,web]'`.

- [ ] **Step 3: Verifiera**

Run:

```bash
./test.sh
```

### Task 7: Senare arkitekturbatchar

**Files:** separata planer/specar per batch.

- [ ] SQLite-migrationer: skapa versionsstyrd migreringsrunner och gamla-db-fixtures.
- [ ] `src/Utredning.py`-split: extrahera rendering, RAG-runner och tool-loop utan beteendeforandring.
- [ ] Paket/importstadning: minska `sys.path.insert`, behall wrappers.
- [ ] RAG-eval: lagg 10-20 golden-fragor med forvantade kallor.
- [ ] LLM batch tracking: lagg `run_id`, status och senaste fel i state.db.
