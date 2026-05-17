# palmemordsarkivet

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Skript för att ladda ner, OCR-tolka och söka i materialet på
[palmemordsarkivet.se](https://palmemordsarkivet.se) — ett publikt Google Sheet
med 3 762 PDF-filer (~35 000 sidor) som länkas via "Länk till kopia".

## Web-gränssnitt

Efter nedladdning, ocr-scanning så finns det ett web-gränssnitt som man kan ställa frågor om Palme-mordet i.

### RAG (standard)

En fast pipeline: frågan
embedas och matchas mot vektorindexet, de bästa utdragen rerankas, och de
6 mest relevanta skickas som kontext till AI som formulerar svaret med
källhänvisningar. Snabbt och förutsägbart — passar enkla faktafrågor där ett
söksteg räcker.

![Web-gränssnitt — RAG-läge](cross-encoder.png)

### MCP (utredningsläge) 

AI söker *autonomt* via
[Model Context Protocol](https://modelcontextprotocol.io). Istället för en
fast pipeline får Claude tillgång till verktyg (`search_archive`, `get_page`)
som den anropar hur många gånger den vill — provar olika söktermer, följer
upp intressanta träffar och läser hela sidor för mer kontext. Bättre täckning
på komplexa flerstegs-frågor, men långsammare (~1–3 min).

![Web-gränssnitt — MCP-läge](utredningsläge.png)

## Kom igång

```bash
./install.sh           # installera alla beroenden (brew, Python-paket, tessdata)
```

Sätt sedan en API-nyckel för den LLM du vill använda:

```bash
# Anthropic (Claude) — Pro/Max-abonnemang (rekommenderas):
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
# eller API-credits:
export ANTHROPIC_API_KEY=sk-ant-...

# OpenAI (GPT-5 / GPT-4o):
export OPENAI_API_KEY=sk-...

# DeepSeek (V4 / Reasoner):
export DEEPSEEK_API_KEY=sk-...
```

Minst en nyckel krävs. Webgränssnittet sparar valt backend i `generated/llm_config.json` mellan sessioner — se avsnittet [LLM-konfiguration](#llm-konfiguration-generatedllm_configjson) nedan.

Kör sedan pipeline:

```bash
./run_pipeline.sh      # kör alla steg i ett (download → OCR → ingest)
./web.sh               # Starta webgränssnittet och ställ frågor
```

Eller steg för steg:

```bash
./download.sh          # 1. Ladda ner alla PDF:er (3 762 st, tar några timmar)
./download_wpu.sh      # 1b. (valfritt) Ladda ner WPU PDF:er (7 155 st)
./ocr.sh               # 2. OCR → text (Tesseract + Surya på svåra sidor, tar flera timmar)
./ingest.sh            # 3. Bygg vektorindex i LanceDB (kan ta flera timmar)
./web.sh               # 4. Starta webgränssnittet och ställ frågor
```

Varje steg är idempotent — avbryt och fortsätt när som helst.

## State-databas (`generated/db/state.db`)

Alla pipeline-markörer och status lagras i SQLite (`generated/db/state.db`).
Inspektera med t.ex. `sqlite3 generated/db/state.db`. Tabellerna:

- `downloads` — Drive- och wpu-nedladdningar (drive_id, sha1, filename)
- `pdf_files` — per-PDF-status (redaction_checked_at, merged_at, normalized_at, text_mtime)
- `pdf_pages` — per-sida OCR-resultat (text, engine, score)
- `quality` / `quality_pages` — kvalitetspoäng per fil och sida
- `ingest` — vad som indexerats i LanceDB och med vilken text_mtime
- `llm_corrections` — vilka sidor som LLM-korrigerats
- `wpu_decisions` — vilka wpu-stems `merge_wpu` redan fattat beslut för

**Migrering från äldre versioner:** kör `./migrate_to_db.sh` en gång. Skriptet är
idempotent. När pipelinen verifierats mot state.db i några körningar kan
`cleanup_legacy_state.sh` köras för att ta bort gamla filmarkörer (manifest.csv,
stamp-filer, .redact, page-*.json, quality.csv/jsonl, text_wpu/*.done).

**Livscykel:** radera inte `generated/db/state.db` mitt under en pågående
pipeline-körning. Befintliga processer fortsätter skriva mot den unlinkade inoden
medan nya processer skapar en tom db — det ger inkonsekvent state. Om databasen
har raderats, kör `./migrate_to_db.sh` innan nästa körning för att återskapa
state från eventuella kvarvarande legacy-filer.

## Krav

- macOS (testat på Darwin 25), Python 3.11+
- [Homebrew](https://brew.sh)
- Claude Pro/Max-abonnemang (OAuth-token) eller Anthropic API-nyckel

## Vad install.sh gör

`install.sh` sköter allt via Homebrew och pip:

- `ocrmypdf`, `tesseract-lang`, `poppler`, `unpaper`, `hunspell` via brew
- sv_SE-ordlista länkad till `~/Library/Spelling/` (för qualitets-scoring)
- `.venv/` med `pip install -e .[webui]`
- Laddar ner `swe_best.traineddata` (~12 MB) via `setup_tessdata.sh`

Surya-OCR ingår som standard. Hoppa över det (snabbare install) med:

```bash
./install.sh --no-surya
```

> **Beroendepinnar:** `sentence-transformers<5` och `transformers<5` är medvetet pinnade — 5.x-versionerna bryter cross-encoder-laddning och Surya-integrationen.

## Användning

### 1. Ladda ner PDF-filerna

```bash
./download.sh
./download.sh --out files --help
```

- Hämtar Google Sheet som CSV, plockar ut Drive-ID från "Länk till kopia".
- Filnamn blir `<Nr> — <Titel> — <Beställt> — <Upplagt> — <Anmärkning> — <Sidor>.pdf`.
- Korrekt extension via magic-bytes (PDF/JPG/PNG/...).
- Idempotent: hoppa över redan nedladdade. Visar progress + ETA.

För hela arkivet (tar några timmar), kör i bakgrunden:

```bash
nohup ./download.sh > log.txt 2>&1 &
```

#### Komplettera med wpu.nu

[wpu.nu](https://wpu.nu/wiki/Dokument) publicerar en del av samma material men ibland med bättre skanningar. Ladda ner hela wpu-samlingen till en separat katalog:

```bash
./download_wpu.sh          # ladda ner alla PDF:er → downloaded/wpu_files/
./download_wpu.sh --dry-run  # lista utan att ladda ner
```

Wpu-PDF:er får exakt samma OCR-behandling som palme-PDF:er: `ocr.sh` kör `ocr_tesseract.sh` på `downloaded/wpu_files/` också så varje wpu-fil får sin egen `generated/text/<stem>.txt` och `generated/ocr/<stem>.pdf`. Sen kör `merge_wpu.sh` som jämför kvalitetspoäng för matchande dokument-ID och **raderar förlorarens** text- och ocr-filer:

| Jämförelse (margin 5 p) | Utfall |
|---|---|
| wpu vinner | palme-versionens `generated/text/`+`generated/ocr/` raderas |
| palme vinner | wpu-versionens `generated/text/`+`generated/ocr/` raderas |
| inom margin (oavgjort) | båda behålls |
| ingen matchning | wpu står ensam, inget händer |

Surya-steget körs sen mot kvarvarande `generated/text/`-filer och hanterar palme- och wpu-filer identiskt.

```bash
./merge_wpu.sh             # kör om manuellt (parallellt, default cpu_count)
./merge_wpu.sh --dry-run   # visa vad som skulle hända
./merge_wpu.sh --rebuild   # ignorera .done-markeringar
./merge_wpu.sh --jobs 8    # antal parallella processer
```

### 2. OCR till text

#### Rekommenderad workflow: `ocr.sh`

Kör hela OCR-pipelinen i ett enda kommando — Tesseract på allt, kvalitets-
bedömning, Surya på sidor som inte når tröskeln, och slutbedömning:

```bash
./ocr.sh                         # full pipeline (rekommenderas)
./ocr.sh --threshold 60          # mer aggressiv om-OCR
./ocr.sh --skip-redo             # bara Tesseract + bedömning, ingen Surya
./ocr.sh --help
```

Surya-steget hoppas automatiskt över om paketet inte är installerat. Skriptet
är idempotent — kan avbrytas och köras om utan dubbelarbete.

#### Manuell kontroll

Vill du köra stegen individuellt (för felsökning eller delkörningar) anropar
du delarna direkt:

```bash
./ocr_tesseract.sh               # Tesseract på alla nya filer
./quality.sh --per-page          # bedöm + skriv per-sida-poäng till state.db (tabellerna quality/quality_pages)
./ocr.sh --redo --mode pages     # Surya på sidor under tröskeln
./quality.sh                     # uppdaterad bedömning
```

`ocr_tesseract.sh`:s interna logik:
- Snabbkoll först: om PDF:en redan har användbart textlager extraheras det
  direkt utan OCR (kvalitetscheck: alnum-andel, andel korta ord, siffer-i-ord).
- Skräpigt textlager → `ocrmypdf --redo-ocr` (tar bort det och OCR:ar om).
- Inget textlager → `ocrmypdf --skip-text --deskew --clean --rotate-pages` med
  PSM 6, `swe_best.traineddata`, och `tessdata/swe.user-words` (Palme-specifika
  namn, Q/A-markörer, ärendenummer-prefix).
- Parallelliserar över filer med `xargs -P` (default 4 jobb). Idempotent.

Tunables via flaggor (env-vars fungerar fortfarande som fallback):

```bash
./ocr_tesseract.sh --jobs 8 --per-file-jobs 2 --psm 4
./ocr_tesseract.sh --help                          # alla flaggor
JOBS=8 PER_FILE_JOBS=2 PSM=4 ./ocr_tesseract.sh    # bakåtkompatibelt
```

`ocr_tesseract.sh` byter aldrig OCR-engine på egen hand — för Surya på
dåliga sidor använder du `./ocr.sh` ovan eller `./ocr.sh --redo` manuellt.

Vid riktiga fel skrivs `[fel] <namn>` följt av indragen ocrmypdf-logg. Tesseracts
varningar för blanka sidor (`Too few characters. Skipping this page` /
`Error during processing`) är benigna och göms numera — filen blir ändå OCR:ad.

#### Surya för värsta sidorna

Tesseract klarar ~85 % av materialet bra men kämpar på degraderade scans. För
de sidorna ger [Surya](https://github.com/VikParuchuri/surya) (transformer-OCR)
markant högre kvalitet — i stickprov på 50 svåra filer: medelpoäng 60 → 73,
49 av 50 bättre. Priset är fart (~30–100 s/sida på Apple Silicon MPS, mot
~1 s/sida för Tesseract).

Surya körs per sida via `./ocr.sh --redo --mode pages` (default i full pipeline).
Endast sidor med score < threshold OCR:as om, resultatet mergas tillbaka in i
`generated/text/<stem>.txt` per dokument. Se `Per-sida OCR` nedan.

#### Per-sida OCR (`ocr_pages.sh`)

För ännu finare granularitet — om bara enstaka sidor i en fil är dåliga.
Renderar PDF:en sida för sida, OCR:ar varje sida individuellt och skriver
``page-NNN.txt`` + ``page-NNN.json`` (text + score) i en undermapp. Slutsamlar
till ``<stem>.txt`` med ``\f`` som sidbrytare.

```bash
./ocr_pages.sh --in downloaded/files/foo.pdf --out-dir generated/text_pages --engine tesseract
./ocr_pages.sh --in downloaded/files/foo.pdf --out-dir generated/text_pages --engine surya
./ocr_pages.sh --in downloaded/files/foo.pdf --out-dir generated/text_pages --pages 3,7,12
```

Kombo med `./quality.sh --per-page` + `./ocr.sh --redo --mode pages` kör om
bara de sidor som ligger under tröskeln (med Surya som default). Alla skript
har `--help` som listar tillgängliga flaggor; env-vars fungerar fortfarande
som fallback.

#### Auto-byggda user-words (`build_user_words.sh`)

Bygg `tessdata/swe.user-words.auto` från befintliga `text/*.txt`. Filtrerar
mot hunspell sv_SE om installerat, annars freq ≥ 30. Plockas upp automatiskt
av `ocr_tesseract.sh`:

```bash
./build_user_words.sh
```

### 2b. LLM-korrektion av dåliga sidor (valfritt)

> **Obs:** Regelbaserad normalisering (`normalize.sh`) körs numera automatiskt
> av `ocr.sh` — det behöver inte köras separat.

#### LLM-korrektion av dåliga sidor (`llm_correct.sh`)

Skickar sidor med låg kvalitetspoäng till Claude Haiku som rättar
OCR-fel (fellästa tecken, trasiga ord, skräptecken) med svenska
språkets kontext. Kör automatiskt `normalize.sh`:s logik på varje
sida före och efter rättningen.

Förutsätter att per-sida-poäng finns i state.db (byggs av `./quality.sh --per-page`).
Idempotent: sidor som redan korrigerats spåras via `llm_corrections`-tabellen och hoppas över.

```bash
./llm_correct.sh                     # rätta sidor med score < 50
./llm_correct.sh --threshold 60      # striktare tröskel
./llm_correct.sh --dry-run           # visa vad som skulle rättas
./llm_correct.sh --help              # alla flaggor
```

Kostnad: Claude Haiku är billig (~$0,25/M tokens). En typisk OCR-sida
är ~600–1 000 tokens — om 5 % av ~47 000 sidor är dåliga är totalkostnaden
~$25–40 för hela arkivet.

Kör sedan `./ingest.sh` för att re-indexera ändrade filer (mtime-detektering
fångar dem automatiskt).

### 3. Indexera i vektor-DB

```bash
./ingest.sh                       # nya + ändrade filer (mtime-detektering)
./ingest.sh --rebuild             # börja om från noll
./ingest.sh --reindex-since 2026-05-01  # tvinga om för gamla filer modifierade efter datum
./ingest.sh --help                # alla flaggor
```

**Per-sida-merge av Surya-omkörningar:** `ocr.sh --redo --mode pages` skriver
per-sida-text till `generated/text_pages/<stem>/page-NNN.txt`. Direkt efter att ett
dokument är klart slår `ocr.sh` automatiskt ihop dessa sidor in i
`generated/text/<stem>.txt` (en sida i taget, behåller övriga sidor) och raderar
`page-NNN.txt` + `page-NNN.png`. Kvar i `generated/text_pages/<stem>/` blir bara
lättviktiga `page-NNN.json` (kvalitetsmetadata) som fungerar som idempotens-
markör för nya körningar av `ocr.sh --redo --mode pages`.

Resultat: ingest fångar ändringarna via mtime, och `generated/text_pages/` ackumulerar
inte stort innehåll.

För befintliga `generated/text_pages`-mappar (som inte mergades/städades automatiskt)
finns en engångsåtgärd:

```bash
./merge_pages.sh --all            # slå ihop + städa alla generated/text_pages/<stem>/
./merge_pages.sh --stem "1 — PM …"  # bara en specifik fil
```

Båda kommandona är idempotenta — kör om utan oro, de gör bara något om det
finns nya `page-NNN.txt` att slå in.

- Chunkar `text/*.txt` (800 tecken med 150 teckens överlapp, bryter på radslut).
- Embeddar lokalt med `intfloat/multilingual-e5-large` (svenska duger bra).
- Lagrar i lokal LanceDB med metadata (Nr, Titel, Sida, Anmärkning) **och `mtime`**
  så att ändrade `.txt`-filer (t.ex. efter `ocr.sh --redo`) detekteras automatiskt
  och re-indexeras vid nästa körning.
- För filer som indexerades innan mtime-tracking infördes saknas mtime i tabellen
  (lagras som `0.0`). Använd `--reindex-since <tid>` för att tvinga re-index av
  legacy-rader vars `.txt` modifierats efter en känd tidpunkt — t.ex. när du
  precis kört en re-OCR-våg.
- Filtrerar bort OCR-skräp (chunks med <55 % alfanumeriska tecken).
- Idempotent. Första körningen laddar ned ~1.1 GB modell.

### 4. Ställ frågor

```bash
./web.sh   # Starta Streamlit-webgränssnittet
```

OAuth-token genereras med `claude setup-token` (engångsåtgärd).

#### RAG-läge (standard)

Klassisk *retrieval-augmented generation*: en fast pipeline i tre steg.

1. **Vektorsökning** — frågan embedas lokalt med `intfloat/multilingual-e5-large`
   och matchas mot LanceDB-indexet (top-20 kandidater).
2. **Hybrid + reranking (valfritt)** — `--hybrid` kombinerar vektor och BM25 (FTS)
   med *Reciprocal Rank Fusion* (k=60). Sedan omrankar
   `BAAI/bge-reranker-v2-m3` resultaten och plockar ut topp-6.
3. **Claude svarar** — de 6 utdragen skickas som kontext till Claude Opus 4.7
   (adaptive thinking). Svaret innehåller källhänvisningar `[Nr X, sida Y]`.

Snabbt och förutsägbart. Passar enkla faktafrågor där ett enstaka söksteg räcker.

#### MCP-läge (`--mcp`)

I utredningsläget söker Claude *autonomt* via
[Model Context Protocol](https://modelcontextprotocol.io). Istället för en fast
pipeline startar systemet en MCP-server (`src/rag/mcp_server.py`) som subprocess
och låter Claude anropa dessa verktyg hur många gånger det vill:

| Verktyg | Vad |
|---|---|
| `search_archive` | Vektor- eller hybridsökning med valfri reranking; returnerar utdrag med källinfo |
| `get_page` | Läser råtexten från en specifik sida för att få mer kontext kring en träff |

Claude väljer själv söktermer, kan söka flera gånger med olika fraser, och kan
följa upp intressanta träffar med `get_page`. Det ger markant bättre täckning på
komplexa flerstegs-frågor — till priset av längre svarstid (upp till 10 anrop,
~1–3 min beroende på fråga).

```text
Enkla faktafrågor  → RAG-läge (snabbt, deterministiskt)
Komplexa utredningsfrågor  → MCP-läge (--mcp, autonomt, bättre täckning)
```

Webgränssnittet (Claude-backend) har en "Utredningsläge (MCP)"-toggle i sidebaren.
I MCP-läget får du en chatt där Claude minns tidigare frågor i konversationen
(implementerat via Claude Agent SDK:s `resume`-fält — `session_id` från senaste
svaret skickas med nästa fråga). Sidebar-knappen "Ny konversation" nollställer
historiken och startar en ny session.

#### Webgränssnittet

Stödjer flera AI-backends via en väljare i sidebaren:

- **Claude Opus 4.7** (default) — via `claude-agent-sdk` med adaptive thinking,
  kräver `CLAUDE_CODE_OAUTH_TOKEN` eller `ANTHROPIC_API_KEY`. Stödjer MCP-läge.
- **OpenAI GPT-5 / GPT-4o** — kräver `OPENAI_API_KEY`.
- **DeepSeek V4 / Reasoner** — kräver `DEEPSEEK_API_KEY`
  (`deepseek-chat` är V4-routern, `deepseek-reasoner` är thinking-modellen).
- **OpenAI-kompatibel (custom)** — pekar på vilken `/v1`-endpoint som helst
  (Ollama, LM Studio, llama.cpp, vLLM, fjärr-OpenAI-providers...). URL,
  modellnamn och valfri API-nyckel konfigureras i sidebaren.

Lägg till `openai` om du vill använda andra backends än Claude:

```bash
.venv/bin/pip install openai
```

För lokal Ollama:

```bash
brew install ollama
ollama serve &
ollama pull llama3.1:8b
```

Citat i svaret renderas som små inline-knappar — klick öppnar original-PDF:en
lokalt (via `open`) i en gömd iframe så huvudsidan inte laddas om.

## LLM-konfiguration (`generated/llm_config.json`)

Webgränssnittet sparar valt backend automatiskt i `generated/llm_config.json`
när du byter provider i sidebaren. Filen laddas vid nästa start så du slipper
välja om. Formatet:

```json
{
  "backend_name": "Claude",
  "provider": "claude",
  "model": "claude-opus-4-7",
  "base_url": ""
}
```

| Fält | Möjliga värden |
|---|---|
| `provider` | `claude`, `openai`, `deepseek`, `openai_compatible` |
| `model` | Modellnamn, t.ex. `claude-opus-4-7`, `gpt-4o`, `deepseek-chat`, `deepseek-reasoner` |
| `base_url` | Tomt för molntjänster; URL för lokal endpoint (`http://localhost:11434/v1` för Ollama) |
| `backend_name` | Visningsnamn i gränssnittet (valfritt) |

Filen skapas automatiskt — det finns inget behov av att redigera den för hand.
Ta bort den för att återgå till standardvalet (Claude Opus 4.7).

API-nycklar läses alltid från miljövariabler och lagras **aldrig** i filen:

```bash
export ANTHROPIC_API_KEY=sk-ant-...       # Claude
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...  # Claude Pro/Max (räknas mot prenumeration)
export OPENAI_API_KEY=sk-...              # OpenAI
export DEEPSEEK_API_KEY=sk-...           # DeepSeek
```

## Filer

| Fil | Vad |
|---|---|
| `run_pipeline.sh` | Kör hela pipelinen i ett kommando: download → OCR → ingest (flaggor: `--skip-wpu`, `--skip-redo`, `--with-llm`, `--rebuild-index`, `--jobs N`, `--test N`) |
| `download.sh` → `src/download.py` | Hämta PDF:er från Drive |
| `download_wpu.sh` → `src/download_wpu.py` | Ladda ner alla PDF:er från wpu.nu → `downloaded/wpu_files/` |
| `merge_wpu.sh` → `src/merge_wpu.py` | Jämför wpu- och palme-text per fil, behåll bäst kvalitet |
| `setup_tessdata.sh` | Sätt upp projekt-lokal `tessdata/` med swe_best |
| `ocr.sh` | Full OCR-pipeline (Tesseract → kvalitet → Surya på dåliga sidor); `--redo` kör om dåliga filer/sidor |
| `ocr_tesseract.sh` | Bara Tesseract-steget (textextraktion + ocrmypdf) |
| `ocr_pages.sh` → `src/ocr_pages.py` | Per-sida OCR (Tesseract/Surya) med sidor i `\f`-separerad txt |
| `merge_pages.sh` → `src/merge_pages.py` | Slå ihop `generated/text_pages/<stem>/page-*.txt` in i `generated/text/<stem>.txt` |
| `build_user_words.sh` → `src/build_user_words.py` | Bygg `tessdata/swe.user-words.auto` från `generated/text/*.txt` |
| `quality.sh` → `src/quality.py` | Heuristisk kvalitetsbedömning av `generated/text/*.txt` (`--per-page` finns) |
| `normalize.sh` → `src/normalize_text.py` | Regelbaserad OCR-normalisering (körs automatiskt av `ocr.sh`) |
| `llm_correct.sh` → `src/llm_correct.py` | LLM-korrektion av dåliga OCR-sidor via Claude Haiku |
| `detect_redactions.sh` → `src/ocr_pages.py` | Kör redaktionsdetektering på befintliga text/OCR-par |
| `ingest.sh` → `src/rag/ingest.py` | Bygg vektorindex (LanceDB + BM25 FTS) |
| `src/rag/ask.py` | Frågefunktioner — RAG-läge och MCP-läge (importeras av webui och mcp_server) |
| `src/rag/mcp_server.py` | MCP-server med `search_archive` och `get_page` (startas av ask.py/webui.py) |
| `generated/llm_config.json` | Sparad LLM-konfiguration (backend, modell, URL) — se ovan |
| `src/webui.py` | Streamlit-webgränssnitt för frågor (RAG + MCP-toggle) |
| `web.sh` | Wrapper för Streamlit-servern |
| `migrate_to_db.sh` → `src/migrate_to_db.py` | Engångsmigrering: legacy filmarkörer → `generated/db/state.db` |
| `cleanup_legacy_state.sh` | Tar bort gamla filmarkörer efter migrering (manifest.csv, stamp-filer, .redact, page-*.json, quality.csv/jsonl) |
| `src/db.py` | SQLite-state: schema + CRUD + delta-queries (importeras av övriga skript) |
| `tessdata/swe.user-words` | Palme-specifika ord (committat) |
| `tessdata/tesseract.config` | `preserve_interword_spaces 1` (committat) |

### Bonus: kvalitetskoll

```bash
./quality.sh --top 30      # bedöm och visa värsta 30
./quality.sh --rebuild     # tvinga om-bedömning av alla filer
```

Körs inkrementellt — bara filer vars `text_mtime` är nyare än `scored_at` i
`pdf_files`-tabellen bedöms om. `--rebuild` ignorerar tidsstämplarna och kör om
allt.

Skriver poäng 0–100 per fil till `quality`-tabellen i state.db (sorterat värst först) baserat på
junk-tecken-andel, andel 1–2-tecken-ord, ihopklistrade ord, siffror inuti ord
och vokal/konsonant-balans. Markerar källa `text-layer` (originalet hade text)
vs `ocr` (Tesseract).

Viktig insikt: `text-layer` betyder inte automatiskt "bra" — vissa PDF:er har
gammalt OCR-skräp inbäddat fastän originalbilden är fullt läsbar. Sortera
efter `score`, inte efter källa. För dessa: kör `./ocr.sh --redo --mode files`
som anropar `ocrmypdf --redo-ocr` (tar bort det dåliga textlagret och OCR:ar
om från bilden). Default tar med både `text-layer`- och `ocr`-källor. Tröskel
via flagga: `./ocr.sh --redo --mode files --threshold 70` (eller env
`THRESHOLD=70`). Begränsa till en källa: `./ocr.sh --redo --mode files
--source text-layer`. `./ocr.sh --help` listar alla flaggor.

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

## Tester

```bash
.venv/bin/pip install pytest
.venv/bin/pytest tests/
```

Testerna täcker: `score_text` (quality), `chunk_text` (ingest), `extract_drive_id`/`sniff_extension` (download),
`detect_redactions_image` (ocr_pages), `merge_one` (merge_pages), `merge_wpu` (merge_wpu),
LLM-korrektionslogiken (llm_correct) och re-ingest-flödet (ingest).
Fixturen som genererar en mini-PDF med pymupdf skipas gracefully om pymupdf inte är installerat.

## Felloggning

Skript skriver tab-separerade rader till `generated/errors.log`:
``ISO8601\tcomponent\titem\tmessage``. Python-skript via `errors_log.log_error`,
bash via `>> "$ROOT/generated/errors.log"`. Append-only, idempotent.

## Datafiler (gitignorerade)

```
downloaded/   — nedladdade PDF:er (files/, wpu_files/)
generated/    — allt pipeline-genererat (text/, ocr/, lancedb/, db/state.db, errors.log, …)
tessdata/*.traineddata  — laddas av setup_tessdata.sh
```

Åter-skapas helt av skripten — ta bort katalogerna och kör om.

## Starta om

Alla fyra steg är idempotenta — kör om utan oro. De hoppar över redan färdigt
arbete (download: `downloaded/files/<namn>.pdf` finns; ocr: `generated/text/<namn>.txt` finns;
ingest: `source` redan i tabellen *och* `.txt`-filens mtime är ≤ den lagrade).
Avbrutna körningar fortsätter där de slutade. När en `.txt` skrivs om av t.ex.
`ocr.sh --redo` upptäcker `ingest.sh` det automatiskt och re-indexerar bara den
filen (delete + add).

## Licens

Koden är licensierad under [MIT](LICENSE). Materialet i arkivet ägs av sina
respektive upphovsmän och berörs inte av denna licens.
