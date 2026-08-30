# Teknisk referens

[← Översikt](../README.md) · [Kom igång](kom-igang.md)

Detaljerad dokumentation av alla pipeline-steg, flaggor och delsystem. För en
snabbstart, se [Kom igång](kom-igang.md).

## State-databas (`generated/db/state.db`)

Alla pipeline-markörer och status lagras i SQLite (`generated/db/state.db`).
Inspektera med t.ex. `sqlite3 generated/db/state.db`. Tabellerna:

- `downloads` — palmemordsarkivet- och wpu-nedladdningar (Drive-ID eller URL, sha1 där det finns, filename, bytes)
- `pdf_files` — per-PDF-status (redaction_checked_at, merged_at, normalized_at, text_mtime, tesseract_done_at, tesseract_failed, tesseract_blacklisted_at, surya_failed_at)
- `pdf_pages` — per-sida OCR-resultat (text, engine, score)
- `quality` / `quality_pages` — kvalitetspoäng per fil och sida
- `ingest` — vad som indexerats i LanceDB och med vilken text_mtime
- `llm_corrections` — vilka sidor som LLM-korrigerats
- `wpu_decisions` — vilka wpu-stems `merge_wpu` redan fattat beslut för
- `doc_entities` — extraherade entiteter/relationer per sida (kunskapsgrafen)
- `casebook_entries` — sparade fråga/svar-spår från webgränssnittets utredningspärm
- `source_bookmarks` — bokmärkta källor från källlistorna i webgränssnittet
- `source_annotations` — utredarens fritextanteckningar knutna till en källa/sida (flera per källa tillåts)
- `map_places` — platskatalog för Karta-flikens snabbval
- `map_observations` — källhänvisade observationer med person, koordinat, tid (`HH:MM`), osäkerhet, `nr`, `sida` och notering; `src/karta.py` tolkar tiden mot basdatum `1986-02-28` när `TimestampedGeoJson` byggs
- `map_observation_candidates` — granskningskö för LLM-extraherade kartförslag. Raderna har `status` (`pending`, `approved`, `rejected`) och blir inte publicerade kartobservationer förrän `approve_map_observation_candidate` skapar en rad i `map_observations`
- `map_observation_extractions` — per-sida-markör för kartobservations-extraktionen. Även sidor där LLM:en hittar noll kandidater markeras här så omkörningar inte skickar samma tomma sida igen

Schemat är versionsstyrt i `src/db.py`. `init_schema(conn)` skapar en färsk
databas direkt, migrerar äldre databaser via pending migrations och skriver
aktuell version både till tabellen `schema_version` och `PRAGMA user_version`.
Om databasen har högre version än koden stödjer avbryts initieringen med fel,
så äldre kod inte tyst skriver mot ett nyare schema.

**Livscykel:** radera inte `generated/db/state.db` mitt under en pågående
pipeline-körning — befintliga processer fortsätter skriva mot den unlinkade inoden
medan nya processer skapar en tom db, vilket ger inkonsekvent state.
Om databasen däremot saknas när `scripts/ingest.py` startar identifieras befintliga
LanceDB-källor och ersätts i stället för att dupliceras.

## Vad install.py gör

`scripts/install.py` sköter pipeline- och webgränssnittsberoendena via Homebrew och pip:

- `ocrmypdf`, `tesseract-lang`, `poppler`, `unpaper`, `hunspell` via brew
- sv_SE-ordlista länkad till `~/Library/Spelling/` (för qualitets-scoring)
- `.venv/` med `pip install -e .[web]` (bl.a. Streamlit, `folium` och `streamlit-folium`)
- med `--dev`: `.venv/` med `pip install -e '.[dev,web]'` så `.venv/bin/python scripts/test.py --static` även kan köra `ruff` och `mypy`
- Laddar ner `swe_best.traineddata` (~12 MB) via `scripts/setup_tessdata.py`

Surya-OCR ingår som standard. Hoppa över det (snabbare install) med:

```bash
.venv/bin/python scripts/install.py --no-surya
```

För utveckling/tester:

```bash
.venv/bin/python scripts/install.py --dev
```

> **Beroendepinnar:** `sentence-transformers<5` och `transformers<5` är medvetet pinnade — 5.x-versionerna bryter cross-encoder-laddning och Surya-integrationen.

## Användning

### 1. Ladda ner PDF-filerna

```bash
.venv/bin/python scripts/download.py
.venv/bin/python scripts/download.py --out files --help
```

- Hämtar Google Sheet som CSV, plockar ut Drive-ID från "Länk till kopia".
- Filnamn blir `<Nr> — <Titel> — <Beställt> — <Upplagt> — <Anmärkning> — <Sidor>.pdf`.
- Korrekt extension via magic-bytes (PDF/JPG/PNG/...).
- Idempotent: hoppa över redan nedladdade. Visar progress + ETA.

För hela arkivet (tar några timmar), kör i bakgrunden:

```bash
nohup .venv/bin/python scripts/download.py > log.txt 2>&1 &
```

#### Komplettera med wpu.nu

