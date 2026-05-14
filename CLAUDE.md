# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Dokumentation hålls synkad

När du gör förändringar i projektet (ny funktionalitet, ändrade kommandon, nya beroenden, ändrad arkitektur, nya/borttagna filer eller skript, ändrade miljövariabler, etc.) ska du **alltid** uppdatera både `CLAUDE.md` och `README.md` så att de speglar det nya tillståndet — i samma commit som kodändringen.

- `README.md` är den användarvända dokumentationen (svenska) — uppdatera installations-, körnings- och användningsinstruktioner.
- `CLAUDE.md` är instruktioner för framtida Claude-sessioner — uppdatera arkitektur-, kommando- och modulreferenser.

Om en ändring inte påverkar något i dessa filer, behöver de inte röras. Men om du är osäker — uppdatera dem hellre än att låta dem bli inaktuella.

## Project Overview

**palmemordsarkivet** is a Swedish language project that downloads, OCR-processes, and provides searchable access to ~3,700 PDF documents (~33,000 pages) from [palmemordsarkivet.se](https://palmemordsarkivet.se), a public Google Sheet archive related to the Palme murder investigation.

The system combines multiple components:
- PDF download and manifest tracking
- Multi-stage OCR pipeline (Tesseract + optional Surya transformer-OCR)
- OCR quality assessment with heuristics
- Vector embedding and LanceDB indexing
- RAG-based Q&A interface with Claude Opus 4.7 (via Claude Agent SDK)
- Streamlit web UI with multiple AI backend support

All components are Swedish-language facing; comments and docstrings are in Swedish.

## Directory Structure

```
.
├── src/                      # Core Python modules
│   ├── download.py          # Google Drive PDF downloader with manifest tracking
│   ├── quality.py           # OCR quality scoring (heuristics + optional hunspell)
│   ├── ocr_pages.py        # Per-page OCR pipeline (Tesseract/Vision/Surya) + redaktionsdetektering
│   ├── build_user_words.py  # Generate Tesseract user-words from OCR text
│   ├── errors_log.py        # Centralized error logging (tab-separated)
│   ├── normalize_text.py    # Rule-based OCR text normalization (ligatures, whitespace)
│   ├── llm_correct.py       # LLM post-correction of bad OCR pages via Claude Haiku
│   ├── webui.py            # Streamlit web interface
│   ├── merge_pages.py       # Slå ihop generated/text_pages/<stem>/page-*.txt → generated/text/<stem>.txt
│   └── rag/
│       ├── ingest.py       # LanceDB vector index builder
│       └── ask.py          # RAG query interface with Claude integration
├── tests/                   # pytest test suite
│   ├── test_quality.py      # Tests for score_text heuristics
│   ├── test_chunk.py        # Tests for text chunking logic
│   ├── test_download.py     # Tests for download utilities
│   └── conftest.py          # pytest fixtures
├── *.sh                     # Bash wrapper scripts for Python modules
├── pyproject.toml          # Package definition with optional dependencies
├── tessdata/               # Tesseract language data + config
│   ├── swe.user-words      # Manual Palme-specific terms
│   └── tesseract.config    # Tesseract settings (preserve_interword_spaces)
└── README.md               # Detailed Swedish-language documentation
```

Data directories (gitignored, auto-generated):
- `downloaded/files/` — downloaded PDFs from palmemordsarkivet
- `downloaded/wpu_files/` — downloaded PDFs from wpu.nu
- `generated/text/` — extracted OCR text
- `generated/text_pages/` — per-page OCR artifacts (merged into `generated/text/` by `merge_pages.py`)
- `generated/text_wpu/` — wpu.nu merge marker files (`.done`)
- `generated/ocr/` — searchable PDFs with embedded text layers
- `generated/lancedb/` — vector index
- `generated/quality.csv`, `generated/quality_pages.jsonl` — quality reports
- `generated/errors.log` — error log
- `test_files/`, `test_ocr/`, `test_txt/` — test artifacts

## Build, Run, and Test Commands

### Installation

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip

# Core dependencies
.venv/bin/pip install -e .

# Optional extras
.venv/bin/pip install -e .[webui]     # Streamlit + OpenAI
.venv/bin/pip install -e .[surya]     # Surya OCR (transformers<5, pymupdf)
.venv/bin/pip install -e .[dev]       # pytest

# System dependencies (macOS)
brew install ocrmypdf tesseract-lang poppler unpaper claude-code
brew install hunspell  # optional, for Swedish dictionary checks
```

Download Swedish Tesseract model and hunspell dictionary:
```bash
./setup_tessdata.sh
# hunspell: place sv_SE.aff and sv_SE.dic in ~/Library/Spelling/
```

### Running Tests

```bash
# Run all tests
.venv/bin/pytest tests/

# Run specific test file
.venv/bin/pytest tests/test_quality.py -v

# Run single test
.venv/bin/pytest tests/test_quality.py::test_perfect_text_high_score -v

# Show test output + print statements
.venv/bin/pytest tests/ -s
```

Test coverage:
- `test_quality.py`: `score_text()` heuristic function
- `test_chunk.py`: `chunk_text()` chunking logic
- `test_download.py`: `extract_drive_id()`, `sniff_extension()` utilities
- All tests use fixtures from `conftest.py` (includes optional pymupdf fixture)

### Main Workflow Scripts

All scripts are idempotent and can be re-run safely. They skip already-completed work.

```bash
# 1. Download PDFs from Google Sheet
./download.sh [--out downloaded/files] [--sheet-id ID]

# 2. OCR pipeline (recommended: full pipeline)
./ocr.sh                    # Tesseract → normalize → quality → Surya on bad pages
./ocr.sh --skip-redo        # Just Tesseract + normalize + quality scoring
./ocr.sh --redo --mode files  # Re-OCR entire files (--threshold 50)
./ocr.sh --redo --mode pages  # Surya on specific pages (--threshold 50)

# Manual OCR steps (if needed)
./ocr_tesseract.sh [--jobs 8] [--per-file-jobs 2] [--psm 4]
./normalize.sh               # Rule-based: fix ligatures, soft hyphens, whitespace (run by ocr.sh)
./quality.sh [--top 30]      # Build generated/quality.csv, optionally show worst N
./quality.sh --per-page      # Also write generated/quality_pages.jsonl (per-page scores)

# 2b. Text post-processing (optional, LLM-based)
# normalize.sh is now part of ocr.sh — no need to run separately
./normalize.sh --dry-run     # Preview changes
./llm_correct.sh             # LLM: correct bad pages with Claude Haiku (score < 50)
./llm_correct.sh --threshold 60 --dry-run  # Preview stricter threshold

# 3. Build/update vector index
./ingest.sh [--rebuild]      # Index generated/text/ → generated/lancedb/ (mtime-detektering på)
./ingest.sh --limit 100      # Test with subset
./ingest.sh --reindex-since 2026-05-01  # tvinga om för legacy-rader modifierade efter datum

# Slå ihop per-sida-text (generated/text_pages/) in i generated/text/ — engångsåtgärd

./merge_pages.sh --all
./merge_pages.sh --stem "<namn>"   # bara en fil

# 4. Query the archive
./ask.sh "Din fråga här"     # Single query
./ask.sh --no-rerank         # Skip cross-encoder (faster)
./ask.sh --hybrid            # Vector + BM25 hybrid search
./ask.sh                     # Interactive REPL

# 5. Web UI
./web.sh                     # Launch Streamlit in browser
```

Optional wpu.nu merging (jämför wpu-skanning mot palme-text och välj bäst):

```bash
./download_wpu.sh            # ladda ner wpu-PDF:er → downloaded/wpu_files/
./merge_wpu.sh               # parallell merging (default --jobs cpu_count)
./merge_wpu.sh --jobs 8      # explicit antal processer
./merge_wpu.sh --dry-run     # visa beslut utan att skriva
./merge_wpu.sh --rebuild     # ignorera .done-markeringar
```

Each `.sh` wrapper:
- Activates `.venv`
- Reads `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` from environment
- Passes through unknown flags to underlying Python script
- Supports `--help` for both wrapper and Python script

### Environment Variables

Required for querying:
```bash
# Pro/Max Claude subscription (preferred, counts against subscription hours)
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

# OR use API credits
export ANTHROPIC_API_KEY=sk-ant-...

# Optional: other backends in web UI
export OPENAI_API_KEY=...
export DEEPSEEK_API_KEY=...
```

Common flags (most scripts):
```bash
ROOT=...              # Project root (auto-detected, rarely needed)
TEXT_DIR=...          # OCR text directory (default: generated/text/)
FILES_DIR=...         # PDF directory (default: downloaded/files/)
DB_DIR=...            # LanceDB directory (default: generated/lancedb/)
THRESHOLD=50          # Quality score threshold for re-OCR
JOBS=4                # Parallel file jobs (ocr_tesseract.sh)
PER_FILE_JOBS=2       # Threads per file (ocr_tesseract.sh)
```

## Architecture and Data Flow

### The Four-Stage Pipeline

1. **Download** (`download.py`):
   - Fetches CSV from public Google Sheet (column: "Länk till kopia")
   - Extracts Google Drive IDs via regex
   - Downloads files with retries + exponential backoff
   - Detects file type via magic bytes
   - Tracks completion in `manifest.csv` (idempotency key)
   - Detects and logs duplicates (SHA1)

2. **OCR** (multi-step):
   - **Tesseract** (`ocr_tesseract.sh`): Default workhorse
     - Fast inline check: if PDF has usable text layer, extract with `pdftotext` (no OCR)
     - Otherwise: `ocrmypdf --skip-text --deskew --clean --rotate-pages` with PSM 6
     - Uses `tessdata/swe_best.traineddata` + user-words
     - Parallel: 4 files × 2 threads/file (configurable)
   - **Normalisering** (`normalize_text.py`): Regelbaserad textrensning — körs automatiskt av `ocr.sh` efter Tesseract (och efter varje Surya-sida). Rättar ligaturer, mjuka bindestreck, styrtecken och whitespace-artefakter. Idempotent.
   - **Quality scoring** (`quality.py`): Heuristic-based assessment (0–100 score)
     - Junk character ratio, short word ratio, vowel balance, OCR-specific artifacts
     - Optional hunspell dictionary check for Swedish words
     - Marks source as `text-layer` (original PDF had text) or `ocr` (Tesseract)
     - Output: `generated/quality.csv` (sortable by score)
   - **Surya** (`ocr.sh --redo`, optional): Fallback for low-scoring pages
     - Transformer-based (multilingual e5), slower but higher quality on degraded scans
     - Runs only on pages/files below threshold (default 50)
     - **Per-dokument-merge + normalize + cleanup**: efter att `ocr_pages.py` är klar med ett dokument kör `ocr.sh` automatiskt `merge_pages.merge_one` + `normalize_text.process_file` som (a) ersätter/normaliserar motsvarande sidor i `generated/text/<stem>.txt` och (b) raderar `page-NNN.txt` + `page-NNN.png` för de mergade sidorna. `page-NNN.json` behålls som idempotens-markör.
   - **Re-scoring**: Quality metrics updated after Surya pass

3. **Ingest** (`ingest.py`):
   - Reads `generated/text/*.txt` files
   - Chunks with 800-char size, 150-char overlap (breaks on line/space boundaries)
   - Filters out low-quality chunks (<55% alphanumeric)
   - Embeds using `intfloat/multilingual-e5-large` (1024-dim vectors)
   - Stores in LanceDB with metadata: `nr`, `titel`, `sida`, `anmärkning`, **`mtime`** (unix-tid när `.txt` skapades)
   - Detekterar ändrade filer: vid varje körning jämförs `.txt`-filens mtime mot lagrad mtime; nyare → re-ingest (delete + add).
   - Legacy-rader (utan känd mtime, t.ex. indexerade innan mtime-tracking) har `mtime=0.0`; de re-indexeras inte automatiskt. Använd `--reindex-since <tid>` för att tvinga om dem.
   - Auto-migrering: tabeller utan `mtime`-kolumn får den tillagd vid första körning efter uppgradering.
   - Creates FTS (full-text search / BM25) index on `text` column
   - Writes `unusable.txt` if any file produces zero chunks

4. **Query** (RAG interface):
   - User submits question
   - Search retrieves top-20 chunks (vector + optional BM25 hybrid)
   - Rerank with cross-encoder (`BAAI/bge-reranker-v2-m3`) → top-6
   - Format chunks with source metadata as context
   - Stream Claude Opus 4.7 response with system prompt (Swedish, cite sources)
   - Render citations as clickable PDF links (Streamlit only)

### Key Design Patterns

**Idempotency & Resumability**: All shell scripts track progress via output files (PDFs by filename, OCR by `.txt` existence, index by LanceDB table content). Interrupted runs continue from last successful file.

**Quality-Driven Re-OCR**: Instead of blanket re-OCR, the system scores text, identifies problematic files/pages, and targets improvements. Threshold configurable.

**Hybrid Search**: Vector search alone is good but imperfect. LanceDB FTS + RRF (reciprocal rank fusion, k=60) combines semantic and keyword matching.

**Chunking Strategy**: Documents split at 800-char boundaries but prefer line breaks (preserves semantic continuity). Overlap (150 chars) prevents information loss at boundaries. Per-page `\f` separators enable page-level metadata tracking.

**Error Logging**: All scripts append to `generated/errors.log` (ISO8601, tab-sep: timestamp, component, item, message) for audit trails and debugging.

## Core Modules Reference

### `download.py`

Key functions:
- `extract_drive_id(url: str)` → file_id or None (regex on Drive URLs)
- `sniff_extension(path: Path)` → extension string (magic bytes + filetype library)
- `build_filename(values: dict)` → sanitized filename (removes special chars)
- `drive_download(file_id, dest, session)` → SHA1 hex (handles virus-scan confirmation)

### `quality.py`

- `score_text(text: str, use_hunspell: bool) → dict`: Returns 0–100 score + detailed metrics (junk ratio, short words, vowel balance, etc.)
- `original_had_text(stem: str) → bool`: Checks if original PDF has usable text layer (uses `pdftotext` probe)
- `hunspell_pct(words: list) → float`: Percentage of words hunspell accepts as Swedish

Scoring formula: 100 - penalties for junk, short words, long words, digit-mixing, vowel imbalance, non-Swedish words.

### `src/rag/ingest.py`

- `chunk_text(text: str, size: int, overlap: int) → list[tuple]`: Smart chunking (prefers line breaks)
- `parse_filename(stem: str) → dict`: Extracts metadata from filename pattern
- `is_useful(chunk: str) → bool`: Filters chunks with <55% alphanumeric
- `should_reingest(stored_mtime, disk_mtime, reindex_since) → bool`: Re-index-beslut per fil (testat i `tests/test_reingest.py`)
- `find_orphans(stored_sources, disk_filenames) → list[str]`: Returnerar sources som finns i tabellen men saknas på disk; ingest raderar dem (skippas vid `--limit`).
- `parse_reindex_since(value: str) → float`: Tolkar `--reindex-since` (ISO 8601 eller unix-sekunder)

Schema: vector (1024 dims), text, source, page, chunk_idx, mtime, plus metadata fields (nr, titel, etc.).

### `src/ocr_pages.py`

- `detect_redactions_image(image, darkness, min_width_frac, min_height) → list[tuple[int,int]]`: Hitta svarta maskeringsblock i PIL-bild. Söker rader med ett sammanhängande mörkt span ≥ min_width_frac × sidbredd. Returnerar (y0, y1) per block.
- `_merge_redaction_markers(text, blocks, image_height, line_bboxes) → str`: Infoga `[MASKAD]` vid detekterade block. Med `line_bboxes` (surya-format) används exakta y-koordinater; annars approximeras positionen.
- Redaktionsdetektering är på som standard; stäng av med `--no-detect-redactions`.
- Detekterade block lagras även i `page-NNN.json` som `"redactions": N`.
- Testad i `tests/test_detect_redactions.py`.

### `src/normalize_text.py`

- `normalize(text: str) → str`: Rule-based normalization — ligature replacement, BOM/soft-hyphen removal, control-char stripping, whitespace normalization. Idempotent.
- `process_file(path: Path, dry_run: bool) → bool`: Normalize a file in place; returns True if file changed.
- CLI: `--txt DIR`, `--dry-run`, `--stats`. Wrapper: `./normalize.sh`.

### `src/llm_correct.py`

- `_haiku(text: str, model: str) → str` (async): Send page text to Claude Haiku for OCR error correction; returns corrected text (fallbacks to original on empty response).
- `_correct_all(bad, txt_dir, pages_dir, model, dry_run)` (async): Process all bad pages — normalize input, call Haiku, write `page-NNN.txt` + `page-NNN.llm` marker, call `merge_pages.merge_one`.
- Idempotency: pages with `generated/text_pages/<stem>/page-NNN.llm` marker are skipped. Marker survives `merge_one` cleanup (only `.txt`/`.png` are deleted).
- CLI: `--threshold N`, `--model MODEL`, `--pages-jsonl FILE`, `--txt DIR`, `--pages-out DIR`, `--dry-run`. Wrapper: `./llm_correct.sh`.

### `src/merge_pages.py`

- `merge_text(original: str, page_updates: dict[int, str]) → str`: Ren funktion — ersätter enstaka sidor (1-indexerat) i en `\f`-separerad textfil. Sidnummer utanför range ignoreras tyst. Testad i `tests/test_merge_pages.py`.
- `find_updates(stem_dir: Path) → dict[int, str]`: Hittar `page-NNN.txt` i en `generated/text_pages/<stem>/`-mapp.
- `merge_one(stem, txt_dir, pages_dir) → bool`: Slå ihop en fil + städa. Returnerar True om något hände (text uppdaterad ELLER artefakter rensade). Idempotent: andra körningen hittar inga `page-NNN.txt` och gör inget.
- Cleanup-policy: efter merge raderas `page-NNN.txt` + `page-NNN.png` för sidor inom range, plus eventuell legacy `generated/text_pages/<stem>.txt`. `page-NNN.json` behålls. Out-of-range-sidor behålls (skydd mot dataförlust om range-info är fel).
- CLI: `--stem <namn>` eller `--all`. Wrapper: `./merge_pages.sh`.

### `src/rag/ask.py`

- `search(table, model, q, top_k) → list[dict]`: Vector search
- `search_hybrid(table, model, q, top_k) → list[dict]`: Vector + FTS with RRF
- `rerank(q, hits, top_n) → list[dict]`: Cross-encoder reranking
- `format_context(hits) → str`: Formats chunks for Claude prompt

System prompt (Swedish): Instructions to answer on archive material, cite with [Nr X, sida Y], admit when material is insufficient.

### `src/webui.py`

Streamlit app with:
- Multiple AI backend selector (Claude via Agent SDK, OpenAI, DeepSeek, custom OpenAI-compatible)
- Reranking toggle + top-K/top-N sliders
- Citation links: click to open original PDF locally (base64-encoded path in query param)
- Hidden iframe prevents main page reload when opening PDFs
- **MCP-läge = chatt**: när "Utredningsläge (MCP)" är på används `st.chat_input`/`st.chat_message`
  och konversationshistorik sparas i `ss.chat_history`. Kontinuitet uppnås genom att fånga
  `session_id` från `ResultMessage` efter varje tur och skicka tillbaka det som
  `ClaudeAgentOptions(resume=...)` på nästa fråga — Claude minns då tidigare frågor och
  tool-resultat. "Ny konversation"-knappen i sidebaren nollställer `chat_history` +
  `mcp_session_id`. RAG-läget använder fortfarande den gamla form-baserade engångs-UI:n.

## Testing

Run tests with pytest. All tests located in `tests/` and assume `src/` is in `sys.path` (set up by `conftest.py`).

Notable: The `tiny_pdf` fixture creates a minimal PDF with pymupdf; skipped gracefully if unavailable.

## Common Gotchas

1. **Dependency Versions**: `sentence-transformers<5` and `transformers<5` are intentionally pinned. Version 5.x breaks cross-encoder loading and AutoProcessor usage.

2. **Tesseract User-Words**: The `swe.user-words` file contains Palme-specific terminology (Q/A markers, case numbers). Auto-generated words go to `swe.user-words.auto`.

3. **Quality Scoring**: A "text-layer" source doesn't guarantee quality—some PDFs have old OCR junk embedded. Sort by score, not by source.

4. **Surya Performance**: Much slower than Tesseract (~30–100 s/page vs ~1 s/page) but higher quality on degraded scans. Only runs on below-threshold pages.

5. **Database Indexing**: FTS requires modern lancedb with tantivy support. If missing, vector-only search is used (hybrid falls back gracefully).

6. **OAuth vs API Key**: `CLAUDE_CODE_OAUTH_TOKEN` counts against Pro/Max subscription hours; `ANTHROPIC_API_KEY` uses API credits. Choose based on your plan.

## Performance Tuning

- **OCR Parallelism**: `./ocr_tesseract.sh --jobs 8 --per-file-jobs 2` for faster multi-core systems
- **Chunking**: Adjust `--chunk-chars` and `--chunk-overlap` in `ingest.sh` (larger chunks = fewer total, faster embedding but less granular search)
- **Search Top-K**: Increase if you want more reranking candidates; `--top-n` controls final context size sent to Claude
- **Quality Threshold**: Lower `--threshold` to re-OCR more aggressively; higher to skip more files
