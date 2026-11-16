# Skanna om hela dokumentet — implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Skanna om alla sidor i ett dokument med vald motor från Sidgranskning, CLI och Admin, och behåll den bättre läsningen per sida.

**Architecture:** `rescan-pages` får parametern `pdf_stem`; med den väljer en ny urvalsfunktion sidorna 1…N i dokumentet i stället för poängurvalet, och resten av flödet (arkivering, OCR per dokument, jämförelse, kvalitet, omindexering) är oförändrat. Sidgranskning startar `rescan-pages` med `pdf_stem` från en ny knapp.

**Tech Stack:** Python 3.11+, Streamlit (AppTest i tester), SQLite via `src/db/`, pytest.

**Spec:** `docs/superpowers/specs/2026-11-15-skanna-om-dokument-design.md`

## Global Constraints

- Nya parametrar läggs i `src/operations/registry.py`; CLI och Admin genereras därifrån (AGENTS.md).
- Varje fält som visas i Admin har en hjälpunkt i `OPERATION_HELP`, i formulärets ordning.
- Ingen SQL utanför `src/db/`.
- Debugloggning med `ctx.log(..., level="debug")` i jobbkod och `web_log.get_logger` i Sidgranskning.
- Jämförelseregeln är oförändrad: bara strikt högre `comparison_score` vinner.

## Review Focus

- Ett dokument som `merge_wpu` raderat som dubblett ska vägras med `OperationFailed` före skrivning (test i Task 1).
- Ett okänt `pdf_stem` eller saknad PDF ska ge `OperationFailed`, inte "inga sidor" (test i Task 1).
- Sidor som redan lästs med samma motor ska ändå väljas när `pdf_stem` anges (test i Task 1).
- `--max-pages` ska fortfarande begränsa antalet sidor i dokumentläget (test i Task 1).
- Med `llm-vision` får knappen inte gå att trycka förrän bekräftelsen med sidantalet är ikryssad (test i Task 2).

---

### Task 1: `rescan-pages --pdf-stem`

**Files:**
- Modify: `src/operations/rescan_pages.py` (ny `select_document_pages`, ny parameter i `rescan_pages`/`run_rescan_pages`)
- Modify: `src/operations/registry.py` (parametern i `rescan-pages`)
- Modify: `src/operations/adapters.py` (`rescan_pages_adapter` skickar `pdf_stem`)
- Modify: `src/admin_ui.py` (`FORM_LAYOUT["rescan-pages"]["labels"]["pdf_stem"] = "Dokument"`, hjälpunkt, `FILE_PICKERS[("rescan-pages", "pdf_stem")]` som för `rescan-page`)
- Test: `tests/test_rescan_pages.py`

**Interfaces:**
- Produces: `select_document_pages(root: Path, pdf_stem: str, *, max_pages: int, ctx: OperationContext) -> dict[str, list[int]]` — `{pdf_stem: [1, …, N]}`, N = `page_review.page_count(find_pdf(...))`, kapat av `max_pages` (0 = alla). Höjer `OperationFailed` för raderad dubblett eller saknad PDF.
- Produces: `rescan_pages(..., pdf_stem: str | None = None)` och `run_rescan_pages(..., pdf_stem: str | None = None)`; registerparametern `_p("pdf_stem", "--pdf-stem", "str", None, "Bara detta dokument: alla sidor, oavsett tröskel")`.

- [ ] **Step 1: Skriv de fallerande testerna**
  - `test_document_mode_selects_every_page_regardless_of_score_and_engine`: dokument med 3 sidor, sida 1 poäng 90 och redan läst med `surya-layout`; `_scan(root, fake, pdf_stem="doc", threshold=10)` skannar sidorna `[1, 2, 3]` (kontrollera `fake`:s anrop).
  - `test_document_mode_respects_max_pages`: `max_pages=2` → sidorna `[1, 2]`.
  - `test_document_mode_rejects_removed_duplicate`: `tesseract_done_at` satt men ingen textfil → `pytest.raises(OperationFailed)` och inga historikrader.
  - `test_document_mode_rejects_missing_pdf`: okänd stam → `OperationFailed`.
  - `test_document_mode_dry_run_writes_nothing`: `dry_run=True` loggar 3 sidor, inga historikrader.
- [ ] **Step 2: Kör** `.venv/bin/pytest -q tests/test_rescan_pages.py -k document_mode` — väntat: FAIL (okänt argument `pdf_stem`).
- [ ] **Step 3: Implementera** `select_document_pages` och anropa den i `rescan_pages` i stället för `select_pages` när `pdf_stem` är satt; logga på debugnivå att tröskel/källa/"redan läst" inte gäller. Registerparameter, adapter, etikett, hjälpunkt (efter formulärets ordning) och filväljare.
- [ ] **Step 4: Kör** `.venv/bin/pytest -q tests/test_rescan_pages.py tests/test_admin_ui.py tests/test_operation_registry.py tests/test_scripts.py` — väntat: PASS.

### Task 2: Knappen i Sidgranskning

**Files:**
- Modify: `src/pages/9_Sidgranskning.py`
- Test: `tests/test_sidgranskning_ui.py`

**Interfaces:**
- Consumes: operationen `rescan-pages` med parametrarna `root, pdf_stem, engine, profile, reindex, dpi` (övriga från registrets standardvärden via `start_job`).

- [ ] **Step 1: Skriv de fallerande testerna**
  - `test_rescan_document_button_starts_rescan_pages`: klick på `sg_rescan_doc` → `start_job("rescan-pages", {... "pdf_stem": stem, "engine": vald motor, "reindex": ...})`.
  - `test_rescan_document_with_llm_vision_requires_confirmation`: med `llm-vision` och profil är knappen spärrad tills `sg_rescan_doc_ok` är ikryssad; bekräftelsens etikett nämner sidantalet.
  - `test_running_document_job_shows_status_on_every_page`: aktivt `rescan-pages`-jobb med samma `pdf_stem` visar jobbstatusen (som för en enskild sida) på valfri sida i dokumentet.
- [ ] **Step 2: Kör** `.venv/bin/pytest -q tests/test_sidgranskning_ui.py -k document` — väntat: FAIL.
- [ ] **Step 3: Implementera:** `_rescan_target` returnerar `(stem, None)` för `rescan-pages` med `pdf_stem`; `this_page_running` blir sant för hela dokumentet då. `_start` tar operationen som argument. Knappen *Skanna om hela dokumentet* i samma kort; med `llm-vision` en kryssruta "Skicka alla N sidor till profilens endpoint". Klarmeddelandet säger "dokumentet" i stället för "sidan N" för dokumentjobb. Sidbildernas cache behöver inte rensas (PDF:en ändras inte).
- [ ] **Step 4: Kör** `.venv/bin/pytest -q tests/test_sidgranskning_ui.py` — väntat: PASS.

### Task 3: Dokumentation och hela sviten

**Files:**
- Modify: `AGENTS.md` (stycket om massomskanning och Sidgranskning), `docs/teknisk-referens.md` (flaggtabellen för `rescan-pages` och Sidgranskning)

- [ ] **Step 1:** Beskriv `--pdf-stem`, att tröskel/källa/"redan läst" inte gäller i dokumentläget och knappen i Sidgranskning.
- [ ] **Step 2: Kör** `.venv/bin/python scripts/test.py --static` — väntat: alla tester gröna, ruff och mypy utan fel.
