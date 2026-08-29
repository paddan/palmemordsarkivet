# Python-operationer och lokal adminsida Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Repository instructions require a fresh, scoped subagent prompt for each implementation task plus controller review before integration.

**Goal:** Ersätt samtliga 23 shell-script med manuellt körbara Python-script och lägg till en lokal Streamlit-adminsida med ett beständigt, övervakningsbart och avbrytbart bakgrundsjobb.

**Architecture:** Ett nytt `operations`-paket äger gemensamma operationsdefinitioner, progress, subprocesshantering och jobblivscykel. Befintliga domänmoduler exponeras som anropsbara funktioner; både `scripts/*.py` och `src/pages/8_Admin.py` använder samma registry. Jobbmetadata lagras versionsstyrt i `state.db`, medan full logg lagras under `generated/admin_jobs/`.

**Tech Stack:** Python 3.11+, argparse, dataclasses, sqlite3/WAL, subprocess/processgrupper, concurrent.futures, Streamlit 1.57+, pytest, Ruff och mypy.

**Spec:** `docs/superpowers/specs/2026-08-28-python-operationer-admin-design.md`

## Global Constraints

- Alla 23 befintliga `.sh`-filer ska vara borttagna när planen är klar.
- Allt som kan startas eller styras från admin ska kunna startas eller styras från CLI med samma operation, parametrar, defaults och validering.
- Endast ett jobb med `active_slot = 1` får finnas; regeln ska upprätthållas atomiskt i SQLite.
- Workern ska överleva Streamlit-reruns, stängd webbläsare och omstart av Streamlit-processen.
- Jobb ska kunna inspekteras och stoppas från både admin och `scripts/jobs.py`.
- Använd aldrig `shell=True`; externa kommandon byggs som argumentlistor.
- Hemligheter får inte skrivas till `params_json`, jobblogg eller journal.
- All SQLite-access ligger i `src/db.py`; operations- och UI-kod skriver ingen egen SQL.
- Nya kommentarer, docstrings, CLI-hjälptexter och UI-texter skrivs på svenska.
- Behåll befintlig idempotens, exitkod 2 för argumentfel och exitkod 130 för avbrytning.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md` och `AGENTS.md` i samma arbetskopia.
- Skapa inga commits och pusha ingenting. Varje task slutar med ett verifierat diff-checkpoint som huvudagenten granskar.

## File Structure

### Nya grundmoduler

- `src/operations/models.py` — parameter-, operations- och progressmodeller.
- `src/operations/registry.py` — enda katalogen över operationer.
- `src/operations/cli.py` — parsergenerering och förgrundskörning.
- `src/operations/context.py` — logg, progress, cancellation och processgrupper.
- `src/operations/job_service.py` — skapa/starta/lista/reconcile/cancel jobb.
- `src/operations/worker.py` — fristående worker och heartbeat.
- `src/operations/adapters.py` — runners för befintliga Python-domänmoduler.
- `src/operations/tesseract.py` — migrerad `ocr_tesseract.sh`-logik.
- `src/operations/ocr.py` — full OCR, redactions, Surya-fallback och redo.
- `src/operations/pipeline.py` — full `run_pipeline`-orkestrering.
- `src/operations/neo4j.py` — lokal Podman/Neo4j-livscykel.
- `src/admin_ui.py` och `src/pages/8_Admin.py` — testbar adminlogik och Streamlit-sida.

### Nya entrypoints och tester

- `scripts/_bootstrap.py`, 23 ersättningsscript och `scripts/jobs.py`.
- `tests/test_operation_registry.py`
- `tests/test_admin_job_db.py`
- `tests/test_operation_context.py`
- `tests/test_admin_jobs.py`
- `tests/test_operation_adapters.py`
- `tests/test_tesseract_operation.py`
- `tests/test_ocr_operation.py`
- `tests/test_neo4j_operation.py`
- `tests/test_admin_ui.py`

## Dependency Order

Tasks 1–4 låser gemensamma kontrakt. Därefter kan Tasks 5, 7 och 9 genomföras parallellt eftersom de har separata huvudfiler. Task 8 kräver Tasks 5 och 7. Task 10 kräver alla operationer. Task 11 kräver Tasks 1–4 och registry från Task 10. Dokumentation och slutverifiering sker sist.

---

### Task 1: Operationsmodeller, registry och förgrunds-CLI

**Files:**
- Create: `src/operations/__init__.py`
- Create: `src/operations/models.py`
- Create: `src/operations/registry.py`
- Create: `src/operations/cli.py`
- Create: `scripts/__init__.py`
- Create: `scripts/_bootstrap.py`
- Modify: `pyproject.toml`
- Test: `tests/test_operation_registry.py`

**Interfaces:**
- Produces: `ParameterDefinition`, `OperationDefinition`, `ProgressUpdate`, `OperationRegistry`, `get_registry()`, `build_operation_parser()`, `parse_operation_args()`, `run_operation_cli()`.
- Consumes: inga nya projektgränssnitt.

- [ ] **Step 1: Skriv RED-tester för parameterparitet och registry**

```python
def test_same_definition_drives_cli_and_admin_metadata():
    definition = OperationDefinition(
        id="sample",
        label="Prov",
        group="Pipeline",
        description="Testoperation",
        parameters=(
            ParameterDefinition("jobs", ("--jobs",), "int", 4, "Antal jobb"),
            ParameterDefinition("mode", ("--mode",), "choice", "pages", "Läge",
                                choices=("pages", "files")),
        ),
        admin_visible=True,
        mutating=True,
        confirmation=None,
        run=lambda context, params: None,
    )
    assert parse_operation_args(definition, ["--jobs", "8", "--mode", "files"]) == {
        "jobs": 8,
        "mode": "files",
    }
    assert definition.parameters[0].default == 4


def test_secret_parameter_is_rejected_for_background_serialization():
    parameter = ParameterDefinition(
        "api_key", ("--api-key",), "str", "", "API-nyckel", secret=True
    )
    with pytest.raises(ValueError, match="miljövariabel"):
        parameter.validate_background_value("hemlig")
