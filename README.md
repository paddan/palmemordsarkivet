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
- Homebrew för OCR-verktygen och `claude` CLI
- Claude Pro/Max-abonnemang (OAuth-token) eller Anthropic API-nyckel för `rag/ask.py`

## Installation

```bash
git clone git@github.com:paddan/palmemordsarkivet.git
cd palmemordsarkivet

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install gdown requests sentence-transformers lancedb pyarrow claude-agent-sdk

brew install ocrmypdf tesseract-lang poppler unpaper claude-code
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

Lägen:

```bash
./ocr.sh                       # normal: OCR:a sidor utan textlager
REDO_TEXT_LAYER=1 ./ocr.sh     # kör om OCR på alla PDF:er som har textlager
                               # (för PDF:er med inbäddat OCR-skräp)
```

Vid riktiga fel skrivs `[fel] <namn>` följt av indragen ocrmypdf-logg. Tesseracts
varningar för blanka sidor (`Too few characters. Skipping this page` /
`Error during processing`) är benigna och göms numera — filen blir ändå OCR:ad.

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
# Pro/Max-abonnemang (räknas mot abonnemangets timgränser):
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# Eller API-credits:
# export ANTHROPIC_API_KEY=sk-ant-...

.venv/bin/python rag/ask.py "Vad sa Annett Kohut om kvällen 28 februari?"
.venv/bin/python rag/ask.py --rerank "..."   # bättre precision (laddar ned 568 MB modell)
.venv/bin/python rag/ask.py                  # interaktiv repl
```

Hämtar top-20 chunks från vektor-DB, ev. omrankar med `BAAI/bge-reranker-v2-m3`,
skickar topp-6 till Claude Opus 4.7 (adaptive thinking) via Claude Agent SDK.
Svarar på svenska med källhänvisningar `[Nr X, sida Y]`. Säger
"framgår inte av materialet" hellre än att gissa.

OAuth-token genereras med `claude setup-token` (engångsåtgärd).

## Filer

| Fil | Vad |
|---|---|
| `download.py` | Hämta PDF:er från Drive |
| `ocr.sh` | OCR + textextraktion |
| `quality.py` | Heuristisk kvalitetsbedömning av `text/*.txt` |
| `redo_ocr.sh` | Kör om OCR med `--redo-ocr` på filer med dåligt textlager |
| `rag/ingest.py` | Bygg vektorindex |
| `rag/ask.py` | Frågefronten |

### Bonus: kvalitetskoll

```bash
.venv/bin/python quality.py --top 30
```

Skriver `quality.csv` (sorterat värst först) med poäng 0–100 per fil baserat på
junk-tecken-andel, andel 1–2-tecken-ord, ihopklistrade ord, siffror inuti ord
och vokal/konsonant-balans. Markerar källa `text-layer` (originalet hade text)
vs `ocr` (Tesseract).

Viktig insikt: `text-layer` betyder inte automatiskt "bra" — vissa PDF:er har
gammalt OCR-skräp inbäddat fastän originalbilden är fullt läsbar. Sortera
efter `score`, inte efter källa. För dessa: kör `./redo_ocr.sh` som anropar
`ocrmypdf --redo-ocr` (tar bort det dåliga textlagret och OCR:ar om från
bilden). Tröskel via env: `THRESHOLD=70 ./redo_ocr.sh`. Inkludera även
dåliga Tesseract-filer: `SOURCE=any ./redo_ocr.sh`.

Valfritt: installera hunspell + svensk ordlista så fylls `pct_swe`-kolumnen i:

```bash
brew install hunspell
mkdir -p ~/Library/Spelling
curl -L -o ~/Library/Spelling/sv_SE.aff \
  https://raw.githubusercontent.com/LibreOffice/dictionaries/master/sv_SE/sv_SE.aff
curl -L -o ~/Library/Spelling/sv_SE.dic \
  https://raw.githubusercontent.com/LibreOffice/dictionaries/master/sv_SE/sv_SE.dic
echo "katt hus blabla" | hunspell -d sv_SE -l   # ska skriva ut "blabla"
```

## Datafiler (gitignorerade)

`files/`, `ocr/`, `text/`, `rag/lancedb/` — åter-skapas helt av skripten.

## Starta om

Alla fyra steg är idempotenta — kör om utan oro. De hoppar över redan färdigt
arbete (download: `files/<namn>.pdf` finns; ocr: `text/<namn>.txt` finns;
ingest: `source` redan i tabellen). Avbrutna körningar fortsätter där de slutade.

## Licens

Skripten är personliga arbetsverktyg, ingen explicit licens. Materialet i
arkivet ägs av sina respektive upphovsmän.
