# Skanna om sidor — implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** En operation `rescan-pages` som skannar om alla sidor under en poängtröskel med vald motor (inkl. den nya bildmotorn `llm-vision`) och behåller den bättre läsningen.

**Architecture:** Byggstenarna för arkivering/återställning flyttas ur `rescan.py` till `operations/page_readings.py` och delas av båda omskanningsoperationerna. Urval och körning bor i `operations/rescan_pages.py`; OCR:en körs som underprocess `ocr_pages.py --pages … --force` precis som `rescan-page`. Bildmotorn är en ren modul `src/llm_vision.py` som `ocr_pages.py` anropar.

**Tech Stack:** Python 3.11+, SQLite (`src/db/`), `openai`-klienten (redan beroende) för OpenAI-kompatibla endpoints, Streamlit (Admin), pytest/ruff/mypy.

**Spec:** `docs/superpowers/specs/2026-11-14-skanna-om-sidor-design.md`

## Global Constraints

- Svenska i kommentarer, docstrings, loggrader, felmeddelanden och UI-text.
- All SQL i `src/db/`; nya db-funktioner re-exporteras i `src/db/__init__.py` (`test_sql_boundary.py`).
- Ny toppnivåmodul (`src/llm_vision.py`) läggs i `py-modules` i `pyproject.toml` (`test_packaging.py`).
- Ny operation = registerpost + `scripts/rescan_pages.py` + `./rescan_pages.sh` i samma ändring (`test_scripts.py`).
- Varje fält i Admin-formuläret har en punkt i `OPERATION_HELP` i formulärordning.
- Motorer: `tesseract`, `vision`, `surya`, `surya-layout`, `llm-vision`. Standard `surya-layout`.
- Standardvärden: `threshold` 70, `source` `any`, `max_pages` 0 (alla), `dry_run` false, `reindex` true, `jobs` 4, `dpi` 300.
- `engine_identity` för bildmotorn: `llm-vision:<profilnamn>:<modell>`.
- Nytt historikskäl `rejected`; schemaversion 12 → 13.
- Avbrott: rc 3, LLM-fel 401/402/403/429 efter återförsök, 5 dokument i rad helt misslyckade.
- Debugloggning enligt AGENTS.md (`ctx.log(..., level="debug")`); aldrig nycklar eller hela sidtexter i loggen.
- **Ingen `git commit`/`git push`** (AGENTS.md: användaren kör `/cap`). Uppgifternas sista steg är att hela testsviten är grön.

## Review Focus

1. **Sidan vars nya läsning är tom eller kraschade** — gammal läsning orörd, historikraden borttagen, körningen fortsätter (testas i Task 5).
2. **Samma körning två gånger** — andra körningen väljer inga sidor som redan lästs med samma motor, även när de fortfarande ligger under tröskeln (Task 4).
3. **Raderade dubbletter och PDF som saknas** — aldrig valda, aldrig en ny textfil (Task 4).
4. **Hel-fils-OCR vinner jämförelsen** — sidan får en `pdf_pages`-rad med motorn `hel-fils-OCR` och väljs inte av Full OCR:s redo nästa gång (Task 5).
5. **Avbrott mitt i en parallell `llm-vision`-körning** — inga halvskrivna historikrader för sidor vars `pdf_pages` inte ändrats (Task 5).

---

### Task 1: Historikskälet `rejected` (schema 13) och urvalsfrågor

**Files:**
- Modify: `src/db/_schema.py` (tabellen `pdf_page_readings`, `SCHEMA_VERSION`, `MIGRATIONS`)
- Modify: `src/db/pipeline.py`, `src/db/__init__.py`
- Modify: `src/pages/9_Sidgranskning.py:344` (etikett för skälet)
- Test: `tests/test_db_page_layout.py` (eller `tests/test_db.py` där historiktesterna bor)

**Interfaces:**
- Produces: `db.list_rescan_candidates(conn, *, threshold: float, source: str | None) -> list[sqlite3.Row]` med kolumnerna `pdf_stem, page_num, score, tesseract_done_at, source`, sorterade på `score` stigande; `source=None` = alla, annars `pdf_files.source` (`files`/`wpu`).
- Produces: `db.page_read_with(conn, pdf_stem: str, page_num: int, *, engine: str, engine_identity: str | None) -> bool` — sant om gällande `pdf_pages`-rad eller någon `pdf_page_readings`-rad har `engine`; när `engine_identity` inte är `None` måste även den stämma.
- Produces: `archive_page_reading(..., reason="rejected")` tillåts.