```

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_operation_registry.py -q`

Expected: importfel för `operations.models` och `operations.registry`.

- [ ] **Step 3: Implementera modellerna med exakta fält**

```python
ParameterKind = Literal["bool", "int", "float", "str", "path", "choice"]


@dataclass(frozen=True)
class ParameterDefinition:
    name: str
    flags: tuple[str, ...]
    kind: ParameterKind
    default: object
    help: str
    choices: tuple[str, ...] = ()
    required: bool = False
    secret: bool = False


@dataclass(frozen=True)
class ProgressUpdate:
    step: str
    completed: int = 0
    total: int | None = None
    message: str = ""


OperationRunner = Callable[["OperationContext", Mapping[str, object]], None]


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
    run: OperationRunner
```

Implementera `ParameterDefinition.add_to_parser(parser)`, normalisering av
`path` till `Path`, choices-validering och `validate_background_value()` som
avvisar ett icke-tomt hemligt värde.

- [ ] **Step 4: Implementera registry och parsergenerering**

`OperationRegistry.register()` ska vägra dubbla ID:n. `get()` ska ge ett
svenskt `KeyError` för okänt ID. `admin_operations()` returnerar endast
`admin_visible=True`, sorterat på `(group, label)`.

`run_operation_cli(operation_id, argv)` ska skapa en terminal-context genom en
sen import från `operations.context`, köra definitionen och returnera 0, 2
eller 130 utan att mutera `sys.argv`.

- [ ] **Step 5: Lägg till bootstrap och paketering**

`scripts/_bootstrap.py` ska lägga `<root>/src` först i `sys.path` och exponera
`run(operation_id)`. Lägg till `operations*` i
`[tool.setuptools.packages.find].include`.

- [ ] **Step 6: Verifiera Task 1 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_operation_registry.py tests/test_packaging.py -q
.venv/bin/ruff check src/operations scripts tests/test_operation_registry.py
.venv/bin/python -m compileall -q src/operations scripts
git diff --check
```

Huvudagentens checkpoint: modellerna får inte importera Streamlit eller `db`.

---

### Task 2: Versionsstyrd jobbmodell i SQLite

**Files:**
- Modify: `src/db.py`
- Create: `tests/fixtures/state_db_v6.sql`
- Create: `tests/test_admin_job_db.py`

**Interfaces:**
- Produces: `create_admin_job()`, `claim_admin_job()`, `get_admin_job()`, `get_active_admin_job()`, `list_admin_jobs()`, `update_admin_job_progress()`, `heartbeat_admin_job()`, `request_admin_job_cancel()`, `finish_admin_job()`, `mark_admin_job_interrupted()`.
- Consumes: `db.connect()`, `db.init_schema()`, `db.now()`.

- [ ] **Step 1: Skriv RED-tester för migration, lås och transitions**

Testet ska skapa två jobb mot samma temporära databas:

```python
first = db.create_admin_job(
    conn,
    job_id="job-1",
    operation="run-pipeline",
    params_json='{"jobs":4}',
    log_path="generated/admin_jobs/job-1.log",
)
assert first["status"] == "queued"

with pytest.raises(db.ActiveAdminJobError):
    db.create_admin_job(
        conn,
        job_id="job-2",
        operation="ingest",
        params_json="{}",
        log_path="generated/admin_jobs/job-2.log",
    )

assert db.claim_admin_job(conn, "job-1", pid=1234)
db.update_admin_job_progress(
    conn, "job-1", step="OCR", completed=3, total=10, message="Tre klara"
)
db.finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
assert db.get_admin_job(conn, "job-1")["active_slot"] is None
```

Lägg även test för att fixture v6 migreras till v7 med befintlig data kvar.

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_admin_job_db.py -q`

Expected: saknat `admin_jobs`-schema och saknade CRUD-funktioner.

- [ ] **Step 3: Höj schema till version 7 och lägg till tabellen**

Använd exakt statusmängden från specen och ett partiellt unikt index på
`active_slot`. `_migration_007_admin_jobs()` ska skapa tabell och index
idempotent. Färsk `SCHEMA_SQL` ska innehålla samma definition.

- [ ] **Step 4: Implementera atomiska CRUD-funktioner**

`create_admin_job()` ska fånga `sqlite3.IntegrityError` från active-slot-indexet
och kasta `ActiveAdminJobError` med det aktiva jobbets ID. Alla transitions ska
använda villkorad `UPDATE ... WHERE status IN (...)`; noll `rowcount` ger
`InvalidAdminJobTransition`.

`finish_admin_job()` accepterar endast `succeeded`, `failed`, `cancelled` eller
`interrupted` och sätter `active_slot=NULL` i samma UPDATE.

- [ ] **Step 5: Verifiera Task 2 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_admin_job_db.py tests/test_db.py -q
.venv/bin/ruff check src/db.py tests/test_admin_job_db.py
.venv/bin/python -m compileall -q src/db.py tests/test_admin_job_db.py
git diff --check
```

Huvudagentens checkpoint: inga jobbfrågor får ligga utanför `src/db.py`.

---

### Task 3: OperationContext, processgrupper och kontrollerad avbrytning

**Files:**
- Create: `src/operations/context.py`
- Create: `src/operations/exceptions.py`
- Create: `tests/test_operation_context.py`

**Interfaces:**
- Produces: `OperationCancelled`, `OperationFailed`, `ProgressSink`, `TerminalSink`, `OperationContext`, `OperationContext.run_process()`.
- Consumes: `ProgressUpdate` från Task 1.

- [ ] **Step 1: Skriv RED-tester med en riktig barnprocess**

Använd `sys.executable -c` i stället för shell:

```python
def test_run_process_streams_output(tmp_path):
    messages = []
    context = OperationContext(
        sink=RecordingSink(messages),
        cancel_requested=lambda: False,
    )
    rc = context.run_process(
        [sys.executable, "-c", "print('rad ett'); print('rad två')"],
        cwd=tmp_path,
    )
    assert rc == 0
    assert messages == ["rad ett", "rad två"]


