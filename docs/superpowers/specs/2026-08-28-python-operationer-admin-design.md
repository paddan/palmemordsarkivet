# Python-operationer och lokal adminsida

**Datum:** 2026-08-28
**Status:** Godkänd av användaren 2026-08-28

## Bakgrund

Projektet har 23 shell-script i roten. Flera är tunna wrappers runt befintliga
Python-moduler, men framför allt `ocr.sh`, `ocr_tesseract.sh`,
`detect_redactions.sh` och `run_pipeline.sh` innehåller betydande
orkestreringslogik. Samma produktionsflöden ska kunna startas och övervakas
från Streamlit utan att adminsidan får en separat implementation.

Alla shell-script ska ersättas av Python-filer och tas bort. Adminsidan ska
endast exponera produktionsflöden: nedladdning, OCR, kvalitet, ingest,
LLM-baserad korrigering/extraktion och Neo4j. Installation, tester,
tessdata-installation och start av webbservern ska fortfarande migreras till
Python, men inte visas på adminsidan.

Applikationen används lokalt och behöver ingen inloggning.

## Mål

- Ett gemensamt, testbart Python-API för alla operationer.
- Manuell CLI-åtkomst till allt som adminsidan kan göra, med samma parametrar,
  standardvärden och validering.
- Ett lokalt bakgrundsjobb åt gången.
- Jobbet fortsätter när webbläsaren stängs eller Streamlit startas om.
- Progress, heartbeat och logg kan visas efter återanslutning från både
  admin och CLI.
- Jobbet kan stoppas kontrollerat från både admin och CLI.
- Befintlig idempotens i `state.db` och befintliga pipeline-resultat bevaras.

## Avgränsning

Följande ingår inte:

- fjärråtkomst, autentisering eller rollstyrning,
- flera samtidiga skrivande jobb,
- en extern köserver som Celery, Redis eller RQ,
- automatisk återstart av ett jobb efter omstart av hela datorn,
- ändringar av OCR-, kvalitets- eller RAG-algoritmer utöver vad som krävs
  för att flytta orkestreringen från shell till Python.

## Arkitektur

Lösningen har fyra lager:

1. Befintliga domänmoduler i `src/` utför nedladdning, OCR, kvalitet,
   normalisering, ingest och extraktion.
2. Ett nytt `src/operations/` samlar orkestrering, externa processer,
   progress, avbrytning och en gemensam operationskatalog.
3. Python-filer i `scripts/` ger det manuella CLI-gränssnittet.
4. `src/pages/8_Admin.py` renderar operationskatalogen och jobbstatus i
   Streamlit.

CLI och admin använder samma `OperationDefinition` och samma `run()`-funktion.
Streamlit bygger aldrig fria kommandorader och tolkar aldrig terminaltext för
att gissa progress.

### Centrala gränssnitt

`src/operations/models.py` definierar minst:

```python
@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    flags: tuple[str, ...]
    kind: Literal["bool", "int", "float", "str", "path", "choice"]
    default: object
    help: str
    choices: tuple[str, ...] = ()
    required: bool = False
    secret: bool = False


@dataclass(frozen=True)
class OperationDefinition:
    id: str
    label: str
    group: str
    description: str
    parameters: tuple[ParameterDefinition, ...]
    admin_visible: bool
    mutating: bool
    confirmation: str | None
    run: Callable[["OperationContext", Mapping[str, object]], None]
```

`src/operations/context.py` tillhandahåller:

```python
class OperationContext:
    def step(self, name: str, *, completed: int = 0,
             total: int | None = None) -> None: ...
    def progress(self, completed: int, total: int | None,
                 message: str = "") -> None: ...
    def log(self, message: str, *, level: str = "info") -> None: ...
    def check_cancelled(self) -> None: ...
    def run_process(self, argv: Sequence[str], *, cwd: Path,
                    env: Mapping[str, str] | None = None) -> int: ...
```

Förgrundskörning använder en terminal-context. Bakgrundskörning använder
en jobb-context som skriver strukturerad status till SQLite och full output till
jobbloggen. Domän- och orkestreringskod behöver inte känna till Streamlit.

### Operationskatalog och parameterparitet

`src/operations/registry.py` är den enda katalogen över operationer och
parameterdefinitioner. CLI-parsern och adminsidan genereras från dessa
definitioner. Specialiserad validering kan kopplas till en operation, men dess
resultat ska användas av båda gränssnitten.

Riskfyllda parametrar som `--rebuild`, ominitiering eller stopp av Neo4j har en
bekräftelsetext i definitionen. Adminsidan kräver explicit bekräftelse. CLI
behåller explicit flagga eller underkommando som användarens bekräftelse och
ställer inga interaktiva frågor som försvårar automation.

