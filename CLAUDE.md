# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit och push

Kör **inte** `/cap` automatiskt efter varje ändring — användaren kör det själv när hen är redo. Kör aldrig `git commit`/`git push` direkt heller.

## Dokumentation hålls synkad

När du gör förändringar i projektet ska du **alltid** uppdatera den användarvända dokumentationen och `CLAUDE.md` i samma commit — om ändringen påverkar något i dem.

- `README.md` — presentationssida (svenska): vad projektet är + de tre skärmbilderna, länkar vidare
- `docs/kom-igang.md` — snabbstart (svenska): krav, installation, API-nyckel, kör pipelinen, ställ första frågan
- `docs/teknisk-referens.md` — detaljerad dokumentation (svenska): alla steg/flaggor, state-db, kunskapsgraf, LLM-config, filöversikt, tester
- `CLAUDE.md` — instruktioner för framtida Claude-sessioner

## Project Overview

**palmemordsarkivet** laddar ner, OCR-processar och gör 3 762 palme-PDF:er + 7 155 wpu-PDF:er (~47 000 sidor, 8 665 sökbara dokument efter merge) från palmemordsarkivet.se och wpu.nu sökbara via RAG + Codex Opus 4.7, med Streamlit-flikarna Utredning, Utredningspärm, Graf, Maskeringar, Jämförelse och Karta. Alla kommentarer och docstrings är på svenska.

Pipeline: download → OCR (Tesseract + optional Surya) → detect redactions → normalize → quality scoring → LanceDB ingest → RAG query → Streamlit UI.

## Directory Structure

```
src/
  db.py                # SQLite-state: schema + CRUD + delta-queries för pipeline + utredningspärm
  download.py          # Google Drive PDF downloader (state via db.py)
  quality.py           # OCR quality scoring (0–100 heuristics + optional hunspell)
  ocr_pages.py         # Per-page OCR pipeline (Tesseract/Surya) + redaktionsdetektering
  normalize_text.py    # Rule-based OCR normalization (ligatures, whitespace). Inkrementellt via state.db.
  llm_correct.py       # LLM post-correction av dåliga OCR-sidor via Codex Haiku
  merge_pages.py       # Slå ihop per-sida-text från state.db → text/<stem>.txt
  build_user_words.py  # Generera Tesseract user-words från OCR-text
  errors_log.py        # Centraliserad felloggning (tab-separerad)
  Utredning.py         # Streamlit-frågesida (RAG/MCP, svarsgraf, sparknapp, bokmärken, facett-/fuzzy-filter)
  casebook_ui.py       # Delade Streamlit-komponenter för utredningspärm + bokmärken + anteckningar
  redactions.py        # Maskeringsutforskaren: aggregera [MASKAD]-markörer ur pdf_pages
  facets.py            # Facetterad sökning: entiteter ur doc_entities → filtrera träffar
  search_fuzzy.py      # OCR-tolerant fuzzy-sökning (difflib token-index över chunk-korpusen)
  compare.py           # Vittnesjämförelse: promptbyggare som ställer källor mot varandra
  karta.py             # Ren kartlogik: validering, popup-HTML och TimestampedGeoJson
  map_extract.py       # Ren parsing/platsmatchning för kartobservations-kandidater
  extract_map_observations.py # LLM-extraktion av person-position-tid-kandidater
  pages/
    2_Utredningspärm.py # Sparade fråga/svar-spår, källbokmärken och anteckningar
    3_Graf.py          # Fristående grafsida
    5_Maskeringar.py   # Maskeringsutforskaren (dokument sorterade efter antal [MASKAD])
    6_Jämförelse.py    # Vittnesjämförelse (korsförhörsläge — letar motstridiga uppgifter)
    7_Karta.py         # Karta över mordkvällens observationer med formulär och tidslinje
  rag/
    ingest.py          # LanceDB vector index builder
    ask.py             # RAG query + Codex integration
  graph/
    extract_entities.py # Per-sida entitets-/relationsextraktion via LLM → doc_entities i state.db
    load_neo4j.py       # Ladda doc_entities → Neo4j (MERGE, idempotent) + namnkanonisering
    viz.py              # Ego-nätverk för flera center (Cypher → noder/kanter, dedup) + Cytoscape-konvertering
    answer_entities.py  # LLM (Haiku) listar nyckelentiteter ur ett RAG-svar → inline-grafen i Utredning
tests/                 # pytest (test_quality, test_chunk, test_download, test_merge_pages, test_detect_redactions, test_reingest, test_normalize_text, test_answer_entities, test_redactions, test_facets, test_search_fuzzy, test_compare)
*.sh                   # Bash-wrappers (aktiverar .venv, läser API-nycklar, vidarebefordrar flaggor)
tessdata/              # swe_best.traineddata, swe.user-words, tesseract.config
```