def test_cancel_terminates_process_group(tmp_path):
    cancel = threading.Event()
    context = OperationContext(
        sink=RecordingSink([]),
        cancel_requested=cancel.is_set,
        terminate_grace_seconds=0.2,
    )
    # Starta run_process i en tråd, sätt cancel och verifiera OperationCancelled.
```

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_operation_context.py -q`

Expected: saknade context- och exceptionklasser.

- [ ] **Step 3: Implementera sink och context**

`ProgressSink` ska ha `write_log(message, level)` och `write_progress(update)`.
`TerminalSink` skriver progress/logg till givna textströmmar. `OperationContext`
ska exponera `step()`, `progress()`, `log()` och `check_cancelled()`.

- [ ] **Step 4: Implementera processkörning utan shell**

Starta externa processer med `start_new_session=True`, stdout sammanslagen med
stderr och textläge. En läsartråd lägger rader i en `queue.Queue`; huvudloopen
poller process och cancel. Vid cancel:

```python
os.killpg(process.pid, signal.SIGTERM)
# vänta terminate_grace_seconds
os.killpg(process.pid, signal.SIGKILL)  # endast om processen lever
raise OperationCancelled("Operationen avbröts")
```

Maskera värden vars env-nyckel slutar med `_KEY`, `_TOKEN` eller `_PASSWORD`
när argv/env loggas.

- [ ] **Step 5: Verifiera Task 3 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_operation_context.py -q
.venv/bin/ruff check src/operations/context.py src/operations/exceptions.py tests/test_operation_context.py
.venv/bin/mypy src/operations/context.py src/operations/exceptions.py
git diff --check
```

Huvudagentens checkpoint: testprocesser ska vara avslutade efter testkörningen.

---

### Task 4: Jobbservice, fristående worker och jobb-CLI

**Files:**
- Create: `src/operations/job_service.py`
- Create: `src/operations/worker.py`
- Create: `scripts/jobs.py`
- Create: `tests/test_admin_jobs.py`

**Interfaces:**
- Produces: `start_job()`, `run_job()`, `reconcile_active_job()`, `cancel_job()`, `read_log_tail()`, `follow_log()`, `main()` i worker och jobs-script.
- Consumes: Tasks 1–3 samt jobb-CRUD i `src/db.py`.

- [ ] **Step 1: Skriv RED-tester för start, heartbeat, reconcile och CLI**

Testa `run_job()` med injicerad registry och temporär databas:

```python
def fake_runner(context, params):
    context.step("Prov", total=2)
    context.progress(1, 2, "Halvvägs")
    context.progress(2, 2, "Klar")

run_job(job_id, db_path=db_path, registry=registry, heartbeat_interval=0.01)
row = db.get_admin_job(conn, job_id)
assert row["status"] == "succeeded"
assert row["completed_units"] == 2
```

Testa också `jobs.py status`, `list`, `log`, `log --follow` och `cancel` genom
att anropa dess `main(argv, ...)` med injicerade strömmar.

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_admin_jobs.py -q`

Expected: saknade jobbservice- och workerfunktioner.

- [ ] **Step 3: Implementera JobSink och worker**

`JobSink.write_progress()` anropar enbart `db.update_admin_job_progress()`.
`write_log()` appendar en tidsstämplad rad till jobbets loggfil. Heartbeat-
tråden använder en egen SQLite-anslutning var femte sekund och sätter en
lokal cancel-event när status är `cancel_requested`.

`run_job()` ska mappa resultat exakt:

```python
try:
    definition.run(context, params)
except OperationCancelled:
    db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)
except BaseException as exc:
    sink.write_traceback(exc)
    db.finish_admin_job(conn, job_id, status="failed", exit_code=1, error=str(exc))
else:
    if db.get_admin_job(conn, job_id)["status"] == "cancel_requested":
        db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)
    else:
        db.finish_admin_job(conn, job_id, status="succeeded", exit_code=0)
```

- [ ] **Step 4: Implementera detached start och säker reconcile**

`start_job()` validerar parametrar, skapar UUID/logg, infogar jobbraden och
startar:

```python
[sys.executable, "-m", "operations.worker", "--job-id", job_id]
```

med `STATE_DB` och `ADMIN_JOB_LOG_ROOT` i en kopierad env,
`start_new_session=True` och stdio till `DEVNULL`. Vid Popen-fel markeras jobbet
`failed`.

`reconcile_active_job()` markerar bara `interrupted` när heartbeat är äldre
än 15 sekunder och `os.kill(pid, 0)` visar att processen saknas. Ett jobb med
gammal heartbeat men existerande PID får varningsmeddelande men behåller låset.

- [ ] **Step 5: Implementera cancel och jobb-CLI**

`cancel_job()` sätter `cancel_requested`. Om heartbeat är färsk skickas
`SIGTERM` till workerns PID; signalhanteraren ska endast sätta cancel-event.
Workern avslutar sina barn genom `OperationContext` och skriver slutstatus.

`scripts/jobs.py start OPERATION ...` gör tvåstegsparsning: parse `start` och
operation-ID först, sedan `parse_operation_args()` på resten.