## Python-script

De befintliga filerna ersätts enligt tabellen. `scripts/jobs.py` tillkommer för
bakgrundskontroll och har ingen tidigare shell-motsvarighet.

| Tas bort | Python-ersättare | Admin |
|---|---|---|
| `build_user_words.sh` | `scripts/build_user_words.py` | Ja |
| `detect_redactions.sh` | `scripts/detect_redactions.py` | Ja |
| `download.sh` | `scripts/download.py` | Ja |
| `download_wpu.sh` | `scripts/download_wpu.py` | Ja |
| `extract_entities.sh` | `scripts/extract_entities.py` | Ja |
| `extract_map_observations.sh` | `scripts/extract_map_observations.py` | Ja |
| `ingest.sh` | `scripts/ingest.py` | Ja |
| `install.sh` | `scripts/install.py` | Nej |
| `llm_config.sh` | `scripts/llm_config.py` | Ja |
| `llm_correct.sh` | `scripts/llm_correct.py` | Ja |
| `load_graph.sh` | `scripts/load_graph.py` | Ja |
| `merge_pages.sh` | `scripts/merge_pages.py` | Ja |
| `merge_wpu.sh` | `scripts/merge_wpu.py` | Ja |
| `neo4j.sh` | `scripts/neo4j.py` | Ja |
| `normalize.sh` | `scripts/normalize.py` | Ja |
| `ocr.sh` | `scripts/ocr.py` | Ja |
| `ocr_pages.sh` | `scripts/ocr_pages.py` | Ja |
| `ocr_tesseract.sh` | `scripts/ocr_tesseract.py` | Ja |
| `quality.sh` | `scripts/quality.py` | Ja |
| `run_pipeline.sh` | `scripts/run_pipeline.py` | Ja |
| `setup_tessdata.sh` | `scripts/setup_tessdata.py` | Nej |
| `test.sh` | `scripts/test.py` | Nej |
| `web.sh` | `scripts/web.py` | Nej |

Manuell direktkörning använder exempelvis:

```bash
.venv/bin/python scripts/run_pipeline.py --jobs 4
.venv/bin/python scripts/ocr.py --redo --mode pages --threshold 50
.venv/bin/python scripts/ingest.py --rebuild
.venv/bin/python scripts/neo4j.py status
```

Installationsscriptet måste kunna startas med ett system-Python 3.11+ innan
projektets virtualenv finns:

```bash
python3 scripts/install.py --dev
```

### Bakgrundskörning från CLI

`scripts/jobs.py` exponerar samma jobbhantering som admin:

```bash
.venv/bin/python scripts/jobs.py start run-pipeline --jobs 4
.venv/bin/python scripts/jobs.py status
.venv/bin/python scripts/jobs.py log
.venv/bin/python scripts/jobs.py log --follow
.venv/bin/python scripts/jobs.py cancel
.venv/bin/python scripts/jobs.py list
```

`start` accepterar operationens registrerade icke-hemliga flaggor. Hemliga
flaggor som en direktkörning kan ta emot avvisas i bakgrundsläget med en
uppmaning att använda rätt miljövariabel; de kan då ärvas av workern utan att
sparas. Kommandot skriver jobb-ID och loggsökväg. `status` visar aktivt jobb
eller ett jobb valt med `--job-id`. `log --follow` fortsätter tills jobbet når
ett terminalt tillstånd. `cancel` begär kontrollerad avbrytning och kan också
riktas med `--job-id`.

Direktkörning och bakgrundskörning ska ge samma operationella resultat.
Skillnaden är endast var status och output presenteras.

## Jobbmodell i SQLite

All SQL ägs av `src/db.py`, i linje med projektets befintliga design. Schemat
versionshöjs och en migration skapar `admin_jobs` utan att ändra befintliga
tabeller.

Tabellen innehåller:

```sql
CREATE TABLE admin_jobs (
    id                  TEXT PRIMARY KEY,
    operation           TEXT NOT NULL,
    params_json         TEXT NOT NULL,
    status              TEXT NOT NULL,
    active_slot         INTEGER,
    pid                 INTEGER,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    heartbeat_at        TEXT,
    finished_at         TEXT,
    current_step        TEXT,
    completed_units     INTEGER NOT NULL DEFAULT 0,
    total_units         INTEGER,
    message             TEXT,
    log_path            TEXT NOT NULL,
    exit_code           INTEGER,
    error               TEXT,
    cancel_requested_at TEXT,
    CHECK (active_slot IS NULL OR active_slot = 1),
    CHECK (status IN (
        'queued', 'running', 'cancel_requested',
        'succeeded', 'failed', 'cancelled', 'interrupted'
    ))
);

CREATE UNIQUE INDEX admin_jobs_one_active
ON admin_jobs(active_slot)
WHERE active_slot = 1;
```