- [ ] **Step 1: Skriv de fallerande testerna**
  - `test_rejected_reason_is_allowed`: `archive_page_reading(..., reason="rejected")` lyckas på färsk databas.
  - `test_migration_13_keeps_readings_and_allows_rejected`: skapa en v12-databas (kör init på färsk db, sänk `schema_version`/`user_version` till 12 och återskapa tabellen med gamla CHECK), lägg in en `rescan`-rad, kör `init_schema` → raden finns kvar med samma id och `rejected` går att lägga in.
  - `test_list_rescan_candidates_filters_and_sorts`: sidor med poäng 30, 65, 80 och en bildsida 10 → tröskel 70 ger `[30, 65]` i den ordningen; `source="wpu"` filtrerar.
  - `test_page_read_with_matches_engine_and_identity`: rad i `pdf_pages` (`surya`) → `engine="surya"` sant; historikrad `llm-vision` med identitet `llm-vision:A:m` → sant för samma identitet, falskt för `llm-vision:B:m`; `engine="llm-vision"` påverkas inte av en `llm`-rad.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_db_page_layout.py -k "rejected or rescan_candidates or page_read_with" -v` → FAIL.
- [ ] **Step 3: Implementera.** `_migration_013_rejected_reason`: SQLite kan inte ändra CHECK, så tabellen byggs om (skapa ny tabell med `CHECK(reason IN ('rescan','restore','rejected'))`, kopiera rader med id, byt namn, återskapa indexet). Uppdatera `PAGE_READINGS_SCHEMA_SQL` för färska databaser. Sidgranskningen visar `rejected` som "underkänd vid jämförelse".
- [ ] **Step 4:** Samma kommando → PASS; därtill `.venv/bin/pytest tests/test_db*.py -q` grönt (gamla fixtures migreras).

### Task 2: Gemensam modul för läsningshistorik

**Files:**
- Create: `src/operations/page_readings.py`
- Modify: `src/operations/rescan.py` (använder modulen; beteendet oförändrat)
- Test: `tests/test_page_readings.py`; befintliga `tests/test_rescan_operation.py` ska förbli gröna utan ändringar

**Interfaces:**
- Produces (flyttade ur `rescan.py`, samma beteende):
  - `archive_current(root: Path, pdf_stem: str, page: int, reason: str, ctx) -> int | None`
  - `row_snapshot(pdf_stem: str, page: int) -> dict | None`
  - `forget_failed_attempt(pdf_stem: str, page: int, reading_id: int | None, before: dict | None, ctx) -> None`
  - `restore_reading(pdf_stem: str, page: int, reading: Mapping, ctx) -> None` (tidigare `_restore`)
- Produces (nya):
  - `comparison_score(text: str) -> float` = `score_text(normalize(text), use_hunspell=False)["score"]`.
  - `reject_new_reading(root: Path, pdf_stem: str, page: int, old: Mapping, ctx) -> int` — arkiverar gällande (nya) `pdf_pages`-rad med `reason="rejected"` och skriver `old` som gällande rad. Är `old["engine"] == WHOLE_FILE_ENGINE` skrivs en `pdf_pages`-rad med den motorn (tas **inte** bort). Returnerar historikradens id.

- [ ] **Step 1: Skriv fallerande tester** i `tests/test_page_readings.py`:
  - `test_comparison_score_uses_normalized_text`: text med ligatur och samma text utan → samma poäng.
  - `test_reject_new_reading_restores_old_and_archives_new_as_rejected`: gammal läsning (`surya`, "god text") arkiverad, ny rad (`tesseract`, "skräp") gällande → efter anropet är `pdf_pages` den gamla, och en historikrad `rejected` har den nya texten.
  - `test_reject_whole_file_reading_keeps_a_page_row`: gammal läsning med motor `hel-fils-OCR` → `get_page_row` finns, `engine == "hel-fils-OCR"`, `page_exists` sant.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_page_readings.py -v` → FAIL (modulen saknas).
- [ ] **Step 3:** Flytta funktionerna (rescan.py importerar dem; byt `_restore` mot `restore_reading`), implementera de nya.
- [ ] **Step 4:** `.venv/bin/pytest tests/test_page_readings.py tests/test_rescan_operation.py -q` → PASS.