- [ ] **Step 6: Verifiera Task 4 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_admin_job_db.py tests/test_operation_context.py tests/test_admin_jobs.py -q
.venv/bin/ruff check src/operations scripts/jobs.py tests/test_admin_jobs.py
.venv/bin/mypy src/operations/job_service.py src/operations/worker.py
git diff --check
```

Huvudagentens checkpoint: ett testjobb ska kunna observeras efter att
startprocessen avslutats, och inga zombieprocesser får finnas.

---

### Task 5: Anropsbara adapters för fil-, kvalitets- och mergeoperationer

**Files:**
- Create: `src/operations/adapters.py`
- Modify: `src/download.py`
- Modify: `src/download_wpu.py`
- Modify: `src/quality.py`
- Modify: `src/normalize_text.py`
- Modify: `src/merge_pages.py`
- Modify: `src/merge_wpu.py`
- Modify: `src/build_user_words.py`
- Test: `tests/test_operation_adapters.py`
- Test: befintliga modulstester för ovanstående filer

**Interfaces:**
- Produces: `run_download()`, `run_download_wpu()`, `run_quality()`, `run_normalize()`, `run_merge_pages()`, `run_merge_wpu()`, `run_build_user_words()`.
- Consumes: `OperationContext` från Task 3.

- [ ] **Step 1: Skriv RED-tester som kräver anropsbara funktioner**

Varje modul ska få `main(argv: list[str] | None = None)` samt en `run_*` som tar
explicita keyword-parametrar och valfri context. Exempel:

```python
def test_normalize_runner_reports_each_changed_file(tmp_path, recording_context):
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / "a.txt").write_text("A\u00ad  B", encoding="utf-8")
    changed = normalize_text.run_normalize(
        root=tmp_path,
        txt_dir=text_dir,
        dry_run=False,
        stats=False,
        rebuild=True,
        files_from=None,
        context=recording_context,
    )
    assert changed == 1
    assert recording_context.last_progress.completed == 1
```

- [ ] **Step 2: Kör adaptertesterna och verifiera RED**

Run: `.venv/bin/pytest tests/test_operation_adapters.py -q`

- [ ] **Step 3: Extrahera run-funktioner utan att ändra algoritmer**

Använd dessa exakta signaturfamiljer:

```python
run_download(*, out: Path, sheet_id: str, limit: int,
             context: OperationContext | None = None) -> int
run_download_wpu(*, out: Path, dry_run: bool, limit: int | None, rebuild: bool,
                 context: OperationContext | None = None) -> int
run_quality(*, top: int | None, limit: int | None, per_page: bool,
            text_dir: Path, files_dir: Path, rebuild: bool,
            files_from: Path | None,
            context: OperationContext | None = None) -> int
run_normalize(*, root: Path, txt_dir: Path, dry_run: bool, stats: bool,
              rebuild: bool, files_from: Path | None,
              context: OperationContext | None = None) -> int
run_merge_pages(*, stem: str | None, merge_all: bool, txt_dir: Path,
                context: OperationContext | None = None) -> int
run_merge_wpu(*, dry_run: bool, rebuild: bool, margin: float,
              wpu_dir: Path, text_dir: Path, ocr_dir: Path, jobs: int,
              context: OperationContext | None = None) -> int
run_build_user_words(*, text_dir: Path, out: Path, user_words: Path,
                     min_freq: int, rebuild: bool,
                     context: OperationContext | None = None) -> int
```

`main()` ska endast bygga parser, anropa `run_*` och returnera exitkod.

- [ ] **Step 4: Koppla adapters till samma funktioner**

`src/operations/adapters.py` mappar registry-parametrar till signaturerna ovan
och skapar en terminal-context endast om caller inte skickat context. Ingen
adapter får anropa modulernas `main()`.

- [ ] **Step 5: Verifiera Task 5 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_download.py tests/test_download_wpu.py tests/test_quality.py tests/test_normalize_text.py tests/test_merge_pages.py tests/test_merge_wpu.py tests/test_operation_adapters.py -q
.venv/bin/ruff check src/download.py src/download_wpu.py src/quality.py src/normalize_text.py src/merge_pages.py src/merge_wpu.py src/build_user_words.py src/operations/adapters.py tests/test_operation_adapters.py
git diff --check
```

Huvudagentens checkpoint: befintliga `main()`-anrop och return codes ska vara
bakåtkompatibla på Python-nivå.

---

### Task 6: Anropsbara adapters för ingest, LLM, sid-OCR och grafdata

**Files:**
- Modify: `src/rag/ingest.py`
- Modify: `src/llm_correct.py`
- Modify: `src/ocr_pages.py`
- Modify: `src/extract_map_observations.py`
- Modify: `src/graph/extract_entities.py`
- Modify: `src/graph/load_neo4j.py`
- Modify: `src/llm_config_cli.py`
- Modify: `src/operations/adapters.py`
- Test: `tests/test_operation_adapters.py`
- Test: befintliga tester för dessa moduler

**Interfaces:**
- Produces: `run_ingest()`, `run_llm_correct()`, `run_ocr_pages()`, `run_extract_map_observations()`, `run_extract_entities()`, `run_load_graph()`, `run_llm_config()`.
- Consumes: Tasks 1, 3 och 5:s adaptermodul.

- [ ] **Step 1: Skriv RED-tester för varje ny run-funktion**

LLM-tester ska injicera befintliga falska klienter och aldrig göra nätanrop.
Ingest-test ska använda befintliga LanceDB-fixtures. `run_llm_config()` ska
testas med temporär config-path och `reset=True`.

- [ ] **Step 2: Kör de riktade testerna och verifiera RED**

Run:

```bash
.venv/bin/pytest tests/test_reingest.py tests/test_llm_correct.py tests/test_extract_map_observations.py tests/test_extract_entities.py tests/test_load_neo4j.py tests/test_llm_config_cli.py tests/test_operation_adapters.py -q
```

- [ ] **Step 3: Extrahera run-funktioner med context-progress**

Varje dokumentloop anropar `context.check_cancelled()` före nästa dokument
och `context.progress(done, total, label)` efter dokumentet. Async LLM-loopar
kontrollerar cancel mellan färdiga futures/tasks.

`api_key` finns kvar som direkt-CLI-parameter men markeras `secret=True` i
registry; bakgrundskörning använder befintliga miljövariabler.

- [ ] **Step 4: Bevara `main()` som tunn parser**

Alla sju moduler ska ha `main(argv: list[str] | None = None) -> int` eller
behålla dokumenterad `None` endast där testkontraktet kräver det. Parsern ska
inte innehålla arbetsloopar efter refaktorn.