`queued`, `running` och `cancel_requested` har `active_slot = 1`. Alla terminala
tillstånd har `active_slot = NULL`. Den unika partiella indexeringen gör
ett-jobbsregeln atomisk även om CLI och admin försöker starta samtidigt.

`src/db.py` exponerar CRUD och tillståndsövergångar; konsumenter skriver inte
egen SQL. Uppdatering till terminalt tillstånd och frigörande av
`active_slot` sker i samma transaktion.

Tillåtna tillstånd:

```text
queued -> running -> succeeded
                  -> failed
                  -> cancel_requested -> cancelled
queued -> failed
queued/running/cancel_requested -> interrupted
```

Jobbparametrar lagras som normaliserad JSON. Parametrar markerade `secret`
skrivs aldrig till JSON eller logg.

## Worker och livscykel

`src/operations/job_service.py` skapar jobbraden och startar en fristående
worker med `start_new_session=True`. Workern startas med projektets aktuella
Python-interpreter och endast jobb-ID som argument. Om processen inte kan
startas markeras jobbraden `failed` och låset frigörs.

`src/operations/worker.py`:

1. läser operation och parametrar från SQLite,
2. byter atomiskt `queued` till `running` och sparar PID,
3. startar en separat heartbeat-loop,
4. kör operationen med jobb-context,
5. skriver ett terminalt tillstånd i `finally`,
6. stänger logg och databasanslutningar.

Heartbeat skrivs minst var femte sekund, även när ett externt verktyg arbetar
utan ny output. Admin och CLI betraktar ett aktivt jobb med färsk heartbeat som
igång. Om heartbeat är gammal och processen inte finns markeras jobbet
`interrupted`. Ett gammalt jobb signaleras aldrig enbart utifrån ett sparat
PID; signalering kräver färsk heartbeat för att minska risken att ett återanvänt
PID träffas.

Om bara Streamlit eller webbläsaren stängs fortsätter workern. Nästa admin-
eller CLI-anrop läser samma rad och logg och visar aktuell progress. Om hela
datorn startas om blir jobbet `interrupted`; användaren startar sedan samma
idempotenta operation på nytt.

### Progress och logg

Varje jobb får `generated/admin_jobs/<job-id>.log`. Loggen är append-only och
innehåller tidsstämplad output från Python och externa verktyg. Admin visar
slutet av filen och CLI kan följa den.

Strukturerad progress i SQLite består av aktuellt steg, genomförda enheter,
totala enheter och senaste meddelande. Operationer rapporterar dokument eller
sidor när totalen är känd. Ett steg utan känd total använder
`total_units = NULL` och visas som pågående utan falsk procentsiffra.

`run_pipeline` rapporterar både huvudsteg och underoperationens dokument- eller
sidprogress. Nuvarande resume-beteende bevaras: OCR och ingest körs även om
nedladdningen inte hittar nya PDF:er.

### Kontrollerad avbrytning

Admin och `scripts/jobs.py cancel` anropar samma `request_cancel()`:

1. status ändras atomiskt till `cancel_requested`,
2. workern ser begäran via heartbeat-loopen och signalhanteraren,
3. externa barnprocesser får `SIGTERM` som processgrupp,
4. Python-loopar kontrollerar `context.check_cancelled()` mellan dokument,
5. efter en begränsad grace-period används `SIGKILL` för kvarvarande barn,
6. workern markerar jobbet `cancelled` och frigör låset.

Ctrl-C vid direkt CLI-körning går genom samma avbrytningsmekanism och ger
exitkod 130. Redan committade per-dokumentresultat behålls; pågående operation
ska inte markeras som lyckad.

## Migrering av produktionsflöden

### Tunna wrappers

Script som redan bara aktiverar `.venv`, sätter projektrot och anropar en
Python-modul blir tunna Python-entrypoints. Domänmodulernas `main()` delas vid
behov upp i argumentparsning och en anropsbar funktion så admin inte behöver
gå via `subprocess`.

Detta gäller bland annat download, WPU-download, quality, normalize, ingest,
merge, LLM-korrigering och de två LLM-extraktorerna.

### OCR-orkestrering

Shell-logiken i `ocr_tesseract.sh`, `ocr.sh` och `detect_redactions.sh` flyttas
till fokuserade moduler under `src/operations/`:

