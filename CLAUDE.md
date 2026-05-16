# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commit och push

Kör **inte** `/cap` automatiskt efter varje ändring — användaren kör det själv när hen är redo. Kör aldrig `git commit`/`git push` direkt heller.

## Dokumentation hålls synkad

När du gör förändringar i projektet ska du **alltid** uppdatera både `CLAUDE.md` och `README.md` i samma commit — om ändringen påverkar något i dem.

- `README.md` — användarvända dokumentationen (svenska)
- `CLAUDE.md` — instruktioner för framtida Claude-sessioner

## Project Overview

**palmemordsarkivet** laddar ner, OCR-processar och gör ~3 700 PDF-dokument (~33 000 sidor) från palmemordsarkivet.se sökbara via RAG + Claude Opus 4.7. Alla kommentarer och docstrings är på svenska.

Pipeline: download → OCR (Tesseract + optional Surya) → detect redactions → normalize → quality scoring → LanceDB ingest → RAG query → Streamlit UI.

## Directory Structure

```
src/
  download.py          # Google Drive PDF downloader, manifest tracking
  quality.py           # OCR quality scoring (0–100 heuristics + optional hunspell)
  ocr_pages.py         # Per-page OCR pipeline (Tesseract/Surya) + redaktionsdetektering
  normalize_text.py    # Rule-based OCR normalization (ligatures, whitespace). Inkrementellt idempotent via stamp-fil.
  llm_correct.py       # LLM post-correction av dåliga OCR-sidor via Claude Haiku
  merge_pages.py       # Slå ihop text_pages/<stem>/page-*.txt → text/<stem>.txt
  build_user_words.py  # Generera Tesseract user-words från OCR-text
  errors_log.py        # Centraliserad felloggning (tab-separerad)
  webui.py             # Streamlit-gränssnitt
  rag/
    ingest.py          # LanceDB vector index builder
    ask.py             # RAG query + Claude integration
tests/                 # pytest (test_quality, test_chunk, test_download, test_merge_pages, test_detect_redactions, test_reingest, test_normalize_text)
*.sh                   # Bash-wrappers (aktiverar .venv, läser API-nycklar, vidarebefordrar flaggor)
tessdata/              # swe_best.traineddata, swe.user-words, tesseract.config
```

Data-kataloger (gitignored):
- `downloaded/files/`, `downloaded/wpu_files/` — PDF:er
- `generated/text/` — OCR-text, `generated/text_pages/` — per-sida-artefakter
- `generated/lancedb/` — vektorindex, `generated/quality.csv` — kvalitetsrapport
- `generated/errors.log` — fellog

## Commands

```bash
# Tests
.venv/bin/pytest tests/

# Workflow
./download.sh                        # Hämta PDF:er från Google Sheet
./ocr.sh                             # Tesseract → normalize → quality → Surya på dåliga sidor
./ocr.sh --skip-redo                 # Bara Tesseract + normalize + quality
./ocr.sh --redo --mode pages         # Surya på specifika sidor (threshold 50)
./quality.sh [--top 30] [--per-page]
./llm_correct.sh [--threshold 60]    # Claude Haiku korrigerar dåliga sidor
./merge_pages.sh --all               # Slå ihop text_pages/ → text/
./ingest.sh [--rebuild] [--reindex-since 2026-05-01]
./ask.sh "fråga" [--hybrid] [--no-rerank]
./web.sh                             # Starta Streamlit

# wpu.nu (valfritt)
./download_wpu.sh && ./merge_wpu.sh
```

Env-variabler: `CLAUDE_CODE_OAUTH_TOKEN` (Pro/Max, räknas mot prenumeration) eller `ANTHROPIC_API_KEY`. Valfritt: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`.

## Non-obvious Design Decisions

**MCP-läge (webui.py)**: Konversationskontinuitet uppnås genom att fånga `session_id` från `ResultMessage` och skicka tillbaka det som `ClaudeAgentOptions(resume=...)` på nästa fråga. "Ny konversation" nollställer `chat_history` + `mcp_session_id`.

**mtime-tracking (ingest.py)**: Varje `.txt`-fil jämförs mot lagrad mtime; nyare fil → re-ingest. Legacy-rader har `mtime=0.0` och re-indexeras inte automatiskt — använd `--reindex-since`.

**Redaktionsdetektering (ocr_pages.py)**: Hittar svarta maskeringsblock i bilder och infogar `[MASKAD]` i texten. På som standard; `--no-detect-redactions` stänger av. `detect_redactions.sh` skriver alltid en `.redact`-markörfil per PDF och förfiltrerar listan innan `xargs` — filer med markör spawnar inga subprocesser alls.

**Per-dokument-cleanup (ocr.sh + Surya)**: Efter att `ocr_pages.py` är klar kör `ocr.sh` automatiskt `merge_pages.merge_one` + `normalize_text.process_file`, raderar `page-NNN.txt`/`.png` men behåller `page-NNN.json` som idempotens-markör.

**Stamp-filbaserad normalisering (normalize_text.py)**: `normalize.sh` skapar `generated/text/.normalize_stamp` efter en lyckad körning. Nästa körning filtrerar bort `.txt`-filer vars mtime ≤ stampens mtime — bara nya/ändrade filer normaliseras. `--rebuild` ignorerar stämpeln. Stämpeln skapas inte vid `--dry-run` eller om det finns fel. Ta bort `.normalize_stamp` manuellt för att tvinga om-normalisering av alla filer.

**Stamp-filbaserad quality-bedömning (quality.py)**: `quality.sh` skapar `generated/.quality_stamp` efter en lyckad körning. Nästa körning laddar befintlig `quality.csv` (och `quality_pages.jsonl` om `--per-page`) och bedömer bara `.txt`-filer med mtime > stampens mtime. Oförändrade filer behåller sina gamla poäng i den sammanslagna outputen. `--rebuild` kör om alla filer. `--limit` skapar ingen stämpel (testläge).

## Common Gotchas

1. **Beroendepinning**: `sentence-transformers<5` och `transformers<5` är avsiktligt pinnade — v5 bryter cross-encoder och AutoProcessor.
2. **text-layer ≠ bra kvalitet**: Vissa PDF:er har gammal OCR-skräp inbäddad. Sortera på score, inte källa.
3. **Surya-prestanda**: ~30–100 s/sida vs ~1 s/sida för Tesseract. Körs bara på sidor under threshold.
4. **FTS kräver tantivy**: Saknas det faller hybrid-sökning tillbaka på vektor-only.
5. **OAuth vs API**: `CLAUDE_CODE_OAUTH_TOKEN` räknas mot Pro/Max-prenumerationen; `ANTHROPIC_API_KEY` drar API-credits.