### Task 3: Bildmotorn `llm-vision`

**Files:**
- Create: `src/llm_vision.py`; lägg `llm_vision` i `py-modules` i `pyproject.toml`
- Modify: `src/ocr_pages.py` (`--engine llm-vision`, `--profile`, `run_ocr_pages(..., profile: str | None = None)`)
- Modify: `src/operations/rescan.py` `ENGINES` och registrets `rescan-page`-val (så att den nya motorn även går att välja för en sida)
- Test: `tests/test_llm_vision.py`, `tests/test_ocr_pages_llm_vision.py`

**Interfaces:**
- Produces i `llm_vision.py`:
  - `class VisionProfile` (dataclass): `name, model, base_url, api_key, external: bool`.
  - `load_profile(name: str) -> VisionProfile` — `config.load_profile` + `resolve_runtime_profile`; okänt namn eller backend `claude` → `ValueError` med förklaring. `external` = värden i `base_url` är inte `localhost`/`127.0.0.1`/`::1`.
  - `transcribe(image: PIL.Image.Image, profile: VisionProfile, *, client=None) -> VisionResult` där `VisionResult(markdown: str, input_tokens: int | None, output_tokens: int | None)`. Bilden skickas som PNG base64 i ett `image_url`-innehåll; längsta sida skalas ned till högst 2000 px. `finish_reason` annat än `stop` → `VisionError`.
  - `class VisionError(Exception)` med `fatal: bool` (sant för HTTP 401/402/403 och 429 efter återförsök — 3 försök med backoff 2/4/8 s).
  - `check_profile(profile, *, client=None) -> None` — renderar testbilden (text "PALME 1986" i svart på vitt, PIL) och kräver att svaret innehåller `PALME` och `1986`; annars `VisionError(fatal=True, "profilen X kan inte läsa bilder")`.
  - `insert_mask_markers(markdown: str, blocks: list[tuple[int,int]], image_height: int) -> str` — `[MASKAD]` som eget stycke på Markdown-raden proportionellt mot blockets mitthöjd (samma regel som `_merge_redaction_markers` utan radkoordinater).
  - `markdown_to_text(markdown: str) -> str` — tar bort `#`, `**`, `|`-tabellsyntax och listmarkörer; behåller radbrytningar och `[MASKAD]`.
  - `PROMPT: str` (svensk, ordagrann avskrift som Markdown; `[oläsligt]`; inget om svarta fält; inga kommentarer).
- Produces i `ocr_pages`: `--engine llm-vision --profile <namn>`; `record_page(engine="llm-vision", markdown=…, text=markdown_to_text(…), engine_identity="llm-vision:<profil>:<modell>")`; inget PDF-textlager för motorn; `VisionError(fatal=True)` → exitkod **4** (`EXIT_LLM_FATAL`), övriga `VisionError` hoppar sidan. Tokens per sida loggas på debugnivå och summan per dokument som en info-rad (`[llm-vision] <stam>: N sidor, ↑in ↓ut token`) — det är så designens tokensammanfattning når jobbloggen.