- Tesseract-kön, textlagerkontroll, språkdetektering, retry-varianter,
  state-markering och slutkontroll.
- Surya-fallback för Tesseract-fel.
- WPU-ordning, merge, redaktionsdetektering, normalisering, kvalitet och
  Surya-redo.
- Redo per fil och per sida, inklusive `--from-list` och reset av relevant
  state.
- Parallell körning med `concurrent.futures` eller explicita Python-processer,
  aldrig `xargs` eller `bash -c`.

Alla befintliga flaggor och standardvärden bevaras om de fortfarande beskriver
ett giltigt flöde. Inline-Python och inline-SQL i shell-script ersätts av
anropsbara funktioner; all SQLite-access flyttas till `src/db.py`.

### Externa verktyg

`ocrmypdf`, `pdftotext`, `podman`, `brew` och andra systemverktyg körs genom
`OperationContext.run_process()` med argumentlistor, explicit arbetskatalog och
kontrollerad miljö. Ingen implementation använder `shell=True`.

`scripts/setup_tessdata.py` använder Python för filhantering och nedladdning.
`scripts/install.py` anropar Homebrew, venv och pip som explicita processer.
`scripts/web.py` ersätter processen med `.venv/bin/streamlit` via Python.
`scripts/test.py` kör pytest och valfritt Ruff/mypy med befintlig
`--static`-semantik.

## Adminsida

`src/pages/8_Admin.py` är en tunn Streamlit-sida. Ren formattering och
formulärbyggning placeras i `src/admin_ui.py` så att beteendet kan testas utan
att produktionsoperationer körs.

Sidan har följande delar:

1. **Aktivt jobb** — operation, status, starttid, körtid, heartbeat, steg,
   progress, senaste meddelande, logg och Avbryt.
2. **Jobbhistorik** — lyckade, misslyckade, avbrutna och `interrupted` jobb
   med möjlighet att visa parametrar och logg.
3. **Pipeline** — full pipeline samt separat Palme- och WPU-download.
4. **OCR** — full OCR, Tesseract, Surya-fallback/redo, riktad sid-OCR,
   redaktionsdetektering, merge och normalisering.
5. **Kvalitet** — kvalitetsbedömning, LLM-korrigering och användarordlista.
6. **Index** — LanceDB-ingest.
7. **Extraktion och graf** — kartobservationer, entiteter, Neo4j
   start/stop/status och laddning av grafdata.
8. **LLM-inställningar** — samma provider-, modell- och URL-konfiguration som
   `scripts/llm_config.py`.

Jobbpanelen ligger överst och uppdateras med ett tidsstyrt Streamlit-fragment
medan ett jobb är aktivt. Projektets web-extra anger en Streamlit-version som
stöder `st.fragment(run_every=...)`; den nuvarande utvecklingsmiljön har
Streamlit 1.57.0. Om inget jobb körs görs ingen periodisk omkörning.

När ett aktivt skrivande jobb finns är alla startknappar för andra skrivande
operationer avstängda. Jobbstatus, logg och Neo4j-status är fortfarande
tillgängliga. Neo4j start, stop och load är skrivande operationer och följer
samma lås.

API-nycklar visas eller lagras inte på adminsidan. Sidan visar endast om
relevanta miljövariabler är tillgängliga. LLM-provider, modell och base URL kan
sparas via befintlig konfigurationsmodul.

## Felhantering

- Ogiltiga parametrar avvisas innan en jobbrad skapas.
- Saknade program eller beroenden ger ett tydligt fel med operation och
  programnamn.
- Ett obligatoriskt pipeline-steg som misslyckas stoppar efterföljande steg.
- Flöden som idag fortsätter efter enskilda dokumentfel behåller det beteendet
  och sammanfattar felen i loggen.
- Ett undantag i workern markerar jobbet `failed`, sparar en kort felsammanfattning
  i SQLite och traceback i loggen.
- Ett jobb får aldrig terminal status `succeeded` om cancel har begärts.
- En loggfil får inte innehålla API-nycklar, OAuth-token, Neo4j-lösenord eller
  andra hemligheter. Processmiljö och kommandologg redigeras innan utskrift.
- Neo4j-lösenordet fortsätter lagras i `neo4j/.password` med mode 600 och visas
  inte i adminloggen.

## Teststrategi

Implementation sker testdrivet.

### Databas och jobblivscykel

