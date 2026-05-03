# palmemordsarkivet

Skript för att ladda ner, OCR-tolka och söka i materialet på
[palmemordsarkivet.se](https://palmemordsarkivet.se) — ett publikt Google Sheet
med drygt 3 700 PDF-filer (~33 000 sidor) som länkas via "Länk till kopia".

Pipelinen i fyra steg:

```
download.py  →  files/        ocr.sh  →  ocr/  +  text/        rag/ingest.py  →  rag/lancedb/        rag/ask.py
   PDF                          sökbar PDF + .txt                  vektorindex                          fråga → svar
```

## Krav

- macOS (testat på Darwin 25), Python 3.11+
- Homebrew för OCR-verktygen
- Anthropic API-nyckel för `rag/ask.py`

## Installation

```bash
git clone <repo-url>
cd palmemordsarkivet

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install gdown requests sentence-transformers lancedb pyarrow anthropic

brew install ocrmypdf tesseract-lang poppler unpaper
```

## Användning

### 1. Ladda ner PDF-filerna

```bash
.venv/bin/python download.py files
```

- Hämtar Google Sheet som CSV, plockar ut Drive-ID från "Länk till kopia".
- Filnamn blir `<Nr> — <Titel> — <Beställt> — <Upplagt> — <Anmärkning> — <Sidor>.pdf`.
- Korrekt extension via magic-bytes (PDF/JPG/PNG/...).
- Idempotent: hoppa över redan nedladdade. Visar progress + ETA.

För hela arkivet (tar några timmar), kör i bakgrunden:

```bash
nohup .venv/bin/python download.py files > log.txt 2>&1 &
```

### 2. OCR till text

```bash
./ocr.sh
```

- Snabbkoll först: om PDF:en redan har textlager extraheras det direkt utan OCR.
- Annars `ocrmypdf -l swe --rotate-pages --deskew --clean` → sökbar PDF + `.txt`.
- Parallelliserar över filer med `xargs -P` (default 4 jobb).
- Idempotent.

Tunables via env: `JOBS=8 PER_FILE_JOBS=2 ./ocr.sh`.

### 3. Indexera i vektor-DB

```bash
.venv/bin/python rag/ingest.py
```

- Chunkar `text/*.txt` (800 tecken med 150 teckens överlapp, bryter på radslut).
- Embeddar lokalt med `intfloat/multilingual-e5-large` (svenska duger bra).
- Lagrar i lokal LanceDB med metadata (Nr, Titel, Sida, Anmärkning).
- Filtrerar bort OCR-skräp (chunks med <55 % alfanumeriska tecken).
- Idempotent. Första körningen laddar ned ~1.1 GB modell.

### 4. Ställ frågor

```bash
export ANTHROPIC_API_KEY=sk-...

.venv/bin/python rag/ask.py "Vad sa Annett Kohut om kvällen 28 februari?"
.venv/bin/python rag/ask.py --rerank "..."   # bättre precision (laddar ned 568 MB modell)
.venv/bin/python rag/ask.py                  # interaktiv repl
```

Hämtar top-20 chunks från vektor-DB, ev. omrankar med `BAAI/bge-reranker-v2-m3`,
skickar topp-6 till Claude Opus 4.7 (adaptive thinking) som svarar på svenska
med källhänvisningar `[Nr X, sida Y]`. Säger "framgår inte av materialet" hellre
än att gissa.

## Filer

| Fil | Vad |
|---|---|
| `download.py` | Hämta PDF:er från Drive |
| `ocr.sh` | OCR + textextraktion |
| `rag/ingest.py` | Bygg vektorindex |
| `rag/ask.py` | Frågefronten |

## Datafiler (gitignorerade)

`files/`, `ocr/`, `text/`, `rag/lancedb/` — åter-skapas helt av skripten.

## Licens

Skripten är personliga arbetsverktyg, ingen explicit licens. Materialet i
arkivet ägs av sina respektive upphovsmän.