[wpu.nu](https://wpu.nu/wiki/Dokument) publicerar en del av samma material men ibland med bättre skanningar. Ladda ner hela wpu-samlingen till en separat katalog:

```bash
.venv/bin/python scripts/download_wpu.py          # ladda ner alla PDF:er → downloaded/wpu_files/
.venv/bin/python scripts/download_wpu.py --dry-run  # lista utan att ladda ner
```

Wpu-PDF:er får exakt samma OCR-behandling som palme-PDF:er: `scripts/ocr.py` kör `scripts/ocr_tesseract.py` på `downloaded/wpu_files/` också så varje wpu-fil får sin egen `generated/text/<stem>.txt` och `generated/ocr/<stem>.pdf`. Om Tesseract inte får fram text försöker `scripts/ocr.py` Surya som helfils-fallback innan wpu-merge. Sen kör `scripts/merge_wpu.py` som jämför kvalitetspoäng för matchande dokument-ID och **raderar förlorarens** text- och ocr-filer:

| Jämförelse (margin 5 p) | Utfall |
|---|---|
| wpu vinner | palme-versionens `generated/text/`+`generated/ocr/` raderas |
| palme vinner | wpu-versionens `generated/text/`+`generated/ocr/` raderas |
| inom margin (oavgjort) | båda behålls |
| ingen matchning | wpu står ensam, inget händer |

Om en wpu-PDF saknar text efter både Tesseract och Surya (`surya_failed_at` satt)
hoppar `scripts/merge_wpu.py` över den tyst; vanliga saknade textfiler varnas
fortfarande som resume-problem.

Surya-steget för låga kvalitetspoäng körs sen mot kvarvarande
`generated/text/`-filer och hanterar palme- och wpu-filer identiskt.

```bash
.venv/bin/python scripts/merge_wpu.py             # kör om manuellt (parallellt, default cpu_count)
.venv/bin/python scripts/merge_wpu.py --dry-run   # visa vad som skulle hända
.venv/bin/python scripts/merge_wpu.py --rebuild   # ignorera wpu_decisions-tabellen i state.db
.venv/bin/python scripts/merge_wpu.py --jobs 8    # antal parallella processer
```

### 2. OCR till text

#### Rekommenderad workflow: `scripts/ocr.py`

Kör hela OCR-pipelinen i ett enda kommando — Tesseract på allt, Surya-fallback
för filer där Tesseract inte fick fram text, kvalitetsbedömning, Surya på sidor
som inte når tröskeln, och slutbedömning:

```bash
.venv/bin/python scripts/ocr.py                         # full pipeline (rekommenderas)
.venv/bin/python scripts/ocr.py --threshold 60          # mer aggressiv om-OCR
.venv/bin/python scripts/ocr.py --skip-redo             # bara Tesseract + bedömning, ingen Surya
.venv/bin/python scripts/ocr.py --fallback-failed       # Surya-fallback för Tesseract-fel i defaultkatalogen
.venv/bin/python scripts/ocr.py --fallback-failed --in downloaded/wpu_files
.venv/bin/python scripts/ocr.py --help
```

Surya-stegen hoppas automatiskt över om paketet inte är installerat. Skriptet
är idempotent — kan avbrytas och köras om utan dubbelarbete.

#### Manuell kontroll

Vill du köra stegen individuellt (för felsökning eller delkörningar) anropar
du delarna direkt:

```bash
.venv/bin/python scripts/ocr_tesseract.py               # Tesseract på alla nya filer
.venv/bin/python scripts/quality.py --per-page          # bedöm + skriv per-sida-poäng till state.db (tabellerna quality/quality_pages)
.venv/bin/python scripts/ocr.py --redo --mode pages     # Surya på sidor under tröskeln
.venv/bin/python scripts/quality.py --per-page          # uppdaterad fil- och per-sida-bedömning
```

```bash
.venv/bin/python scripts/ocr_tesseract.py --jobs 8 --per-file-jobs 2 --psm 4
.venv/bin/python scripts/ocr_tesseract.py --retry-failed       # försök misslyckade filer igen
.venv/bin/python scripts/ocr_tesseract.py --retry-blacklist    # försök även Tesseract-blacklistade filer
.venv/bin/python scripts/ocr_tesseract.py --help
```

Filer som upprepat misslyckas i Tesseract (t.ex. korrupt JPEG inuti PDF:en,
eller PDF:er som ocrmypdf inte kan hantera) markeras med
`tesseract_blacklisted_at` i `pdf_files`. Det är bara en Tesseract-spärr:
`scripts/ocr.py` försöker fortfarande Surya-fallback om `surya_failed_at` är tomt.
Först när även Surya-fallback inte producerar text sätts `surya_failed_at`.
`--retry-failed` tar inte in Tesseract-blacklistade filer; `--retry-blacklist`
nollställer Tesseract- och Surya-felstatus så filerna kan försökas separat igen.
Vill du bara ge befintliga Tesseract-fel en Surya-chans utan hela pipelinen kör
du `.venv/bin/python scripts/ocr.py --fallback-failed`; lägg till `--in downloaded/wpu_files` för en
separat WPU-körning.

#### Surya för värsta sidorna

Tesseract klarar ~85 % av materialet bra men kämpar på degraderade scans. För
de sidorna ger [Surya](https://github.com/VikParuchuri/surya) (transformer-OCR)
markant högre kvalitet — i stickprov på 50 svåra filer: medelpoäng 60 → 73,
49 av 50 bättre. Priset är fart (~30–100 s/sida på Apple Silicon MPS, mot
~1 s/sida för Tesseract).

Surya används på två ställen i full pipeline: först som helfils-fallback när
Tesseract inte producerade någon text alls, och senare per sida via
`.venv/bin/python scripts/ocr.py --redo --mode pages`. I per-sida-läget OCR:as bara sidor med score <
threshold om, och resultatet mergas tillbaka in i `generated/text/<stem>.txt`
per dokument. Se `Per-sida OCR` nedan.

#### Per-sida OCR (`scripts/ocr_pages.py`)

Renderar PDF:en sida för sida och lagrar OCR-text + metadata i `pdf_pages`-
tabellen i state.db. Kombination med `.venv/bin/python scripts/quality.py --per-page` +
`.venv/bin/python scripts/ocr.py --redo --mode pages` kör om bara sidor under tröskeln med Surya.

```bash
.venv/bin/python scripts/ocr_pages.py --in downloaded/files/foo.pdf --out-dir generated/text_pages --engine surya
.venv/bin/python scripts/ocr_pages.py --help
```

#### Auto-byggda user-words (`scripts/build_user_words.py`)

Bygg `tessdata/swe.user-words.auto` från befintliga `text/*.txt`. Filtrerar
mot hunspell sv_SE om installerat, annars freq ≥ 30. Plockas upp automatiskt
av `scripts/ocr_tesseract.py`:

```bash
.venv/bin/python scripts/build_user_words.py
```

### 2b. LLM-korrektion av dåliga sidor (valfritt)

> **Obs:** Regelbaserad normalisering (`scripts/normalize.py`) körs numera automatiskt
> av `scripts/ocr.py` — det behöver inte köras separat.

#### LLM-korrektion av dåliga sidor (`scripts/llm_correct.py`)

Skickar sidor med låg kvalitetspoäng till Claude Haiku som rättar
OCR-fel (fellästa tecken, trasiga ord, skräptecken) med svenska
språkets kontext. Kör automatiskt `scripts/normalize.py`:s logik på varje
sida före och efter rättningen.

Förutsätter att per-sida-poäng finns i state.db (byggs av `.venv/bin/python scripts/quality.py --per-page`).
Idempotent: sidor som redan korrigerats spåras via `llm_corrections`-tabellen och hoppas över.

```bash
.venv/bin/python scripts/llm_correct.py                     # rätta sidor med score < 50
.venv/bin/python scripts/llm_correct.py --threshold 60      # striktare tröskel
.venv/bin/python scripts/llm_correct.py --jobs 8            # 8 parallella LLM-anrop (default 4)
.venv/bin/python scripts/llm_correct.py --dry-run           # visa vad som skulle rättas
.venv/bin/python scripts/llm_correct.py --help              # alla flaggor
```

Sidorna är oberoende, så flera rättas samtidigt via en delad semafor
(`--jobs N`, default 4 eller env `JOBS`). DB-skrivningarna sker synkront i
event loop-tråden, så den delade sqlite-anslutningen är säker.

Kostnad: Claude Haiku är billig (~$0,25/M tokens). En typisk OCR-sida
är ~600–1 000 tokens — om 5 % av ~47 000 sidor är dåliga är totalkostnaden
~$25–40 för hela arkivet.

Kör sedan `.venv/bin/python scripts/quality.py --per-page` och därefter `.venv/bin/python scripts/ingest.py` för att uppdatera
per-sida-poängen och re-indexera ändrade filer. `.venv/bin/python scripts/run_pipeline.py --with-llm`
gör båda stegen automatiskt.

### 3. Indexera i vektor-DB

```bash
.venv/bin/python scripts/ingest.py                       # nya + ändrade filer (mtime-detektering)
.venv/bin/python scripts/ingest.py --rebuild             # börja om från noll
.venv/bin/python scripts/ingest.py --reindex-since 2026-05-01  # tvinga om för gamla filer modifierade efter datum
.venv/bin/python scripts/ingest.py --help                # alla flaggor
```

**Per-sida-merge av Surya-omkörningar:** `.venv/bin/python scripts/ocr.py --redo --mode pages` skriver
per-sida-text till `pdf_pages`-tabellen i state.db. Direkt efter att ett dokument
är klart slår `scripts/ocr.py` automatiskt ihop dessa sidor in i
`generated/text/<stem>.txt` en sida i taget och behåller övriga sidor.
Idempotens spåras i state.db, och ingest fångar ändringarna via textfilens mtime.
Surya-redo väljer bara dåliga sidor som saknar rad i `pdf_pages`; rader från
Surya, LLM-korrektion eller andra per-sida-motorer räknas som redan försökt så
pipeline-resume inte startar processer som direkt hoppar alla sidor.

För att slå ihop alla väntande per-sida-resultat från state.db:

```bash
.venv/bin/python scripts/merge_pages.py --all            # slå ihop alla väntande per-sida-resultat
.venv/bin/python scripts/merge_pages.py --stem "1 — PM …"  # bara en specifik fil
```

Båda kommandona är idempotenta.

- Chunkar `text/*.txt` (800 tecken med 150 teckens överlapp, bryter på radslut).
- Embeddar lokalt med `intfloat/multilingual-e5-large` (svenska duger bra).
- Lagrar i lokal LanceDB med metadata (Nr, Titel, Sida, Anmärkning) **och `mtime`**
  så att ändrade `.txt`-filer (t.ex. efter `.venv/bin/python scripts/ocr.py --redo`) detekteras automatiskt
  och re-indexeras vid nästa körning.
- För filer som indexerades innan mtime-tracking infördes saknas mtime i tabellen
  (lagras som `0.0`). Använd `--reindex-since <tid>` för att tvinga re-index av
  legacy-rader vars `.txt` modifierats efter en känd tidpunkt — t.ex. när du
  precis kört en re-OCR-våg.
- Filtrerar bort OCR-skräp (chunks med <55 % alfanumeriska tecken).
- Idempotent. Första körningen laddar ned ~1.1 GB modell.

### 4. Ställ frågor

```bash
.venv/bin/python scripts/web.py   # Starta Streamlit-webgränssnittet
```

OAuth-token genereras med `claude setup-token` (engångsåtgärd).

#### RAG-läge (standard)

Klassisk *retrieval-augmented generation*: en fast pipeline i tre steg.

1. **Vektorsökning** — frågan embedas lokalt med `intfloat/multilingual-e5-large`
   och matchas mot LanceDB-indexet (top-20 kandidater).
2. **Hybrid + reranking (valfritt)** — `--hybrid` kombinerar vektor och BM25 (FTS)
   med *Reciprocal Rank Fusion* (k=60). Sedan omrankar
   `BAAI/bge-reranker-v2-m3` resultaten och plockar ut topp-6.
3. **Claude svarar** — de 6 utdragen skickas som kontext till Claude Opus 4.8
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

Fliken **Utredning** (Claude-backend) har en "Utredningsläge (MCP)"-toggle i sidebaren.
I MCP-läget får du en chatt där Claude minns tidigare frågor i konversationen
(implementerat via Claude Agent SDK:s `resume`-fält — `session_id` från senaste
svaret skickas med nästa fråga). Sidebar-knappen "Ny konversation" nollställer
historiken och startar en ny session. När MCP-läget är aktivt döljs
RAG-specifika sidofältskontroller (reranker, top-K/top-N, facetter och fuzzy);
kunskapsgrafens toggle ligger kvar eftersom grafen kan byggas även för
MCP-svar.

#### Utredning-fliken

Stödjer flera AI-backends via namngivna profiler. Profiler skapas och redigeras
i **Admin → Inställningar → LLM-inställningar**; i Utredning väljer du vilken
profil som ska användas för frågan.

- **Claude Opus 4.8** (default) — via `claude-agent-sdk` med adaptive thinking,
  kräver `CLAUDE_CODE_OAUTH_TOKEN` eller `ANTHROPIC_API_KEY`. Stödjer MCP-läge.
  (andra Claude-modeller kan anges i en profil.)
- **OpenAI GPT-5 / GPT-4o** — kräver `OPENAI_API_KEY`.
- **DeepSeek V4 / Reasoner** — kräver `DEEPSEEK_API_KEY`
  (`deepseek-chat` är V4-routern, `deepseek-reasoner` är thinking-modellen).
- **OpenAI-kompatibel (custom)** — pekar på vilken `/v1`-endpoint som helst
  (Ollama, LM Studio, llama.cpp, vLLM, fjärr-OpenAI-providers...). URL,
  modellnamn och namnet på API-nyckelns miljövariabel konfigureras i Admin.

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

Citat i svaret renderas som små inline-knappar — klick skickas via en gömd
iframe så huvudsidan inte laddas om. Openern validerar PDF-token och sidnummer,
bygger en `file://...#page=N`-URL när sida finns och öppnar PDF:en i en ny
webbläsarflik. Om samma dokument-ID delas av flera
filer (t.ex. en palme- och en wpu-version) och svaret inte entydigt pekar ut
vilken, visas en knapp per fil märkt med titeldelen.

#### Utredningspärm och bokmärken

Webgränssnittet har en egen flik, **Utredningspärm**, för återupptagbart
grävarbete. Varje RAG-svar och varje assistentsvar i MCP-chatten kan sparas
från Utredning-fliken med frågan, svaret, källistan, backend/modell, eventuella
inline-grafentiteter och en kort egen anteckning. Poster sparas i
`casebook_entries` i `generated/db/state.db` och visas nyast först på
Utredningspärm-sidan. Sparade svar renderas som stängda expanders med en kort
rubrik byggd från anteckningen om den finns, annars från frågan; fullständigt
svar och metadata visas först när posten öppnas. Källorna i en öppnad post
renderas som samma typ av källkort som i Utredning-fliken, med knappar för
matchande PDF och extraherad text när filerna finns lokalt. Sparade
grafentiteter renderas inte som en passiv lista, utan som en länk till
Graf-fliken; länken skickar de sparade center-entiteterna i query-parametern
`centers` och Graf-sidan återskapar då samma flerkärniga startgraf.

Källlistorna i både RAG- och MCP-läget har dessutom knappen **Bokmärk**. Den
sparar eller uppdaterar källan i `source_bookmarks`, deduplicerat på
`source` + `page` (okänd sida lagras som 0 internt men visas som tom sida i UI).
Bokmärkena visas på Utredningspärm-sidan och kan öppna matchande PDF i ny
webbläsarflik.

#### Anteckningar på källor

Varje källkort (i Utredning, Jämförelse och Maskeringar) har en hopfällbar
**✏️ Anteckna**-ruta för fritextanteckningar. Till skillnad från bokmärken kan
en källa/sida ha flera anteckningar; de sparas i `source_annotations` och
samlas på en egen flik (**Anteckningar**) i Utredningspärmen där de kan läsas,
öppna matchande PDF och tas bort. CRUD ligger i `src/db.py`
(`record_source_annotation`, `list_source_annotations`,
`update_source_annotation`, `delete_source_annotation`).

#### Sökfilter: facetter och OCR-tolerant fuzzy-sökning

I RAG-läget har Utredning-flikens sidofält två sökfilter. De visas inte i
MCP-läget eftersom MCP-verktygen väljer egna sökparametrar per verktygsanrop:

- **Facetter** — en multiselect med personer, platser och organisationer ur
  kunskapsgrafen (`doc_entities`). Väljs en eller flera entiteter *prefiltreras*
  vektorsökningen på `source`-kolumnen (`facets.sources_where_clause` →
  `table.search(...).where(..., prefilter=True)`), så att även dokument som
  ligger utanför den ofiltrerade topp-K kan ytas. `src/facets.py` bygger ett
  cachat `casefold(namn) → {pdf_stem}`-index så `doc_entities` inte parsas om
  per sökning.
- **OCR-tolerant fuzzy-sökning** — en toggle (med en likhetströskel-slider) som
  lägger till träffar där söktermen förekommer felstavad av OCR (t.ex.
  *Engstrcm* för *Engström*). `src/search_fuzzy.py` bygger med ren `difflib` ett
  token-index över hela chunk-korpusen *en gång* (cachat med
  `st.cache_resource`, kolumnprojicerat så vektorkolumnen aldrig laddas,
  ~100 MB minne, ~30 s första körningen) och matchar frågans tokens mot
  vokabulären. Tröskeln styr toleransen: korta namn med ett OCR-fel
  (*Palme*→*Paine*, difflib-ratio ~0.6) kräver låg tröskel, längre ord klarar
  högre. Träffarna varvas (round-robin) med vektorträffarna så de får plats
  bland topp-N även utan reranker; med rerankern rangordnas hela den utökade
  kandidatmängden.

#### Maskeringsutforskaren (Maskeringar-fliken)

Sidan `pages/5_Maskeringar.py` listar dokument sorterade efter hur många
`[MASKAD]`-markörer redaktionsdetekteringen infogat. Kontextutdrag och
källknappar renderas först för den dokumentrad användaren klickar i tabellen,
så sidan inte bygger hundratals dolda detaljblock vid sidbyte. All aggregering sker i
`src/redactions.py` direkt mot `pdf_pages` i state.db (ingen ny pipeline-körning
behövs).

#### Vittnesjämförelse (Jämförelse-fliken)

Sidan `pages/6_Jämförelse.py` är ett korsförhörsläge: ange ett ämne, hämta
flera källor med samma sök+rerank som Utredning, och låt språkmodellen ställa
dem mot varandra. System-prompten (i `src/compare.py`) ber modellen lyfta fram
**motstridiga** och **överensstämmande** uppgifter med källhänvisningar, i
stället för att syntetisera bort konflikterna. Backend följer det val som
sparats i `generated/llm_config.json` (sätts i Utredning-flikens sidofält).
Inline-referenser renderas med samma `?pdf=&page=`-opener som Utredning-sidan,
så klick på en referens öppnar matchande PDF i ny webbläsarflik på refererad
sida utan att jämförelsesvaret laddas om.

#### Karta (Karta-fliken)

`src/pages/7_Karta.py` renderar en folium-karta via `streamlit-folium`. Alla
observationer med giltig koordinat ritas som **statiska markörer** (färg per
person) så att valda personer alltid syns; personer med minst två tidsatta
observationer får en rörelselinje. Tidsanimeringen (`TimestampedGeoJson` byggd i
`src/karta.py`) är en **valbar toggle** ("Animera tidslinje") eftersom den bara
visar punkter vid reglagets aktuella tidpunkt. Tider före kl 12 tolkas som efter
midnatt (nästa dygn) så mordkvällens 23:xx sorteras före 00:xx–05:xx.

**Flytta-läge:** klicka på en markör för att välja en observation
(`st_folium` `last_object_clicked` → `karta.observations_at_coord`; flera träffar
på samma punkt löses via listan). Med "Flytta-läge" på flyttas den valda
observationen dit man klickar på kartan (`last_clicked` → `update_map_observation`),
och tiden kan sättas i ett snabbfält intill kartan. Observationer utan tid visas
som markörer men inte i den animerade tidslinjen.

**Källväljare:** i stället för att skriva källans `Nr` blint söker man fram
dokumentet i en sökruta (nr eller titel) och väljer ur en filtrerad lista
(`karta.search_sources` över ett cachat index byggt ur `citations.build_nr_to_pdf`,
~11 000 dokument). Valet fyller i `Nr`-fältet; `sida` anges separat. Seed-data
ligger i `data/karta/platser.json` och `data/karta/rorelser.json`; seed körs bara
när karttabellerna är tomma.

`scripts/extract_map_observations.py` läser `generated/text/*.txt` sida för sida och
skapar kandidater i `map_observation_candidates`. Extraktorn använder samma
LLM-konfigurationsmönster som kunskapsgrafens `scripts/extract_entities.py`, men har en
snäv prompt: bara person + plats + tid + citatutdrag. Platsnamn matchas mot
`data/karta/platser.json`; okända platser sparas utan koordinater och måste
kompletteras i review-UI:t (**Granska extraherade kartförslag** i Karta-fliken)
innan de kan godkännas. Varje lyckat sid-anrop markeras i
`map_observation_extractions`, även när svaret innehåller noll kandidater, så
idempotenta omkörningar bara försöker nya eller tidigare felande sidor. Kommandon:

```bash
.venv/bin/python scripts/extract_map_observations.py --dry-run [--limit N]
.venv/bin/python scripts/extract_map_observations.py [--limit N] [--provider openai --model gpt-4o-mini]
```

#### Kunskapsgraf till svaret

När Neo4j är igång (`.venv/bin/python scripts/neo4j.py` + `.venv/bin/python scripts/load_graph.py`) erbjuds ett ego-nätverk ur
kunskapsgrafen som en hopfälld sektion (toggle) i fullbredd mellan svaret och
källistan — i både RAG-läget och utredningsläget. **Grafen byggs först när
användaren öppnar toggeln, inte automatiskt efter varje svar.** Det är då, och
bara då, som den valda LLM-backenden listar svarets nyckelentiteter
(`src/graph/answer_entities.py`). DeepSeek/OpenAI använder vald modell och
Claude använder Haiku för den lilla extraktionsuppgiften. Namnen slås upp i
grafen (`viz.lookup_centers`) och ritas med Cytoscape (st-link-analysis, ingår
i extran `.[graph]`). Resultatet (center-noderna) cachas per svar i session
state så att öppna/stänga eller andra reruns inte kör om den dyra extraktionen.
Cytoscape-komponenten kan dessutom inte monteras dold (ingen resize-hantering),
så grafen ritas oavsett bara när sektionen är synlig. `Visa kunskapsgraf` i
sidofältet styr om sektionen (toggeln) visas alls.

Grundgrafen visar **endast svarets entiteter** och relationerna mellan dem —
inte deras hela grannskap. Grafen är interaktiv: dubbelklick på en entitetsnod
fäller ut dess grannskap (relationer + dokument som nämner den), dubbelklick
på en dokumentnod öppnar PDF:en i ny webbläsarflik. Centernoder (svarets entiteter +
utfällda) märks med ★ i namnet. Inbyggd verktygsrad ger fullskärm och
zoom-passning.

I grafsektionen finns en sidopanel med:

- **Höjdreglage** — gör graffönstret större (400–1200 px).
- **Legend** — typ och färg för nodtyperna som ingår (Person, Plats,
  Organisation, Dokument).
- **Återställ** — tar bort utfällda noder. Utfällda noder sparas per svar och
  nollställs automatiskt vid nytt svar.
- **Dokumentlänkar** — grafens dokumentnoder som klickbara länkar; öppnar
  PDF:en i ny webbläsarflik via samma `?pdf=`-mekanism som citatlänkarna i svaret
  (utan sidnummer om grafnoden inte har sida).

Grafen är frivillig: är Neo4j nere eller LLM-konfig saknas visas en kort notis
i stället, och sidofältets toggle "Visa kunskapsgraf" (på som standard)
stänger av hela steget. Entitetslistorna cachas per svar i session state, så
klick på PDF-knappar triggar inga nya LLM-anrop.

## Kunskapsgraf (Neo4j)

Utöver vektorsökningen kan arkivet byggas upp som en kunskapsgraf: en LLM
extraherar personer, platser, organisationer och relationer ur arkivtexten,
sida för sida. Grafen är ett **komplement** till RAG-sökningen, inte en
ersättning — LanceDB sköter fortfarande all sökning, medan Neo4j ger
relationsfrågor och visualisering. De två lagren delar nyckeln `pdf_stem`.

Extraktionen använder samma LLM som är konfigurerad i `generated/llm_config.json`
(samma config som Utredning-fliken/`scripts/llm_correct.py`); finns ingen config används Claude
Haiku som default. Flaggorna `--provider`, `--model`, `--base-url` och
`--api-key` skriver över den sparade konfigurationen för just denna körning.

Kunskapsgrafen är valfri och dess beroenden installeras därför separat:

```bash
brew install podman
podman machine init --memory 4096
.venv/bin/pip install -e '.[graph]'
```

```bash
.venv/bin/python scripts/extract_entities.py --dry-run        # se omfång utan kostnad
.venv/bin/python scripts/extract_entities.py --limit 20       # provkörning
.venv/bin/python scripts/extract_entities.py                  # hela arkivet (~$60–120, gäller Claude Haiku)
```

Starta sedan Neo4j och ladda grafen. `.venv/bin/python scripts/neo4j.py` (podman) sköter allt —
startar podman-maskinen vid behov, genererar ett lösenord (sparas i
`neo4j/.password`, läses automatiskt av `scripts/load_graph.py`), skapar/startar
containern och väntar tills Neo4j svarar:

```bash
.venv/bin/python scripts/neo4j.py             # starta (skapar container + lösenord första gången)
.venv/bin/python scripts/load_graph.py        # ladda grafen — lösenordet plockas upp automatiskt
.venv/bin/python scripts/neo4j.py status      # kör den?
.venv/bin/python scripts/neo4j.py stop
```

Kör du **Docker** istället för podman finns även en compose-fil:

```bash
cd neo4j && NEO4J_PASSWORD=... docker compose up -d
NEO4J_PASSWORD=... .venv/bin/python scripts/load_graph.py     # ladda grafen till Neo4j
```

> Podman-maskinen behöver ≥4 GiB minne för Neo4j:s 2G-heap:
> `podman machine set --memory 4096` (en gång, med maskinen stoppad).

> Om Utredning-fliken har sparat en dyrare modell (t.ex. Opus) i `llm_config.json`, kör
> `.venv/bin/python scripts/extract_entities.py --model claude-haiku-4-5-20251001` för att tvinga
> Haiku och hålla kostnaden nere.

`--jobs` (default 4) styr hur många sidor som extraheras parallellt — sidorna
i ett dokument är oberoende av varandra, så detta krävs för att hinna med hela
arkivet (~40 000 sidor) på rimlig tid. `--timeout` (default 120 s) sätter en
maxgräns per LLM-anrop; sidor som tar längre loggas som fel och försöks om vid
nästa körning.

Schema:

```
(:Dokument {stem, nr, titel})
(:Person {norm, namn})  (:Plats {norm, namn})  (:Organisation {norm, namn})
(:Dokument)-[:NÄMNER {sida}]->(entitet)
(entitet)-[:RELATERAR {typ, stem, sida}]->(entitet)
```

### Utforska i Neo4j Browser

Öppna Neo4j Browser på <http://localhost:7474> och logga in med användarnamnet
`neo4j` och lösenordet du satte i `NEO4J_PASSWORD`. Exempel-Cypher:

```cypher
MATCH (p:Person {norm: "stig engström"})<-[:NÄMNER]-(d:Dokument) RETURN d.titel, d.nr
MATCH (a:Person)-[r:RELATERAR]->(b) RETURN a.namn, r.typ, b.namn LIMIT 50
```

Extraktionen är idempotent — vilka sidor som behandlats spåras i
`doc_entities`-tabellen i state.db. Neo4j-grafen kan alltid byggas om från
grunden därifrån, och en omkörning av `scripts/load_graph.py` ger samma graf igen
(`MERGE`).

### Utforska i webgränssnittet

Webgränssnittet (`.venv/bin/python scripts/web.py`) har en **Graf**-sida i sidofältet. Sök en person,
plats eller organisation så ritas dess nätverk (relationer + dokument som
nämner den) som en interaktiv graf, och källdokumenten listas med
PDF-knappar. Kräver att Neo4j är igång (`.venv/bin/python scripts/neo4j.py`) och grafen laddad
(`.venv/bin/python scripts/load_graph.py`) — saknas det visar sidan en uppmaning i stället för fel.
Utöver grafsidan visas också ett ego-nätverk automatiskt under varje svar på
frågesidan (se *Kunskapsgraf till svaret* ovan).

**Fäll ut noder:** dubbelklicka på en entitetsnod i grafen för att lägga till
dess nätverk — så går det att vandra utåt från den sökta noden. Dubbelklick på
en dokumentnod öppnar PDF:en. Centernoder (sökta + utfällda) märks med ★;
*Återställ till sökt nod* i sidofältet fäller ihop igen. Återkommer en
relation i flera dokument kollapsas den till en kant märkt `×N`.

### Namnvarianter (entity resolution)

Samma person kan stavas på flera sätt i materialet. Två deterministiska steg i
`load_neo4j` motar det utan att gissa: `Efternamn, Förnamn` vänds till
`Förnamn Efternamn`, och ett ensamt efternamn (`Engström`) slås ihop med
fullnamnet (`Stig Engström`) **bara om exakt ett** fullnamn i samma dokument
matchar. Tvetydiga fall (dokument med både Olof och Lisbeth Palme) lämnas
orörda. Återstående varianter motas vid källan av en prompt-regel som kräver
konsekvent `Förnamn Efternamn`-form (gäller nya extraktioner). Efter ändrade
regler: töm grafen (`MATCH (n) DETACH DELETE n` i Neo4j Browser) och kör
`scripts/load_graph.py` igen.

## LLM-konfiguration (`generated/llm_config.json`)

Admin sparar namngivna profiler i `generated/llm_config.json`; Utredning väljer
en profil per session. Filen skapas automatiskt — ta bort den för att återgå
till standardprofilen (Claude Opus 4.8). API-nycklar läses alltid från
miljövariabler och lagras aldrig i filen. För en egen OpenAI-kompatibel endpoint
anger profilen endast miljövariabelns namn i `api_key_env`.

```json
{
  "profiles": {
    "Standard": {
      "backend_name": "Claude",
      "provider": "claude",
      "model": "claude-opus-4-8",
      "base_url": ""
    }
  },
  "default": "Standard"
}
```

| Fält | Möjliga värden |
|---|---|
| `provider` | `claude`, `openai`, `deepseek`, `openai_compatible` |
| `model` | t.ex. `claude-opus-4-8`, `gpt-4o`, `deepseek-chat`, `deepseek-reasoner` |
| `base_url` | Tomt för molntjänster; URL för lokal endpoint (`http://localhost:11434/v1` för Ollama) |
| `backend_name` | Visningsnamn i gränssnittet (valfritt) |
| `api_key_env` | Namnet på miljövariabeln som innehåller API-nyckeln; aldrig nyckelvärdet |

### Visa/ändra konfigen utan webgränssnitt (`scripts/llm_config.py`)

Snabbaste sättet att se eller byta vald LLM utan att starta Streamlit.

Kör utan argument i en terminal startas en **interaktiv meny** där backend och
modell väljs ur samma backend-katalog som Admin (Claude / OpenAI / DeepSeek /
Ollama / OpenAI-kompatibel). För OpenAI-kompatibla providers hämtas modell-listan
live från `/v1/models` (faller tillbaka på en inbyggd lista om endpoint eller
nyckel saknas), och konfigurerbara backends frågar efter endpoint-URL och en
valfri API-nyckel. Körs skriptet utan terminal (pipe/skript) skrivs i stället den
aktuella konfigurationen ut.

```bash
.venv/bin/python scripts/llm_config.py                                              # interaktiv meny (terminal); annars visa konfig
.venv/bin/python scripts/llm_config.py --model claude-haiku-4-5-20251001            # byt modell (samma provider)
.venv/bin/python scripts/llm_config.py --provider openai                            # byt provider, modell återställs till providerns default
.venv/bin/python scripts/llm_config.py --provider openai --model gpt-4o --base-url https://api.deepseek.com/v1
.venv/bin/python scripts/llm_config.py --reset                                      # ta bort sparad konfig, tillbaka till defaults
```

Backend-katalogen (namn, modell-listor, endpoints) bor i `src/backends.py` och
delas mellan Admin, Utredning och `scripts/llm_config.py`.

## Bakgrundsjobb och adminsida

Produktionsflöden (download, OCR, kvalitet, ingest, LLM-extraktion, Neo4j) kan
köras i förgrunden som vanliga scripts eller som **bakgrundsjobb** från CLI
eller adminsidan (`src/pages/8_Admin.py`). Jobbet startas som en fristående
worker-process och fortsätter även om webbläsaren eller Streamlit stängs.

### Jobbmodell (`admin_jobs` i `state.db`)

- Tabellen skapas av schema-migration 7 i `src/db.py`; all jobb-SQL ligger i
  `src/db.py` (konsumenter skriver aldrig egen SQL).
- Statusar: `queued` → `running` → `succeeded`/`failed`, eller via
  `cancel_requested` → `cancelled`; kraschade/omstartade jobb markeras
  `interrupted`.
- **Endast ett aktivt skrivande jobb åt gången**: `queued`, `running` och
  `cancel_requested` har `active_slot = 1`; det partiella unika indexet
  `admin_jobs_one_active` gör regeln atomisk även vid parallella startförsök.
- Parametrar sparas som normaliserad JSON i `params_json`. Parametrar markerade
  `secret` (t.ex. `--api-key`) avvisas i bakgrundsläget — API-nycklar läses i
  stället ur processmiljön (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`) och skrivs aldrig till JSON eller logg.

### Worker och livscykel

1. `scripts/jobs.py start <operation> [flaggor]` (eller adminsidans startknapp)
   skapar jobbraden, loggfilen `generated/admin_jobs/<id>.log` och startar
   `python -m operations.worker --job-id <id>` med `start_new_session=True`.
2. Workern claimar jobbet (`queued` → `running` + PID), startar en
   heartbeat-loop som skriver `heartbeat_at` minst var 5:e sekund och kör
   operationen med en jobb-context som skriver progress till SQLite och output
   till loggfilen.
3. Vid avslut skrivs ett terminaltillstånd och `active_slot` frigörs i samma
   transaktion. Ett ohanterat undantag ger `failed` med traceback i loggen.
4. `status`/adminsidan kör en reconcile: aktivt jobb med färsk heartbeat
   (< 15 s) eller levande process lämnas orört; gammal heartbeat utan process
   markeras `interrupted`. Signalering sker aldrig enbart utifrån ett sparat
   PID (återanvända PID:er). Ett pid-löst `queued`-jobb äldre än 60 s antas
   kvarlämnat och avbryts.

Efter en datoromstart markeras jobbet `interrupted`; starta samma idempotenta
operation igen för att slutföra arbetet.

### Progress och logg

- Strukturerad progress ligger i SQLite: `current_step`, `completed_units`,
  `total_units` och senaste `message`. Steg utan känd total har
  `total_units = NULL` och visas utan procentsiffra.
- Loggfilen `generated/admin_jobs/<id>.log` är append-only och tidsstämplad;
  kommandorader redigeras så att hemligheter (nycklar, token, lösenord) aldrig
  hamnar i loggen.
- `scripts/jobs.py log [--job-id X]` visar de sista 50 raderna;
  `log --follow` följer tills jobbet når ett terminaltillstånd.

### Avbryta

`scripts/jobs.py cancel [--job-id X]` och adminsidans Avbryt-knapp anropar
samma `request_cancel()`: status → `cancel_requested`, workern ser begäran via
heartbeat-loopen, externa barnprocesser får SIGTERM som processgrupp och efter
en grace-period SIGKILL. Ctrl-C i förgrunden ger exitkod 130. Redan committade
per-dokumentresultat behålls; ett avbrutet jobb markeras aldrig `succeeded`.

### Adminsidan (`src/pages/8_Admin.py`)

- Lokal, utan inloggning; exponerar bara produktionsflöden (installation,
  tester, tessdata och webbstart visas inte).
- Aktivt jobb-panel överst med progress, logg och Avbryt; uppdateras med
  `st.fragment(run_every=...)` bara medan ett jobb är aktivt.
- Medan ett aktivt skrivande jobb finns är startknapparna för andra skrivande
  operationer avstängda; jobbstatus, logg och Neo4j-status förblir tillgängliga.
- API-nycklar visas eller lagras inte — sidan visar bara om relevant
  miljövariabel är tillgänglig. LLM-provider, modell och base URL sparas via
  den gemensamma konfigurationsmodulen (`generated/llm_config.json`).
- Formulär, validering och startknappar genereras ur samma operationsregistry
  som CLI:n, så parametrar/defaults är identiska.

## Filer

| Fil | Vad |
|---|---|
| `scripts/jobs.py` | Starta/övervaka/avbryta bakgrundsjobb (`start`, `status`, `list`, `log`, `log --follow`, `cancel`) — delar registry med adminsidan |
| `scripts/install.py` | Installera pipeline/webgränssnitt via Homebrew och pip (Python-paket, tessdata, hunspell) |
| `scripts/run_pipeline.py` | Kör hela pipelinen i ett kommando: download → OCR → ingest (flaggor: `--skip-wpu`, `--skip-redo`, `--with-llm`, `--jobs N`, `--test N`) |
| `scripts/download.py` → `src/download.py` | Hämta PDF:er från Drive |
| `scripts/download_wpu.py` → `src/download_wpu.py` | Ladda ner alla PDF:er från wpu.nu → `downloaded/wpu_files/` |
| `scripts/merge_wpu.py` → `src/merge_wpu.py` | Jämför wpu- och palme-text per fil, behåll bäst kvalitet |
| `scripts/setup_tessdata.py` | Sätt upp projekt-lokal `tessdata/` med swe_best |
| `scripts/ocr.py` | Full OCR-pipeline (Tesseract → Surya-fallback för Tesseract-fel → kvalitet → Surya på dåliga sidor); `--redo` kör om dåliga filer/sidor |
| `scripts/ocr_tesseract.py` | Bara Tesseract-steget (textextraktion + ocrmypdf) |
| `src/ocr_db_helper.py` | CLI-hjälpare för tesseract-steget: tesseract-status och `text_mtime`-stämpling i `state.db` |
| `scripts/ocr_pages.py` → `src/ocr_pages.py` | Per-sida OCR (Tesseract/Surya), lagrar sidtext och metadata i `state.db` |
| `scripts/merge_pages.py` → `src/merge_pages.py` | Slå ihop per-sida-text från `state.db` in i `generated/text/<stem>.txt` |
| `scripts/build_user_words.py` → `src/build_user_words.py` | Bygg `tessdata/swe.user-words.auto` från `generated/text/*.txt` |
| `scripts/quality.py` → `src/quality.py` | Heuristisk kvalitetsbedömning av `generated/text/*.txt` (`--per-page` finns) |
| `scripts/normalize.py` → `src/normalize_text.py` | Regelbaserad OCR-normalisering (körs automatiskt av `scripts/ocr.py`) |
| `scripts/llm_correct.py` → `src/llm_correct.py` | LLM-korrektion av dåliga OCR-sidor via Claude Haiku |
| `scripts/detect_redactions.py` → `src/operations/detect_redactions.py` | Kör redaktionsdetektering på befintliga text/OCR-par |
| `scripts/ingest.py` → `src/rag/ingest.py` | Bygg vektorindex (LanceDB + BM25 FTS) |
| `src/rag/ask.py` | Frågefunktioner — RAG-läge och MCP-läge (importeras av Utredning-sidan och mcp_server) |
| `src/rag/mcp_server.py` | MCP-server med `search_archive` och `get_page` (startas av ask.py/Utredning.py) |
| `generated/llm_config.json` | Sparad LLM-konfiguration (backend, modell, URL) — se ovan |
| `src/config.py` | Läser/skriver `generated/llm_config.json` (delas av Utredning-sidan och llm_correct) |
| `src/backends.py` | Delad backend-katalog (Claude/OpenAI/DeepSeek/Ollama/custom) + `fetch_models`/`available_models` — delas av Utredning-sidan och `scripts/llm_config.py` |
| `scripts/llm_config.py` → `src/llm_config_cli.py` | Visa/ändra `generated/llm_config.json` utan webgränssnittet (interaktiv meny i terminal) |
| `src/citations.py` | Slår upp `[Nr X, sida Y]` och WPU-prefix som `[Pol-..., sida Y]` mot PDF:er och renderar citatlänkar |
| `src/Utredning.py` | Streamlit-flik för frågor (RAG + MCP-toggle), svarsgraf, sparknapp, källbokmärken samt facett-/fuzzy-sökfilter |
| `src/casebook_ui.py` | Delade Streamlit-komponenter för utredningspärm, källbokmärken och anteckningar |
| `src/pages/2_Utredningspärm.py` | Streamlit-sida för sparade fråga/svar-spår, bokmärkta källor och anteckningar |
| `src/facets.py` | Facetterad sökning: entiteter ur `doc_entities` → filtrera sökträffar |
| `src/search_fuzzy.py` | OCR-tolerant fuzzy-sökning (difflib token-index över chunk-korpusen) |
| `src/redactions.py` → `src/pages/5_Maskeringar.py` | Maskeringsutforskaren: aggregera `[MASKAD]` ur `pdf_pages`, visa kontext |
| `src/compare.py` → `src/pages/6_Jämförelse.py` | Vittnesjämförelse (korsförhörsläge — letar motstridiga uppgifter) |
| `src/karta.py` → `src/pages/7_Karta.py` | Kartmodulen: validera observationer, bygg tidslinje-GeoJSON och rendera karta |
| `src/map_extract.py` | Ren parsing/tidsnormalisering/platsmatchning för kartobservations-kandidater |
| `scripts/extract_map_observations.py` → `src/extract_map_observations.py` | LLM-extraktion av person-position-tid-kandidater till `map_observation_candidates` |
| `scripts/extract_entities.py` → `src/graph/extract_entities.py` | Entitets-/relationsextraktion till `doc_entities` i state.db (Claude Haiku) |
| `scripts/load_graph.py` → `src/graph/load_neo4j.py` | Ladda kunskapsgrafen från state.db till Neo4j |
| `scripts/neo4j.py` → `src/operations/neo4j.py` | Starta/stoppa Neo4j via podman (genererar lösenord → `neo4j/.password`) |
| `src/graph/viz.py` | Bygg ego-nätverk (flera center) + Cytoscape-konvertering för grafvyerna |
| `src/graph/answer_entities.py` | Vald LLM-backend listar nyckelentiteter ur ett RAG-svar — DeepSeek/OpenAI använder vald modell, Claude använder Haiku |
| `src/pages/3_Graf.py` | Streamlit-grafsida: sök entitet → interaktivt nätverk, fäll ut noder |
| `neo4j/docker-compose.yml` | Neo4j 5 för kunskapsgrafen med Docker (Browser på :7474) |
| `scripts/web.py` | Startar Streamlit-servern |
| `src/operations/` | Delat operationslager: modeller, registry, CLI, `job_service`/`worker`, OCR/redactions/pipeline/neo4j-orkestrering |
| `src/admin_ui.py` + `src/pages/8_Admin.py` | Lokal Streamlit-adminsida: bakgrundsjobb, operationer och LLM-inställningar |
| `src/db.py` | SQLite-state: versionsstyrt schema + CRUD + delta-queries, inklusive `admin_jobs`, utredningspärm, källbokmärken och anteckningar |
| `tests/fixtures/state_db_v4.sql` + `tests/fixtures/state_db_v5_missing_surya.sql` | Gamla state-db-fixtures som verifierar schema-migreringar |
| `tessdata/swe.user-words` | Palme-specifika ord (committat) |
| `tessdata/tesseract.config` | `preserve_interword_spaces 1` (committat) |
| `data/karta/platser.json` + `data/karta/rorelser.json` | Seed-filer för kartans platser och observationer |

### Bonus: kvalitetskoll

```bash
.venv/bin/python scripts/quality.py --top 30      # visa värsta 30 filer
.venv/bin/python scripts/quality.py --rebuild     # tvinga om-bedömning av alla filer
```

Poäng 0–100 per fil, inkrementellt (bara filer vars `text_mtime` är nyare än `scored_at`). Notera att `text-layer` inte automatiskt innebär god kvalitet — vissa PDF:er har gammalt OCR-skräp inbäddat. Sortera på `score`, inte källa. Dåliga textlager rättas med `.venv/bin/python scripts/ocr.py --redo --mode files`.

Valfritt: installera hunspell + sv_SE-ordlista för att fylla i `pct_swe`-kolumnen (`brew install hunspell` + ordlistfiler från LibreOffice/dictionaries).

## Tester

```bash
.venv/bin/pip install -e '.[dev,web]'
.venv/bin/python scripts/test.py
.venv/bin/python scripts/test.py --static
```

`scripts/test.py` kör pytest. Med `--static` körs även `ruff check .` och `mypy src`
via `.venv/`; saknas verktygen avslutar scriptet med en installationshint.

Testerna täcker: `score_text` (quality), `chunk_text` (ingest), `extract_drive_id`/`sniff_extension` (download),
`detect_redactions_image` (ocr_pages), `merge_one` (merge_pages), `merge_wpu` (merge_wpu),
LLM-korrektionslogiken (llm_correct), re-ingest-flödet (ingest), state-databasen inkl.
schema-migrationer, delta-urval och `text_mtime`-stämpling (db, ocr_db_helper, normalize, quality),
citatuppslag/-länkning (citations), RRF-hybridsökningen (ask) och `get_page` (mcp_server),
entitetsextraktion (extract_entities), graf-laddning (load_neo4j) och
nyckelentiteter ur svar (answer_entities).
Fixturen som genererar en mini-PDF med pymupdf skipas gracefully om pymupdf inte är installerat.

## Felloggning

Skript skriver tab-separerade rader till `generated/errors.log`:
``ISO8601\tcomponent\titem\tmessage``. Alla Python-entrypoints loggar via
`errors_log.log_error` (roteras automatiskt vid 10 MB). Append-only, idempotent.

## Datafiler (gitignorerade)

```
downloaded/   — nedladdade PDF:er (files/, wpu_files/)
generated/    — allt pipeline-genererat (text/, ocr/, lancedb/, db/state.db, errors.log, …)
tessdata/*.traineddata  — laddas av scripts/setup_tessdata.py
```

Åter-skapas helt av skripten — ta bort katalogerna och kör om.