Data-kataloger (gitignored):
- `downloaded/files/`, `downloaded/wpu_files/` — PDF:er
- `generated/text/` — OCR-text, `generated/text_pages/` — per-sida-artefakter
- `generated/lancedb/` — vektorindex
- `generated/db/state.db` — SQLite-databas med pipeline-state (markörer, kvalitet, ingest-mtime) samt Utredningspärm, källbokmärken och källanteckningar (`source_annotations`). WAL-filer (`state.db-wal`, `state.db-shm`) ligger bredvid.
- `generated/errors.log` — fellog

Repo-data:
- `data/karta/platser.json`, `data/karta/rorelser.json` — seed-filer för kartans platser och observationer

## Commands

```bash
# Tests
./test.sh                             # pytest
./test.sh --static                    # pytest + ruff/mypy via .venv
.venv/bin/pytest tests/               # bara pytest

# Setup
./install.sh                         # Installera pipeline/webgränssnitt (brew, Python-paket, tessdata)
./install.sh --dev                   # Installera även pytest/ruff/mypy för utveckling

# Workflow
./run_pipeline.sh                    # Hela pipelinen i ett: download → OCR → ingest
./run_pipeline.sh --test 5           # Testläge: bara 5 filer från palme + 5 från wpu
./download.sh                        # Hämta PDF:er från Google Sheet
./ocr.sh                             # Tesseract → normalize → quality → Surya på dåliga sidor
./ocr.sh --skip-redo                 # Bara Tesseract + normalize + quality
./ocr.sh --redo --mode pages         # Surya på specifika sidor (threshold 50)
./ocr_tesseract.sh                   # Bara Tesseract-steget
./detect_redactions.sh               # Redaktionsdetektering på befintliga text/OCR-par
./quality.sh [--top 30] [--per-page]
./llm_correct.sh [--threshold 60]    # Codex Haiku korrigerar dåliga sidor
./merge_pages.sh --all               # Slå ihop per-sida-text från state.db → text/
./build_user_words.sh                # Bygg tessdata/swe.user-words.auto från text/*.txt
./ingest.sh [--rebuild] [--reindex-since 2026-05-01]
./ask.sh "fråga" [--hybrid] [--no-rerank]
./web.sh                             # Starta Streamlit

# wpu.nu (valfritt)
./download_wpu.sh && ./merge_wpu.sh

# Kartobservations-kandidater (valfritt; LLM extraherar förslag till granskning)
./extract_map_observations.sh [--dry-run] [--limit N]

# Kunskapsgraf (valfritt; installera först podman + Python-extra .[graph])
./extract_entities.sh [--limit N] [--dry-run]
./neo4j.sh
./load_graph.sh
```

Env-variabler: `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max, räknas mot prenumeration) eller `ANTHROPIC_API_KEY`. Valfritt: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`.

## Non-obvious Design Decisions

**MCP-läge (Utredning.py)**: Konversationskontinuitet uppnås genom att fånga `session_id` från `ResultMessage` och skicka tillbaka det som `ClaudeAgentOptions(resume=...)` på nästa fråga. "Ny konversation" nollställer `chat_history` + `mcp_session_id`.

**SQLite-state (`generated/db/state.db`)**: all operativ pipeline-state lever här —
downloads, per-PDF-status (redaktion/merge/normalize), per-sida OCR-resultat,
kvalitetspoäng, LLM-korrigeringar och ingest-tracking. Utredningspärmen
lever också här i `casebook_entries`, `source_bookmarks` och `source_annotations`
(utredarens fritextanteckningar — flera per källa/sida, till skillnad från
bokmärken). Karta-flikens granskningskö använder `map_observation_candidates`
och `map_observation_extractions` (per-sida-markör även när inga kandidater
hittas). Inkrementell logik
bygger på att jämföra `pdf_files.text_mtime` mot `normalized_at`/`scored_at`/etc.
`--rebuild` tvingar omkörning. Modulen `src/db.py` exponerar alla CRUD- och delta-queries; konsumenter skriver aldrig egen SQL.

**Utredningspärm → Graf**: sparade svar i Utredningspärmen visas kollapsade.
När en post öppnas renderas källor som källkort med PDF/text-knappar och
grafentiteter som en länk till Graf-sidan. Länken kodar sparade center-entiteter
i query-parametern `centers`; `src/pages/3_Graf.py` läser den och återskapar
startgrafen utan ny sökning.

**Kartmodulen (`src/karta.py` + `src/pages/7_Karta.py`)**: Observationer lever i
state.db (`map_places`, `map_observations`) och seedas från `data/karta/*.json`
bara om karttabellerna är tomma. `src/karta.py` är Streamlit-fri och bygger
validerad TimestampedGeoJson; `map_observations.time` lagras som `HH:MM` och
expanderas mot basdatum `1986-02-28` innan tidslinjen ritas. UI:t visar inte
observationer i tidslinjen utan tid, koordinat och källa.