- [ ] **Step 5: Verifiera Task 6 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_ask.py tests/test_reingest.py tests/test_llm_correct.py tests/test_ocr_pages.py tests/test_extract_map_observations.py tests/test_extract_entities.py tests/test_load_neo4j.py tests/test_llm_config_cli.py tests/test_operation_adapters.py -q
.venv/bin/ruff check src/rag/ingest.py src/llm_correct.py src/ocr_pages.py src/extract_map_observations.py src/graph/extract_entities.py src/graph/load_neo4j.py src/llm_config_cli.py src/operations/adapters.py
git diff --check
```

Huvudagentens checkpoint: inga API-nycklar får ingå i progress eller exceptions.

---

### Task 7: Migrera `ocr_tesseract.sh` till Python

**Files:**
- Create: `src/operations/tesseract.py`
- Create: `tests/test_tesseract_operation.py`
- Modify: `src/errors_log.py` endast om context kräver injicerbar loggpath
- Consume: befintliga `src/db.py`- och `src/ocr_db_helper.py`-beteenden

**Interfaces:**
- Produces: `TesseractOptions`, `detect_language()`, `text_quality_ok()`, `build_ocr_attempts()`, `process_pdf()`, `run_tesseract()`.
- Consumes: `OperationContext`, `db`-helpers och `errors_log.log_error()`.

- [ ] **Step 1: Skriv RED-enhetstester för inline-shelllogiken**

```python
def test_text_quality_rejects_ocr_garbage():
    assert not text_quality_ok("1 2 3 x y z 7a 8b " * 50)


def test_build_attempts_drops_clean_then_deskew():
    attempts = build_ocr_attempts(mode="skip", common=["--rotate-pages"])
    assert "--clean" in attempts[0]
    assert "--clean" not in attempts[1]
    assert "--deskew" not in attempts[2]


def test_completed_or_blacklisted_pdf_is_not_scheduled(tmp_state_db, pdf_path):
    # Markera stem klar/blacklistad via db.py och verifiera tom kandidatkö.
```

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_tesseract_operation.py -q`

- [ ] **Step 3: Implementera options och rena helpers**

`TesseractOptions` ska motsvara samtliga nuvarande flaggor: root, in/ocr/txt,
tessdata, user words, config, psm, langs, jobs, per-file-jobs, min-text-chars,
image-dpi, errors-log, files-from, retry-failed och retry-blacklist.

Flytta språkdetektering och kvalitetsheuristik från inline-Python ordagrant i
beteende, men som normala Python-funktioner.

- [ ] **Step 4: Implementera per-PDF-flödet med falskbar processrunner**

`process_pdf()` ska:

1. kontrollera done/blacklist/failed i `db.py`,
2. extrahera råtext med `pdftotext`,
3. välja `swe`, `eng` eller `swe+eng`,
4. kopiera gott textlager eller köra tre OCRmyPDF-försök,
5. skriva layout-text,
6. markera done/failed och `text_mtime`,
7. returnera en typad `TesseractResult(stem, status, error)`.

- [ ] **Step 5: Implementera parallell kö och slutkontroll**

Använd `ThreadPoolExecutor(max_workers=jobs)` eftersom arbetet sker i externa
processer. Huvudtråden äger progress och avbrytning. `files_from` ska bevara
ordning och trimma `.txt`. Full körning ska göra samma missing/failed/
blacklisted/merged-away-kontroll som shell-scriptet.

- [ ] **Step 6: Verifiera Task 7 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_tesseract_operation.py tests/test_db.py -q
.venv/bin/ruff check src/operations/tesseract.py tests/test_tesseract_operation.py
.venv/bin/mypy src/operations/tesseract.py
git diff --check
```

Huvudagentens checkpoint: alla OCRmyPDF-argument och retryordningen ska jämföras
rad för rad med `ocr_tesseract.sh` innan shell-filen tas bort.

---

### Task 8: Migrera redactions, full OCR och full pipeline

**Files:**
- Create: `src/operations/detect_redactions.py`
- Create: `src/operations/ocr.py`
- Create: `src/operations/pipeline.py`
- Create: `tests/test_ocr_operation.py`
- Modify: `tests/test_scripts.py`

**Interfaces:**
- Produces: `run_detect_redactions()`, `run_surya_fallback()`, `run_redo_pages()`, `run_redo_files()`, `run_ocr()`, `run_pipeline()`.
- Consumes: Tasks 3, 5–7 och befintliga `db`, `merge_pages`, `normalize_text`, `ocr_pages`.

- [ ] **Step 1: Ersätt textinspektions-tester med beteende-RED-tester**

Radera tester som splittrar text ur `ocr.sh`/`run_pipeline.sh`. Skriv i stället
injekterade operationer:

```python
def test_pipeline_resumes_ocr_and_ingest_without_downloads(recording_context):
    calls = []
    deps = PipelineDependencies(
        download=lambda **kwargs: calls.append("download") or 0,
        download_wpu=lambda **kwargs: calls.append("download_wpu") or 0,
        ocr=lambda **kwargs: calls.append("ocr"),
        llm_correct=lambda **kwargs: calls.append("llm_correct"),
        quality=lambda **kwargs: calls.append("quality"),
        ingest=lambda **kwargs: calls.append("ingest"),
        count_pdfs=lambda path: 10,
    )
    run_pipeline(PipelineOptions(), recording_context, deps=deps)
    assert calls == ["download", "download_wpu", "ocr", "ingest"]
```

Lägg separata tester för LLM → quality → ingest, Surya före WPU-merge,
skip-redo, fallback-only, redo pages och `--from-list`.

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_ocr_operation.py tests/test_scripts.py -q`

- [ ] **Step 3: Implementera redaktionsorkestreringen**

Flytta pending-query till `src/db.py` som `list_pending_redaction_stems()` och
reset till `reset_redaction_state()`. `run_detect_redactions()` ska regenerera
text vid `rebuild_text`, förfiltrera kandidater och köra `ocr_pages.detect_redactions_file`
parallellt utan `xargs`.

- [ ] **Step 4: Implementera Surya-fallback och redo pages/files**

Bevara dessa invarianter i kod och tester:

- fallback väljer endast Tesseract-fel/blacklist med tom text och
  `surya_failed_at IS NULL`,