- Schema och migration från en äldre fixture.
- Skapa, starta, uppdatera progress och avsluta jobb.
- Endast ett aktivt jobb trots två konkurrerande startförsök.
- Giltiga och ogiltiga tillståndsövergångar.
- `active_slot` frigörs atomiskt vid alla terminala tillstånd.
- Färsk heartbeat identifieras som pågående; gammal heartbeat utan process
  blir `interrupted`.
- Hemliga parametrar serialiseras inte.

### Worker, CLI och avbrytning

- En falsk operation fortsätter efter att den startande processen avslutats.
- `status`, `list`, `log`, `log --follow` och `cancel` fungerar mot temporär
  databas och loggkatalog.
- Ctrl-C och cancel terminerar en falsk processgrupp och ger `cancelled`/130.
- Ett undantag ger `failed`, traceback i logg och frigjort jobb-lås.
- Samma parameterdefinition ger samma typer, defaults och fel i CLI och admin.

### Produktionsoperationer

- `run_pipeline` bevarar ordningen download, valfri WPU, OCR, valfri
  LLM-korrigering, ny kvalitet och ingest.
- OCR och ingest körs även när inga nya filer laddas ner.
- Tesseract-retries, blacklist, felmarkörer och `text_mtime` bevaras.
- Surya-fallback körs före WPU-merge och markeras korrekt vid utebliven text.
- Redo per sida hoppar alla redan försökta `pdf_pages`-rader.
- `--from-list` nollställer exakt de berörda filerna och kör rätt kedja.
- Redaktionsdetektering förfiltrerar redan kontrollerade dokument.
- Neo4j start/stop/status testas mot falskt `podman` och falsk HTTP-kontroll.

### Admin och paketering

- Adminsidan visar ett aktivt jobb efter en ny sidkörning och inaktiverar nya
  skrivande starter.
- Progress, logg och Avbryt visas för aktivt jobb.
- Riskflaggor kräver bekräftelse.
- Endast operationer med `admin_visible=True` renderas.
- Varje borttagen `.sh` har angiven Python-ersättare och dokumenterade
  kommandon refererar inte till shell-filer.

Full verifiering är:

```bash
.venv/bin/python scripts/test.py
.venv/bin/ruff check <alla berörda Python-filer>
.venv/bin/mypy <alla nya operationsmoduler>
.venv/bin/python -m compileall src scripts tests
git diff --check
```

`scripts/test.py --static` körs också som rapporterande kontroll. Om den
fortfarande träffar äldre, orelaterade statiska fel ska dessa redovisas separat
och inte beskrivas som orsakade av migreringen.

## Dokumentation

Samma ändring uppdaterar:

- `README.md` med Admin-fliken och Python-kommandon,
- `docs/kom-igang.md` med installation, pipeline och webbstart via Python,
- `docs/teknisk-referens.md` med alla operationer/flaggor, jobbschema,
  bakgrundslivscykel, loggar, cancel och återanslutning,
- `AGENTS.md` med katalogstruktur, kommandon, jobbinvarianter och borttagna
  shell-referenser,
- `.gitignore` med genererade jobbloggar om katalogen inte redan täcks.

Alla nya kommentarer, docstrings och användartexter skrivs på svenska enligt
projektets konvention.

## Filöversikt

| Fil eller katalog | Roll |
|---|---|
| `src/operations/models.py` | Operations- och parameterdefinitioner |
| `src/operations/context.py` | Progress, logg, subprocesser och cancel |
| `src/operations/registry.py` | Gemensam katalog för CLI och admin |
| `src/operations/job_service.py` | Skapa, starta, inspektera och stoppa jobb |
| `src/operations/worker.py` | Fristående bakgrundsworker och heartbeat |
| `src/operations/pipeline.py` | Full pipeline och stegorkestrering |
| `src/operations/ocr.py` | Full OCR, Surya-fallback och redo |
| `src/operations/tesseract.py` | Tesseract-kö, retries och state |
| `src/operations/detect_redactions.py` | Parallell redaktionsorkestrering |
| `src/operations/neo4j.py` | Lokal Podman/Neo4j-livscykel |
| `src/admin_ui.py` | Testbara adminpresentatörer och formulärhelpers |
| `src/pages/8_Admin.py` | Lokal Streamlit-adminsida |
| `src/db.py` | Jobbschema, migration och all jobb-SQL |
| `scripts/*.py` | Manuella Python-entrypoints |
| `tests/test_admin_jobs.py` | Jobbmodell, worker och cancel |
| `tests/test_operations_*.py` | Pipeline-, OCR- och processregressioner |
| `tests/test_admin_ui.py` | Adminrendering och parameterparitet |

Inga commits eller pushar ingår i implementationen. Användaren kör `/cap`
när ändringen är granskad och redo.