- [ ] **Step 1: Fallerande tester** (falsk klient med `chat.completions.create`, inga nätanrop):
  - `test_transcribe_sends_png_and_returns_markdown`: klienten får en bild-URL som börjar med `data:image/png;base64,` och modellnamnet; resultatet bär Markdown och tokens.
  - `test_transcribe_length_cut_is_an_error`: `finish_reason="length"` → `VisionError`, `fatal` falskt.
  - `test_transcribe_429_retries_then_fatal`: tre 429 → `VisionError(fatal=True)`; anropet gjordes 3 gånger (sleep monkeypatchad).
  - `test_check_profile_rejects_model_without_vision`: svar "Jag kan inte se bilder" → `VisionError(fatal=True)` med profilnamnet i texten.
  - `test_load_profile_marks_external_and_rejects_claude`: OpenRouter-profil → `external` sant; Ollama `http://localhost:11434/v1` → falskt; Claude-profil → `ValueError`.
  - `test_insert_mask_markers_and_text_agree`: block i övre halvan av en 4-raders Markdown → `[MASKAD]` före rad 3; `markdown_to_text` behåller markören och tar bort `## `.
  - `test_ocr_pages_llm_vision_records_markdown_and_identity` (i `test_ocr_pages_llm_vision.py`): `run_ocr_pages` med `llm_vision.transcribe` monkeypatchad på en enkel PDF → raden har `engine="llm-vision"`, Markdown, ren text och identiteten.
  - `test_ocr_pages_llm_vision_fatal_error_exit_code`: `transcribe` höjer `VisionError(fatal=True)` → exitkod 4, ingen rad skriven.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_llm_vision.py tests/test_ocr_pages_llm_vision.py -v` → FAIL.
- [ ] **Step 3:** Implementera modulen och kopplingen i `ocr_pages.py` (maskeringsdetektering på bilden som för övriga motorer, sedan `insert_mask_markers` på Markdown).
- [ ] **Step 4:** Samma kommando → PASS; `.venv/bin/pytest tests/test_ocr_pages*.py tests/test_rescan_operation.py tests/test_packaging.py -q` grönt.

### Task 4: Urval och torrkörning

**Files:**
- Create: `src/operations/rescan_pages.py`
- Test: `tests/test_rescan_pages.py`

**Interfaces:**
- Consumes: `db.list_rescan_candidates`, `db.page_read_with` (Task 1); `page_review.is_removed_duplicate`, `page_review.find_pdf`.
- Produces: `select_pages(root: Path, *, engine: str, identity: str | None, threshold: float, source: str, max_pages: int, ctx) -> dict[str, list[int]]` — dokument → sidor (sorterat), i ordningen lägst poäng först, högst `max_pages` sidor totalt (0 = alla). Debugloggar antal bortvalda per skäl (dubblett, PDF saknas, redan läst med motorn) och de tre första exemplen.

- [ ] **Step 1: Fallerande tester:**
  - `test_select_skips_duplicates_missing_pdfs_and_already_read`: fyra dokument — vanligt, raderad dubblett, utan PDF, sida redan läst med `surya` — motorn `surya` → bara det vanliga.
  - `test_select_same_engine_twice_selects_nothing`: efter att en sida fått en `surya-layout`-rad väljs den inte igen trots poäng 20.
  - `test_select_llm_vision_other_model_is_reselected`: historikrad `llm-vision:A:m1` → identiteten `llm-vision:A:m2` väljer sidan.
  - `test_select_max_pages_takes_worst_first`: poäng 60, 20, 40 och `max_pages=2` → sidorna med 20 och 40.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_rescan_pages.py -k select -v` → FAIL.
- [ ] **Step 3:** Implementera `select_pages`.
- [ ] **Step 4:** Samma kommando → PASS.

### Task 5: Körning, jämförelse, felpolicy, kvalitet och indexering

**Files:**
- Modify: `src/operations/rescan_pages.py`
- Test: `tests/test_rescan_pages.py`

**Interfaces:**
- Consumes: Task 2 (`archive_current`, `row_snapshot`, `forget_failed_attempt`, `comparison_score`, `reject_new_reading`), Task 3 (`load_profile`, `check_profile`, `EXIT_LLM_FATAL`), Task 4 (`select_pages`), `operations.ocr.merge_and_normalize(stem, …, only_pages=…)`, `quality.run_quality(files_from=…)`, `ingest_adapter` med `only_stems`.
- Produces: `run_rescan_pages(*, root: Path, engine: str, profile: str, threshold: float, source: str, max_pages: int, dry_run: bool, reindex: bool, jobs: int, dpi: int, context=None) -> int` och en `RescanSummary` (dataclass: `selected, scanned, improved, kept_old, failed`). Tokens räknas inte här — `ocr_pages` körs som underprocess och loggar dem själv (Task 3).

Flöde per dokument (sekventiellt, eller med `ThreadPoolExecutor(jobs)` över dokument när motorn är `llm-vision`): arkivera varje sida → `ocr_pages.py --pages … --force --engine … --dpi … [--profile …]` via `ctx.run_process` → per sida: oförändrad `row_snapshot` = misslyckad (`forget_failed_attempt`), annars jämför `comparison_score(ny)` mot `comparison_score(gammal)`; lägre → `reject_new_reading` → `merge_and_normalize(only_pages=<sidor med ny gällande läsning>)`. Innan första dokumentet för `llm-vision`: `load_profile` + `check_profile`, och info-rad "sidbilder skickas till <host> (extern)" eller "(lokal)".