- lyckad fallback merge:ar, normaliserar och rensar OCR-fel,
- misslyckad fallback markerar både Surya-fel och blacklist,
- redo pages hoppar varje befintlig `pdf_pages`-rad oavsett engine,
- lyckad sid-redo merge:ar text och stämplar normalisering/mtime,
- redo files bevarar threshold/source och `--from-list`-reset.

- [ ] **Step 5: Implementera full `run_ocr()` med direkta Python-anrop**

Använd en `OcrDependencies`-dataclass för testinjektion. Standarddependencies
pekar på riktiga run-funktioner. Kör exakt Tesseract → fallback → WPU
Tesseract → fallback → merge WPU → redactions → normalize → quality
→ Surya pages → quality.

- [ ] **Step 6: Implementera full `run_pipeline()`**

`PipelineOptions` ska ha skip_wpu, skip_redo, with_llm, jobs och test_limit.
Räkna PDF:er före/efter download enbart för rapportering. Kör alltid OCR
och ingest. Exitkod 2 från download behandlas som nuvarande "inget att hämta";
andra fel stoppar.

- [ ] **Step 7: Verifiera Task 8 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_ocr_operation.py tests/test_scripts.py tests/test_detect_redactions.py tests/test_ocr_pages.py tests/test_quality.py tests/test_merge_pages.py tests/test_merge_wpu.py -q
.venv/bin/ruff check src/operations/detect_redactions.py src/operations/ocr.py src/operations/pipeline.py tests/test_ocr_operation.py tests/test_scripts.py
.venv/bin/mypy src/operations/detect_redactions.py src/operations/ocr.py src/operations/pipeline.py
git diff --check
```

Huvudagentens checkpoint: jämför alla flaggor och steg med de tre shell-filerna
innan de markeras redo för borttagning.

---

### Task 9: Neo4j-operation och fristående systemscript

**Files:**
- Create: `src/operations/neo4j.py`
- Create: `scripts/install.py`
- Create: `scripts/setup_tessdata.py`
- Create: `scripts/test.py`
- Create: `scripts/web.py`
- Create: `tests/test_neo4j_operation.py`
- Modify: `tests/test_scripts.py`

**Interfaces:**
- Produces: `neo4j_status()`, `neo4j_start()`, `neo4j_stop()`, samt manuellt körbara Python-ersättare för install/setup/test/web.
- Consumes: `OperationContext` för Neo4j; systemscript ska fungera utan registry när det krävs.

- [ ] **Step 1: Skriv RED-tester med falska exekverbara filer**

Lägg falskt `podman` först i PATH och injicera en HTTP-probe. Verifiera
start av maskin, skapande/omstart av container, status och stop. Lösenordstest
ska verifiera mode `0o600` utan att skriva värdet till loggen.

Testa `scripts/test.py --help`, `scripts/install.py --help` och
`scripts/web.py --help` genom `main(argv)`; inget test ska installera eller
starta Streamlit.

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_neo4j_operation.py tests/test_scripts.py -q`

- [ ] **Step 3: Implementera Neo4j med explicita subprocessargument**

Använd `secrets.token_hex(16)` för första lösenordet, `urllib.request` för
HTTP-probe och `context.run_process()` för Podman. Visa aldrig lösenordet i
jobbloggen eller adminsidan.

- [ ] **Step 4: Implementera de fyra icke-admin-scripten**

- `install.py`: detektera Python 3.11+, kör brew, venv och pip med list-argv,
  anropa `setup_tessdata.main()` direkt och bevara `--no-surya`/`--dev`.
- `setup_tessdata.py`: använd `Path`, symlänkar och `urllib.request.urlretrieve`;
  bevara `--root`/`--dest`.
- `test.py`: välj `.venv/bin/python`, kör pytest och valfritt Ruff/mypy;
  bevara `--static`.
- `web.py`: ladda `.zshrc.local` inte alls; dokumentera att nycklar ska finnas i
  processmiljön och starta `.venv/bin/streamlit run src/Utredning.py` med
  `os.execvpe` och extra argument efter `--`.

- [ ] **Step 5: Verifiera Task 9 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_neo4j_operation.py tests/test_scripts.py -q
.venv/bin/ruff check src/operations/neo4j.py scripts/install.py scripts/setup_tessdata.py scripts/test.py scripts/web.py tests/test_neo4j_operation.py
.venv/bin/python -m compileall -q src/operations/neo4j.py scripts
git diff --check
```

Huvudagentens checkpoint: borttagningen av `.zshrc.local`-sourcing ska vara
synlig i dokumentationen som ett avsiktligt säkerhets-/portabilitetsval.

---

### Task 10: Registrera alla operationer, skapa entrypoints och ta bort shell

**Files:**
- Modify: `src/operations/registry.py`
- Create: samtliga produktionsfiler under `scripts/` enligt specens tabell
- Modify: `tests/test_operation_registry.py`
- Modify: `tests/test_packaging.py`
- Modify: `tests/test_scripts.py`
- Delete: samtliga 23 `*.sh` i projekroten
- Modify: Python-användartexter som fortfarande nämner `.sh`

**Interfaces:**
- Produces: komplett registry och 23 manuellt körbara Python-ersättare.
- Consumes: Tasks 1 och 5–9.

- [ ] **Step 1: Skriv RED-manifesttest för exakt 23 ersättare**

```python
SHELL_TO_PYTHON = {
    "build_user_words.sh": "build_user_words.py",
    "detect_redactions.sh": "detect_redactions.py",
    "download.sh": "download.py",
    "download_wpu.sh": "download_wpu.py",
    "extract_entities.sh": "extract_entities.py",
    "extract_map_observations.sh": "extract_map_observations.py",
    "ingest.sh": "ingest.py",
    "install.sh": "install.py",
    "llm_config.sh": "llm_config.py",
    "llm_correct.sh": "llm_correct.py",
    "load_graph.sh": "load_graph.py",
    "merge_pages.sh": "merge_pages.py",
    "merge_wpu.sh": "merge_wpu.py",
    "neo4j.sh": "neo4j.py",
    "normalize.sh": "normalize.py",
    "ocr.sh": "ocr.py",
    "ocr_pages.sh": "ocr_pages.py",
    "ocr_tesseract.sh": "ocr_tesseract.py",
    "quality.sh": "quality.py",
    "run_pipeline.sh": "run_pipeline.py",
    "setup_tessdata.sh": "setup_tessdata.py",
    "test.sh": "test.py",
    "web.sh": "web.py",
}
```

Testet ska initialt kräva att Python-filen finns och svarar på `--help`; efter
borttagningen ska det också kräva att shell-filen saknas.

- [ ] **Step 2: Registrera alla adminoperationer med exakta defaults**

Registry ska innehålla grupperna Pipeline, OCR, Kvalitet, Index, Extraktion och
graf samt LLM-inställningar. `install`, `setup-tessdata`, `test` och `web` ska
inte vara admin-visible. Neo4j status ska ha `mutating=False`; start/stop/load
ska vara muterande.

- [ ] **Step 3: Skapa tunna entrypoints**

Varje produktionsscript ska bestå av bootstrap, operation-ID och exit:

```python
from _bootstrap import run