**Kartobservations-extraktion (`src/extract_map_observations.py` + `src/map_extract.py`)**:
LLM-extraktorn skriver bara till `map_observation_candidates`. Kandidater är
granskningsmaterial, inte fakta på kartan. Först när användaren godkänner en
kandidat i Karta-fliken (**Granska extraherade kartförslag**) skapas en rad i
`map_observations`. Platsmatchning mot `data/karta/platser.json` är transparent
(exact/fuzzy/none); okända platser måste få koordinater innan godkännande.
Varje lyckad sida markeras dessutom i `map_observation_extractions`, även om
LLM:en returnerar noll kandidater, så omkörningar inte debiterar tomma sidor igen.

**Kunskapsgraf byggs lazy (Utredning.py)**: `_render_answer_graph` tar svaret,
inte färdiga centers. Den dyra entitetsextraktionen (`_compute_answer_centers`
→ Claude Haiku) och Neo4j-frågorna körs **först när användaren öppnar
graf-toggeln** — inte automatiskt efter varje svar. Resultatet cachas per svar
i session state (`{state_key}_centers`/`_answer`) och returneras så
utredningspärmen kan spara det. Gäller både RAG- och MCP-turerna.

**Sökfilter i Utredning (RAG-läget)**: Sidofältet har facett-filter och
OCR-tolerant fuzzy-sökning. Facetterna kommer ur `doc_entities` via
`src/facets.py` och begränsar träffarna till dokument som nämner valda
entiteter (efter sökning, före rerank). Fuzzy-sökningen (`src/search_fuzzy.py`,
ren `difflib`) bygger ett token-index över hela chunk-korpusen *en gång*
(`st.cache_resource`, ~100 MB) och slås bara på vid behov — den fångar
söktermer som OCR:en felstavat. Båda gäller bara RAG-läget, inte MCP-läget
(där modellen söker själv).

**Maskeringsutforskaren (`src/redactions.py` + `pages/5_Maskeringar.py`)**:
aggregerar `[MASKAD]`-markörerna ur `pdf_pages` och visar dokument sorterade
efter antal maskeringar med kontextutdrag — "vad har dolts".

**Vittnesjämförelse (`src/compare.py` + `pages/6_Jämförelse.py`)**: ett
korsförhörsläge. Promptbyggaren är ren/testbar; sidan kör samma sök+rerank som
Utredning men med en system-prompt som letar *motstridiga* uppgifter mellan
källor i stället för att syntetisera bort dem. Följer det backend-val som
sparats i `generated/llm_config.json`.

**mtime-tracking (ingest.py)**: Varje `.txt`-fil jämförs mot mtime som lagras i `ingest`-tabellen i state.db (auktoritativt); nyare fil → re-ingest. LanceDB-tabellen har fortfarande en `mtime`-kolumn men den läses inte längre för delta-beslut. Om ingest-state saknas men källan finns i LanceDB behandlas filen som re-indexering, så gamla chunks ersätts i stället för att dupliceras. Legacy-rader med sentinel-mtime `0.0` re-indexeras bara med `--reindex-since`.

**Redaktionsdetektering (ocr_pages.py)**: Hittar svarta maskeringsblock i bilder och infogar `[MASKAD]` i texten. På som standard; `--no-detect-redactions` stänger av. Idempotens spåras via `pdf_files.redaction_checked_at` i state.db — `.redact`-markörfilerna är borttagna. `detect_redactions.sh` förfiltrerar listan baserat på databasen så filer som redan kollats spawnar inga subprocesser alls.

**Per-dokument-cleanup (ocr.sh + Surya)**: Efter att `ocr_pages.py` är klar kör `ocr.sh` automatiskt `merge_pages.merge_one` + `normalize_text.process_file`. Idempotens och per-sida-text spåras i `pdf_pages`-tabellen i state.db — `page-NNN.txt`/`.json`-markörerna existerar inte längre.

**Pipeline-resume (`run_pipeline.sh`)**: OCR- och ingest-stegen körs alltid, även när download inte hittade nya PDF:er. Stegen är idempotenta och detta gör att en tidigare avbruten körning kan slutföras.

## Common Gotchas

1. **Beroendepinning**: `sentence-transformers<5` och `transformers<5` är avsiktligt pinnade — v5 bryter cross-encoder och AutoProcessor.
2. **text-layer ≠ bra kvalitet**: Vissa PDF:er har gammal OCR-skräp inbäddad. Sortera på score, inte källa.
3. **Surya-prestanda**: ~30–100 s/sida vs ~1 s/sida för Tesseract. Körs bara på sidor under threshold.
4. **FTS kräver tantivy**: Saknas det faller hybrid-sökning tillbaka på vektor-only.
5. **OAuth vs API**: `CLAUDE_CODE_OAUTH_TOKEN` räknas mot Pro/Max-prenumerationen; `ANTHROPIC_API_KEY` drar API-credits.
6. **SQLite WAL-filer**: `generated/db/state.db-wal` och `-shm` är normala WAL-filer och syncas vid checkpoint. Säkerhetskopiera alla tre samtidigt.
7. **Kartor kräver nät för bakgrundsrutor**: folium-markörer och spår renderas även offline, men OSM-bakgrunden kräver internet.