- [ ] **Step 1: Fallerande tester** (`ctx.run_process` ersatt av en falsk process som skriver `pdf_pages` som i `test_rescan_operation.py`):
  - `test_better_reading_is_kept`: gammal "x#&¤" mot ny "Protokoll fört vid förhör" → ny gällande, textfilens sida uppdaterad, `summary.improved == 1`.
  - `test_worse_reading_is_rejected_and_restored`: omvänt → gammal gällande, historikrad `rejected` med den nya texten, `kept_old == 1`, textfilen orörd.
  - `test_failed_page_keeps_old_and_continues`: första dokumentet skriver ingen rad (rc 1), andra lyckas → historikraden för det första borttagen, andra förbättrat, rc ≠ 0 och `failed == 1`.
  - `test_surya_unavailable_aborts`: rc 3 → `OperationFailed`; nästa dokument körs inte.
  - `test_five_failed_documents_in_a_row_abort`: sex dokument som alla misslyckas → `OperationFailed` efter fem underprocesser.
  - `test_llm_fatal_exit_aborts`: rc 4 → `OperationFailed` med "LLM".
  - `test_dry_run_writes_nothing`: inga underprocesser, inga historikrader; loggen har antal sidor och dokument.
  - `test_quality_and_ingest_only_for_touched_documents`: `run_quality` får en lista med bara berörda stammar (även när `reindex=False`); ingest får `only_stems` = samma mängd och anropas inte när `reindex=False`.
  - `test_whole_file_winner_is_not_redone_by_full_ocr`: gammal hel-fils-OCR vinner → `page_exists` sant efteråt.
  - `test_cancel_during_parallel_llm_run_leaves_no_orphan_readings`: avbrott efter första dokumentet med `jobs=2` → varje kvarvarande historikrad har en ändrad `pdf_pages`-rad.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_rescan_pages.py -v` → FAIL.
- [ ] **Step 3:** Implementera `run_rescan_pages`. Bekräfta att `ctx.run_process` och `ctx.log` tål anrop från flera trådar (läs `operations/context.py`); gör de inte det, serialisera loggskrivningen med ett lås i `rescan_pages.py`.
- [ ] **Step 4:** Samma kommando → PASS.

### Task 6: Operation, Admin, skript och dokumentation

**Files:**
- Modify: `src/operations/registry.py` (post `rescan-pages`, grupp `OCR`, `mutating=True`, parametrar enligt Global Constraints; `engine` med valen ovan; `profile` som str)
- Modify: `src/operations/adapters.py` (`rescan_pages_adapter`)
- Create: `scripts/rescan_pages.py`, `rescan_pages.sh` (kopior av mönstret för `rescan_page`)
- Modify: `src/admin_ui.py` (`OPERATION_HELP["rescan-pages"]` med en punkt per fält i formulärordning, `FORM_LAYOUT` med etikett "Skanna om sidor" och knappen "Skanna om")
- Modify: `AGENTS.md` (katalogträdet, kommandolistan, ett stycke under Non-obvious Design Decisions: urvalsregeln, jämförelsen, `rejected`, hel-fils-raden, `llm-vision` vs `llm`), `docs/teknisk-referens.md` (nytt avsnitt med flaggtabell, motorn, kostnadsbroms), `docs/kom-igang.md` (en rad)
- Test: `tests/test_operation_registry.py`, `tests/test_admin_ui.py`, `tests/test_scripts.py` (befintliga bevakningar)

- [ ] **Step 1: Fallerande test** `test_rescan_pages_operation_defaults` i `tests/test_operation_registry.py`: posten finns, standardvärdena är de i Global Constraints och `engine`-valen är de fem motorerna.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_operation_registry.py tests/test_scripts.py tests/test_admin_ui.py -q` → FAIL.
- [ ] **Step 3:** Implementera registerpost, adapter, skript, genväg, hjälp och dokumentation.
- [ ] **Step 4:** `.venv/bin/python scripts/test.py --static` → alla tester, ruff och mypy gröna.
- [ ] **Step 5:** Manuell rök i förgrunden: `.venv/bin/python scripts/rescan_pages.py --engine tesseract --threshold 30 --max-pages 2 --dry-run` skriver urvalet utan att ändra något. (Kör **inte** utan `--dry-run` mot det riktiga arkivet utan ägarens klartecken.)