if __name__ == "__main__":
    raise SystemExit(run("ingest"))
```

`neo4j.py` och `llm_config.py` kan ha subkommandoparsning men ska fortfarande
slå upp samma registrydefinitioner som admin.

- [ ] **Step 4: Byt alla Python-runtime-meddelanden från shell till Python**

Uppdatera docstrings och feltexter i `src/Utredning.py`, `src/pages/3_Graf.py`,
`src/llm_correct.py`, `src/rag/ingest.py`, `src/quality.py`, `src/ocr_pages.py`,
`src/db.py`, `src/graph/viz.py` och `src/llm_config_cli.py` till
`python scripts/...`.

- [ ] **Step 5: Ta bort de 23 shell-filerna**

Ta bort endast filerna i manifestet. Kontrollera med:

```bash
test -z "$(rg --files -g '*.sh' -g '!graphify-out/**')"
```

- [ ] **Step 6: Verifiera Task 10 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_operation_registry.py tests/test_packaging.py tests/test_scripts.py -q
.venv/bin/python scripts/run_pipeline.py --help
.venv/bin/python scripts/ocr.py --help
.venv/bin/python scripts/jobs.py --help
.venv/bin/python scripts/neo4j.py status
rg -n '\./[A-Za-z0-9_-]+\.sh\b' src scripts tests pyproject.toml
git diff --check
```

Expected: sista `rg` ger inga aktiva runtime- eller testkommandon till
borttagna shell-script; manifeststrängar utan `./` och historiska designspecar
omfattas inte av kontrollen.

---

### Task 11: Lokal Streamlit-adminsida

**Files:**
- Create: `src/admin_ui.py`
- Create: `src/pages/8_Admin.py`
- Create: `tests/test_admin_ui.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `group_admin_operations()`, `format_job_status()`, `progress_fraction()`, `render_operation_form()`, `render_active_job()`, Admin-sidan.
- Consumes: registry och jobbservice från Tasks 1–4 och 10.

- [ ] **Step 1: Skriv RED-tester för ren UI-logik**

```python
def test_progress_fraction_handles_unknown_total():
    assert progress_fraction({"completed_units": 3, "total_units": None}) is None
    assert progress_fraction({"completed_units": 3, "total_units": 10}) == 0.3


def test_only_admin_visible_operations_are_grouped(registry):
    grouped = group_admin_operations(registry)
    ids = {definition.id for values in grouped.values() for definition in values}
    assert "run-pipeline" in ids
    assert "install" not in ids
```

Använd `streamlit.testing.v1.AppTest` för ett separat fixture-page-script som
injicerar temporär `STATE_DB`; verifiera aktivt jobb, progress, logg och
inaktiverad startknapp.

- [ ] **Step 2: Kör testerna och verifiera RED**

Run: `.venv/bin/pytest tests/test_admin_ui.py -q`

- [ ] **Step 3: Implementera rena adminhelpers**

`render_operation_form()` ska rendera widget efter `ParameterDefinition.kind`,
använda definitionens default och inte rendera `secret=True`. Vid riskoperation
måste checkboxen med `confirmation` vara sann innan startknappen aktiveras.

- [ ] **Step 4: Implementera aktiv jobbpanel med automatisk refresh**

Skapa bara ett tidsstyrt fragment när den första statusläsningen hittar ett
aktivt jobb:

```python
initial_job = reconcile_active_job()
if initial_job is None:
    st.caption("Inget jobb körs.")
else:
    @st.fragment(run_every=2.0)
    def active_job_fragment() -> None:
        job = reconcile_active_job()
        if job is None or job["status"] in TERMINAL_JOB_STATUSES:
            st.rerun(scope="app")
        render_active_job(job)

    active_job_fragment()
```

Visa status, operation, starttid, elapsed, heartbeat, steg, determinate eller
indeterminate progress, senaste meddelande och slutet av loggen. Avbryt anropar
`cancel_job()` och visar `cancel_requested` direkt.

- [ ] **Step 5: Implementera historik och grupperade formulär**

Visa senaste 50 jobb. När aktivt muterande jobb finns ska startknappar för
muterande operationer vara disabled. Neo4j status och läsning av jobb/logg ska
fungera samtidigt. LLM-sidan visar boolesk tillgänglighet för env-nycklar men
aldrig värden.

- [ ] **Step 6: Sätt explicit Streamlit-version**

Ändra web-extra till `streamlit>=1.57,<2` eftersom implementationen och testerna
använder `st.fragment(run_every=...)` och miljön är verifierad med 1.57.0.

- [ ] **Step 7: Verifiera Task 11 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_admin_ui.py tests/test_operation_registry.py tests/test_admin_jobs.py -q
.venv/bin/ruff check src/admin_ui.py src/pages/8_Admin.py tests/test_admin_ui.py
.venv/bin/python -m compileall -q src/admin_ui.py src/pages/8_Admin.py
git diff --check
```

Huvudagentens checkpoint: inget Streamlit-anrop får finnas i
`src/operations/`.

---

### Task 12: Användardokumentation och AGENTS.md

**Files:**
- Modify: `README.md`
- Modify: `docs/kom-igang.md`
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`
- Modify: `.gitignore` endast om `generated/` inte redan täcker jobbloggar
- Test: `tests/test_scripts.py`

**Interfaces:**
- Produces: full användar- och agentdokumentation för nya kommandon och admin.
- Consumes: slutliga kommandon och beteenden från Tasks 1–11.

- [ ] **Step 1: Skriv dokumentationsregression före omskrivning**

Utöka `tests/test_scripts.py` så de fyra aktuella dokumenten inte får innehålla
ett aktivt kommando som matchar `./<namn>.sh`, och så de innehåller
`python scripts/run_pipeline.py`, `python scripts/jobs.py status` och Admin.

- [ ] **Step 2: Kör testet och verifiera RED**

Run: `.venv/bin/pytest tests/test_scripts.py -q`

- [ ] **Step 3: Uppdatera README och snabbstart**

Byt installation, pipeline, webbstart och extraktion till Python-kommandon.
Lägg till Admin-fliken, ett-jobbsregeln och kort CLI-sektion för
start/status/log/cancel.

- [ ] **Step 4: Uppdatera teknisk referens fullständigt**

Dokumentera samtliga 23 Python-ersättare, registry, jobbtabell och statusar,
heartbeat/reconcile, loggpath, bakgrundskörning, Ctrl-C/cancel, datoromstart och
varför hemligheter måste ligga i miljön.

- [ ] **Step 5: Uppdatera AGENTS.md som auktoritativ framtida instruktion**

Byt kommandoblock, directory tree och designbeslut för OCR/pipeline. Lägg till
invarianten att admin och CLI alltid delar registry och att inga nya shell-
wrappers ska införas.

- [ ] **Step 6: Verifiera Task 12 och granska diffen**

Run:

```bash
.venv/bin/pytest tests/test_scripts.py -q
rg -n '\./[A-Za-z0-9_-]+\.sh\b' README.md docs/kom-igang.md docs/teknisk-referens.md AGENTS.md
git diff --check
```

Expected: `rg` ger inga aktiva gamla kommandon.

---

### Task 13: Full verifiering, live smoke och journal

**Files:**
- Modify: endast filer som behöver korrigeras av verifieringsfynd
- External journal: dagens Obsidian-journal enligt repository-instruktionen

**Interfaces:**
- Produces: verifierat slutresultat och sammanhållen journalpost.
- Consumes: alla tidigare tasks.

- [ ] **Step 1: Kör full pytest genom den nya ersättaren**

Run: `.venv/bin/python scripts/test.py`

Expected: alla tester passerar. Om ett test faller, använd
`superpowers:systematic-debugging` innan korrigering.

- [ ] **Step 2: Kör riktade statiska kontroller på hela ändringsmängden**

Run:

```bash
.venv/bin/ruff check src/operations src/admin_ui.py src/pages/8_Admin.py scripts tests/test_admin_job_db.py tests/test_admin_jobs.py tests/test_operation_context.py tests/test_operation_registry.py tests/test_operation_adapters.py tests/test_tesseract_operation.py tests/test_ocr_operation.py tests/test_neo4j_operation.py tests/test_admin_ui.py
.venv/bin/mypy src/operations src/admin_ui.py
.venv/bin/python -m compileall -q src scripts tests
git diff --check
```

- [ ] **Step 3: Kör full statisk grind som rapporterande kontroll**

Run: `.venv/bin/python scripts/test.py --static`

Om den är röd enbart på äldre, orelaterade filer ska exakta fel och berörda
filer rapporteras. Nya eller berörda filer ska vara gröna.

- [ ] **Step 4: Smoke-testa CLI-jobblivscykeln med en ofarlig dry-run**

Välj `extract-map-observations --dry-run --limit 1` eller annan registrerad
dry-run som inte skriver produktionsdata:

```bash
.venv/bin/python scripts/jobs.py start extract-map-observations --dry-run --limit 1
.venv/bin/python scripts/jobs.py status
.venv/bin/python scripts/jobs.py log --follow
.venv/bin/python scripts/jobs.py list
```

Verifiera att status går queued → running → succeeded och att låset frigörs.

- [ ] **Step 5: Smoke-testa Streamlit-admin lokalt**

Starta med den nya Python-ersättaren:

```bash
.venv/bin/python scripts/web.py -- --server.headless true --server.port 8502
```

Kontrollera Admin-sidan, aktiv jobbpanel, historik och disabled startknappar.
Använd live UI när CLI-automation inte kan verifiera Streamlit-reruns.

- [ ] **Step 6: Verifiera slutlig fil- och dokumentationsinventering**

Run:

```bash
test -z "$(rg --files -g '*.sh' -g '!graphify-out/**')"
rg -n '\./[A-Za-z0-9_-]+\.sh\b' README.md docs/kom-igang.md docs/teknisk-referens.md AGENTS.md src scripts tests
git status --short
git diff --stat
git diff --check
```

Endast avsedda projektfiler samt design/plan får vara ändrade.

- [ ] **Step 7: Genomför tvåstegsgranskning och åtgärda fynd**

Följ `superpowers:requesting-code-review`: först spec compliance mot designen,
därefter kodkvalitet/säkerhet. Kritiska och viktiga fynd korrigeras och hela
relevant verifiering körs om.

- [ ] **Step 8: Journalför slutresultatet**

Läs de senaste journalanteckningarna och skriv en sammanhållen post i dagens
svenska journalfil. Ta endast med slutstatus, nya Python-kommandon, Admin,
jobbövervakning/cancel, schema-version och hur resultatet verifierades. Skriv
inga hemligheter och ingen felsökningshistorik.

- [ ] **Step 9: Slutrapport utan commit eller push**

Rapportera berörda huvudfiler, testresultat, statisk status, live-smoke,
eventuell accepterad skuld och att arbetskopian är lämnad ocommittad för
användarens `/cap`.
