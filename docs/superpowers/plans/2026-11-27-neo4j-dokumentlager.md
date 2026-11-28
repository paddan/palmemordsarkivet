# Neo4j som fast del och dokumentlagret — implementationsplan

> **För agentiska arbetare:** KRÄVD UNDER-SKILL: använd superpowers:subagent-driven-development (rekommenderas) eller superpowers:executing-plans för att genomföra planen uppgift för uppgift. Stegen använder kryssrutor (`- [ ]`).

**Mål:** Neo4j startas med webben och installeras som standard, och alla ~10 000 dokument och ~47 000 sidor finns som noder i grafen med sin metadata, uppslag, hänvisningar och delar — sökbara och visade i Graf-sidan.

**Arkitektur:** En ren modul (`src/graph/document_rows.py`) gör om `document_meta` och textfilernas sidantal till rader; en inläsare (`src/graph/load_documents.py`) skriver dem till Neo4j i faser med `MERGE` och städar bort inaktuella noder och kanter utan att röra kunskapsgrafens `NÄMNER`/`RELATERAR`. Operationen `load-documents` körs fristående och sist i `run-pipeline`. Graf-sidan får dokument- och uppslagsvyer.

**Teknik:** Python 3.11+, Neo4j 5 (podman, drivrutin `neo4j>=6,<7`), Streamlit + st-link-analysis, SQLite (`src/db/`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-ontologi-mordnatten-design.md` — delprojekt 1. Bygger på `document_meta` från `docs/superpowers/plans/2026-11-27-dokumentmeta-ur-filnamn.md` (klar).

## Avvikelse från spec

- **`neo4j-backup`/`neo4j-restore` flyttas till delprojekt 3.** Specen lägger dem i delprojekt 1, men kopian ska innehålla det som inte går att bygga om (granskningsstatus, rättade värden, identitetskanter, angivna koordinater), och inget av det finns förrän granskningen byggs i delprojekt 3. Allt delprojekt 1 skriver byggs om ur `document_meta` och textfilerna.

## Globala villkor

- Kommentarer, docstrings, loggrader och dokumentation på svenska.
- Ingen `git commit`/`git push` av agenter — användaren committar med `/cap`.
- All SQLite-SQL i `src/db/`; nya db-funktioner re-exporteras i `src/db/__init__.py` (import med `x as x` och `__all__`).
- Ny operation kräver post i `src/operations/registry.py`, `scripts/X.py` som kör `run("<id>")` och `./X.sh` (dum genväg) i samma ändring; hjälptext i `OPERATION_HELP` i `src/admin_ui.py` med en punkt per visat fält i formulärets ordning.
- Cypher använder alltid parametrar — aldrig strängformatering med stammar, titlar eller koder (stammar innehåller `—`, `'` och `"`).
- Dokumentlagrets kanter (`HAR_SIDA`, `INGÅR_I`, `HÄNVISAR_TILL`, `DEL_AV`) och kunskapsgrafens (`NÄMNER`, `RELATERAR`) rör aldrig varandra: inläsaren ändrar eller tar aldrig bort `NÄMNER`/`RELATERAR`, och `graph_sync` ändras inte.
- Neo4j-tester körs bara mot en databas som är tom när testet börjar; de körs aldrig mot standardadressen utan uttrycklig `NEO4J_TEST_URI`.
- Loggning med `ctx.log`; summeringar, inte en rad per dokument; aldrig lösenordet.
- Verifiering: `.venv/bin/python scripts/test.py --static` grönt efter varje uppgift.

## Fokus vid granskning

1. **Ett dokument försvinner eller får färre sidor mellan två körningar** — dess inaktuella `Sida`-noder och dokumentlagrets kanter tas bort, medan `NÄMNER`-kanter till samma dokument ligger kvar. Test i uppgift 3.
2. **Hänvisning till ett nr som finns flera gånger, inte finns alls eller pekar på dokumentet självt** — kopplas till alla, räknas som saknat mål respektive hoppas över. Test i uppgift 2.
3. **Neo4j svarar inte när `run-pipeline` körs** — pipelinen loggar en varning och slutar med exitkod 0 i stället för att fälla ingest-resultatet. Test i uppgift 3.
4. **Någon kör testerna lokalt med `NEO4J_TEST_URI` mot sin riktiga, icke-tomma graf** — fixturen vägrar och rör ingenting. Test i uppgift 1.
5. **Stam med `'`, `"` och `—` i Graf-sökningen och inläsningen** — fungerar, eftersom allt går som parametrar. Test i uppgift 3 och 4.

---

### Task 1: Neo4j installeras, startas med webben och testas i CI

**Filer:**
- Ändra: `scripts/install.py` (brew-listan rad ~275 blir konstanten `BREW_PACKAGES` med `podman` tillagd; extras rad ~321)
- Ändra: `scripts/web.py`
- Ändra: `.github/workflows/ci.yml`
- Ändra: `pyproject.toml` (pytest-markören `neo4j` i `markers`)
- Skapa/ändra: `tests/conftest.py` (fixturen `neo4j_session`)
- Test: `tests/test_install.py`, `tests/test_web_entry.py` (ny), `tests/test_neo4j_fixture.py` (ny)
- Dokumentation: `docs/kom-igang.md`, `docs/teknisk-referens.md` (tester, krav), `AGENTS.md` (Commands/Gotchas: Neo4j obligatoriskt, testdatabasregeln)

**Gränssnitt:**
- Producerar:
  - `install.pip_extras(with_dev: bool) -> str` → `"dev,web,graph"` respektive `"web,graph"`.
  - `web.start_neo4j() -> bool` i `scripts/web.py`: anropar `operations.neo4j.neo4j_start(None)`; `True` vid rc 0; vid icke-noll rc eller undantag skrivs en rad till stderr — `Kunskapsgrafen startade inte (<orsak>). Graf-sidan fungerar inte förrän den startas i Admin → Extraktion och graf.` — och `False` returneras. `main` startar Streamlit oavsett utfall. `--help` startar inte Neo4j och skriver exakt samma text som i dag.
  - pytest-fixturen `neo4j_session` i `tests/conftest.py`: läser `NEO4J_TEST_URI`, `NEO4J_TEST_USER` (standard `neo4j`), `NEO4J_TEST_PASSWORD`. Saknas URI → `pytest.skip`, utom när `PALME_REQUIRE_NEO4J_TESTS=1` → `pytest.fail`. Innehåller databasen någon nod när fixturen startar → `pytest.fail("Testdatabasen är inte tom — vägrar köra Neo4j-tester mot den.")` utan att skriva något. Efter testet: `MATCH (n) DETACH DELETE n`. Testfunktioner som använder den märks `@pytest.mark.neo4j`.
  - CI: tjänstecontainer `neo4j:5` med `NEO4J_AUTH: neo4j/palme-ci-test`, port 7687, hälsokontroll; installationssteget `.[dev,web,graph]`; teststeget får `NEO4J_TEST_URI=bolt://localhost:7687`, `NEO4J_TEST_PASSWORD=palme-ci-test`, `PALME_REQUIRE_NEO4J_TESTS=1`. Kommentaren om att Neo4j mockas skrivs om.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
# tests/test_install.py
def test_pip_extras_include_graph():
    assert install.pip_extras(with_dev=True) == "dev,web,graph"
    assert install.pip_extras(with_dev=False) == "web,graph"

def test_brew_installs_podman():
    assert "podman" in install.BREW_PACKAGES  # ny konstant som ersätter den inskrivna listan i brew-anropet (rad ~275)

# tests/test_web_entry.py
def test_web_starts_streamlit_even_if_neo4j_fails(monkeypatch, capsys):
    # neo4j_start höjer RuntimeError("podman saknas"); os.execvpe ersätts av en fångare
    # → execvpe anropas med streamlit, och stderr innehåller "Kunskapsgrafen startade inte (podman saknas)"
def test_web_help_does_not_start_neo4j(monkeypatch, capsys):
    # neo4j_start ersätts av en funktion som höjer AssertionError; main(["--help"]) == 0
def test_web_starts_neo4j_before_streamlit(monkeypatch):
    # anropsordningen är ["neo4j", "streamlit"]

# tests/test_neo4j_fixture.py
@pytest.mark.neo4j
def test_fixture_gives_empty_database(neo4j_session):
    assert neo4j_session.run("MATCH (n) RETURN count(n) AS c").single()["c"] == 0
def test_fixture_refuses_non_empty_database():
    session = _FakeSession(count=3)  # run(...).single()["c"] == 3; registrerar alla anrop
    with pytest.raises(pytest.fail.Exception, match="Testdatabasen är inte tom"):
        _guard_empty(session)
    assert all("DELETE" not in q for q in session.queries)
```

Bryt ut kontrollen i en hjälpfunktion `_guard_empty(session) -> None` i `tests/conftest.py` så att vägran kan testas utan Neo4j.

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_install.py tests/test_web_entry.py tests/test_neo4j_fixture.py -v`
Förväntat: FAIL (`pip_extras`/`start_neo4j`/`_guard_empty` saknas); `test_fixture_gives_empty_database` SKIPPED lokalt.

- [ ] **Steg 3: Implementera** enligt gränssnittet. `web.py` importerar `operations.neo4j` lat inne i `start_neo4j` (med `src` på `sys.path` som övriga scripts gör).

- [ ] **Steg 4: Kör testerna**

Kör: `.venv/bin/pytest tests/test_install.py tests/test_web_entry.py tests/test_neo4j_fixture.py tests/test_scripts.py -v`
Förväntat: PASS (Neo4j-testet SKIPPED lokalt om `NEO4J_TEST_URI` saknas).

- [ ] **Steg 5: Prova fixturen mot en egen testcontainer (valfritt lokalt)**

```bash
podman run -d --rm --name palme-neo4j-test -p 17687:7687 -e NEO4J_AUTH=neo4j/palme-ci-test neo4j:5
NEO4J_TEST_URI=bolt://localhost:17687 NEO4J_TEST_PASSWORD=palme-ci-test PALME_REQUIRE_NEO4J_TESTS=1 .venv/bin/pytest -m neo4j -v
podman stop palme-neo4j-test
```
Förväntat: PASS. Använd aldrig port 7687 (den riktiga grafen).

- [ ] **Steg 6: Dokumentera och kör hela sviten**

Uppdatera dokumentationen enligt fillistan. Kör: `.venv/bin/python scripts/test.py --static` — inga fel.

---

### Task 2: Dokumentrader ur `document_meta` (ren logik)

**Filer:**
- Skapa: `src/graph/document_rows.py`
- Ändra: `src/db/pipeline.py`, `src/db/__init__.py` (`list_document_meta`)
- Test: `tests/test_document_rows.py`, `tests/test_db_document_meta.py`

**Gränssnitt:**
- Konsumerar: tabellen `document_meta` och `db.get_document_meta`-formen (listor avkodade, `avkortat` som bool).
- Producerar:
  - `db.list_document_meta(conn) -> list[dict[str, Any]]` — alla rader i samma form som `get_document_meta`, sorterade på `pdf_stem`.
  - `count_pages(text: str) -> int` — samma sidindelning som `rag.ingest.split_pages` (`len(text.split("\f"))` om `"\f"` finns, annars 1), så att `Sida.sida` motsvarar LanceDB:s `page`. Modulen importerar inte `rag.ingest` (tung import).
  - `nr_key(nr: str) -> str` — `nr.strip().replace(",", ".")`.
  - `@dataclass(frozen=True) class DocumentRows` med `documents: list[dict]`, `pages: list[dict]`, `uppslag: list[dict]`, `references: list[dict]`, `parts: list[dict]`, `unresolved_references: int`.
  - `build_document_rows(metas: list[dict], page_counts: Mapping[str, int]) -> DocumentRows`:
    - `documents`: per meta `{stem, arkiv, nr, titel, bestallt, upplagt, anmarkning, antal_sidor, dokumentdatum, avkortat, sidor}` där `sidor = page_counts.get(stem, 0)`.
    - `pages`: `{id: f"{stem}#{n}", stem, sida: n}` för `n` i `1..sidor`.
    - `uppslag`: `{stem, kod}` per kod i `meta["uppslag"]`.
    - `references`: för varje hänvisning i ett palme-dokument, `{stem, target_stem}` för **alla** palme-dokument vars `nr_key(nr)` är lika med `nr_key(hänvisning)`, utom dokumentet självt; en hänvisning utan mål ökar `unresolved_references`.
    - `parts`: för palme-dokument, segmenten `[s for s in nr_key(nr).split(".") if s]`; ta bort sista segmentet tills de återstående (minst ett) motsvarar ett befintligt `nr_key` → `{stem, parent_stem}` för alla dokument med det numret; ett nr med ett segment har ingen förälder.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def _meta(stem, nr=None, arkiv="palme", uppslag=(), hanv=()):
    return {"pdf_stem": stem, "arkiv": arkiv, "nr": nr, "titel": "T", "bestallt": None,
            "upplagt": None, "anmarkning": None, "antal_sidor": None, "dokumentdatum": None,
            "uppslag": list(uppslag), "hanvisningar": list(hanv), "avkortat": False}

def test_count_pages_matches_ingest_split():
    from rag.ingest import split_pages
    for text in ("", "a", "a\fb", "a\f\fc\f"):
        assert count_pages(text) == len(split_pages(text))

def test_pages_and_document_rows():
    rows = build_document_rows([_meta("A", "1")], {"A": 2})
    assert rows.documents[0]["sidor"] == 2
    assert rows.pages == [{"id": "A#1", "stem": "A", "sida": 1}, {"id": "A#2", "stem": "A", "sida": 2}]

def test_reference_to_number_with_two_documents_links_both():
    rows = build_document_rows([_meta("A", "1", hanv=["5"]), _meta("B", "5"), _meta("C", "5")], {})
    assert sorted(r["target_stem"] for r in rows.references) == ["B", "C"]

def test_unknown_and_self_references():
    rows = build_document_rows([_meta("A", "1", hanv=["1", "999"])], {})
    assert rows.references == [] and rows.unresolved_references == 1

def test_comma_and_dot_numbers_match():
    rows = build_document_rows([_meta("A", "1", hanv=["281,10"]), _meta("B", "281.10")], {})
    assert rows.references == [{"stem": "A", "target_stem": "B"}]

def test_part_links_to_nearest_existing_parent():
    rows = build_document_rows([_meta("P", "1295"), _meta("C", "1295.24.3")], {})
    assert rows.parts == [{"stem": "C", "parent_stem": "P"}]

def test_wpu_documents_get_no_references_or_parts():
    rows = build_document_rows([_meta("W", None, arkiv="wpu", uppslag=["E9979-00-A"])], {})
    assert rows.references == [] and rows.parts == [] and rows.uppslag == [{"stem": "W", "kod": "E9979-00-A"}]
```

Lägg till i `tests/test_db_document_meta.py`: `list_document_meta` ger alla rader sorterade på stam, med listor och bool.

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_document_rows.py tests/test_db_document_meta.py -v`
Förväntat: FAIL (`ModuleNotFoundError: graph.document_rows`, `list_document_meta` saknas).

- [ ] **Steg 3: Implementera** enligt gränssnittet.

- [ ] **Steg 4: Kör testerna och kontrollera mot riktiga data (läsning)**

Kör: `.venv/bin/pytest tests/test_document_rows.py tests/test_db_document_meta.py -v` → PASS. Bygg sedan raderna ur den riktiga `document_meta` och `generated/text` utan att skriva något och skriv ut antal dokument, sidor, uppslag, hänvisningar, saknade mål och delar. Förväntat: 10 361 dokument, ~47 000 sidor, inget undantag.

- [ ] **Steg 5: Hela sviten** — `.venv/bin/python scripts/test.py --static` utan fel.

---

### Task 3: Inläsning till Neo4j och operationen `load-documents`

**Filer:**
- Skapa: `src/graph/load_documents.py`, `scripts/load_documents.py`, `load_documents.sh`
- Ändra: `src/operations/registry.py` (`_register_graph_operations`), `src/operations/adapters.py`, `src/operations/pipeline.py`, `src/admin_ui.py` (`OPERATION_HELP`)
- Test: `tests/test_load_documents.py`, `tests/test_operation_pipeline.py`
- Dokumentation: `docs/teknisk-referens.md` (kunskapsgrafen, pipelinen, kommandon), `AGENTS.md` (Commands; kort designbeslut om dokumentlagret och kantägarskapet), `README.md` om den listar operationer

**Gränssnitt:**
- Konsumerar: `build_document_rows`, `count_pages`, `DocumentRows` (uppgift 2); `db.list_document_meta`; `graph.viz.resolve_password`; fixturen `neo4j_session` (uppgift 1).
- Producerar:
  - `DOCUMENT_CONSTRAINTS: list[str]` — unikhet på `Dokument.stem` (samma namn som befintliga `dokument_stem`, `IF NOT EXISTS`), `Sida.id`, `Uppslag.kod`.
  - `write_document_layer(session, rows: DocumentRows, *, batch: int, ctx) -> dict[str, int]` — skriver i faser, varje batch i en egen skrivtransaktion: (1) `MERGE (d:Dokument {stem})` och `SET` alla dokumentegenskaper; (2) `MERGE (s:Sida {id})` med `stem`, `sida` och `HAR_SIDA` från dokumentet; (3) per batch av dokument: ta bort dokumentets **utgående** `INGÅR_I`/`HÄNVISAR_TILL`/`DEL_AV` och skapa dem igen ur raderna (`MERGE (u:Uppslag {kod})`); (4) städning: `Sida`-noder vars `stem` saknas eller vars `sida` > dokumentets `sidor` tas bort; dokument med egenskapen `arkiv` vars stam inte finns i raderna förlorar sina `Sida`-noder och dokumentlagrets kanter, och raderas om de då saknar kanter — annars tas dokumentlagrets egenskaper bort men `stem`, `nr`, `titel` behålls; `Uppslag` utan `INGÅR_I` raderas. Returnerar antal per kategori, inklusive borttagna.
  - `run_load_documents(*, uri: str, user: str, batch: int, text_dir: Path, context=None) -> int` — läser `document_meta` och sidantal (`count_pages` på `text_dir/<stem>.txt`, 0 om filen saknas, med en debugrad med antal och fem exempel); saknat lösenord eller `batch < 1` → `OperationFailed`; en info-rad: `Dokumentlager: N dokument, M sidor, U uppslag, H hänvisningar (X utan mål), D delar; borttaget: …`.
  - Registret: `id="load-documents"`, `label="Ladda dokumentnoder"`, `group="Extraktion och graf"`, `description="Läs in alla dokument och sidor med metadata i Neo4j."`, parametrar `uri` (`--uri`, str, `bolt://localhost:7687`, "Neo4j-URI"), `user` (`--user`, str, `neo4j`, "Användare"), `batch` (`--batch`, int, 1000, "Rader per skrivbatch"), `text_dir` (`--text-dir`, path, `ROOT/"generated"/"text"`, "Text-katalog"); `admin_visible=True`, `mutating=True`, `confirmation=None`; `adapters.load_documents_adapter` i samma stil som `load_graph_adapter`.
  - `OPERATION_HELP["load-documents"]`: förklaring (läser in alla dokument och sidor med metadata, uppslag, hänvisningar och delar; kunskapsgrafens kanter rörs inte; körs också sist i Full pipeline) och en punkt per fält i formulärets ordning.
  - `PipelineDependencies.load_documents: Callable[..., int] | None`; `run_pipeline` kör den efter ingest med `uri="bolt://localhost:7687"`, `user="neo4j"`, `batch=1000`, `text_dir=options.txt`. `OperationFailed` om lösenord saknas, `neo4j.exceptions.ServiceUnavailable` eller `OSError` → `ctx.log("Hoppar dokumentnoder: <orsak>. Kör Ladda dokumentnoder när Neo4j är igång.", level="warning")` och pipelinen fortsätter till `Pipeline klar.` En icke-noll rc från steget fäller pipelinen som andra steg.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
@pytest.mark.neo4j
def test_load_creates_documents_pages_and_edges(neo4j_session): ...
    # två palme-dokument (det ena hänvisar till det andra, delar uppslag "D4991-00") med 2 resp. 1 sida
    # → 2 Dokument med arkiv/nr/titel, 3 Sida (id "A#1", "A#2", "B#1"), 1 Uppslag, 2 INGÅR_I, 1 HÄNVISAR_TILL
@pytest.mark.neo4j
def test_reload_is_idempotent(neo4j_session): ...   # två körningar → samma antal noder och kanter
@pytest.mark.neo4j
def test_reload_removes_stale_pages_and_documents_but_keeps_mentions(neo4j_session): ...
    # skapa först (Dokument {stem:"A"})-[:NÄMNER {graph_owner:'palmemordsarkivet'}]->(:Person {norm:"x"});
    # ladda A (2 sidor) och B; ladda sedan A med 1 sida och utan B
    # → A#2 borta, B och dess Sida borta, NÄMNER-kanten och Person finns kvar, A har kvar nr/titel
@pytest.mark.neo4j
def test_odd_characters_in_stem(neo4j_session): ...  # stam med ', " och — läses in och hittas igen
def test_missing_password_fails(monkeypatch, tmp_path): ...  # resolve_password → None ⇒ OperationFailed
def test_pipeline_continues_when_neo4j_is_down(...): ...
    # PipelineDependencies med falska steg; load_documents höjer ServiceUnavailable
    # → run_pipeline == 0, en warning-rad som börjar "Hoppar dokumentnoder:"
def test_pipeline_runs_load_documents_after_ingest(...): ...  # anropsordningen slutar ["ingest", "load-documents"]
```

Registret, scripts och hjälptexten bevakas av befintliga `tests/test_scripts.py`, `tests/test_operation_registry.py` och `tests/test_admin_ui.py` — de ska falla tills alla tre delarna och hjälpen finns.

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_load_documents.py tests/test_scripts.py tests/test_admin_ui.py tests/test_operation_registry.py tests/test_operation_pipeline.py -v`
Förväntat: FAIL (modul och operation saknas); Neo4j-testerna SKIPPED lokalt utan `NEO4J_TEST_URI`.

- [ ] **Steg 3: Implementera** enligt gränssnittet.

- [ ] **Steg 4: Kör testerna, även mot testcontainern**

Kör samma kommandon som i steg 2 → PASS, och Neo4j-testerna mot en testcontainer på port 17687 enligt uppgift 1 steg 5 → PASS.

- [ ] **Steg 5: Hela sviten** — `.venv/bin/python scripts/test.py --static` utan fel.

- [ ] **Steg 6: Dokumentera** enligt fillistan.

- [ ] **Steg 7: Riktig inläsning (bara efter användarens klartecken)**

`.venv/bin/python scripts/jobs.py start load-documents` med Neo4j igång. Förväntat: loggraden `Dokumentlager: 10361 dokument, ~47000 sidor, …`, och kunskapsgrafens `NÄMNER`-antal är oförändrat före och efter (`MATCH ()-[r:NÄMNER]->() RETURN count(r)`).

---

### Task 4: Graf-sidan visar dokument och uppslag

**Filer:**
- Ändra: `src/graph/viz.py`, `src/pages/3_Graf.py`
- Test: `tests/test_viz.py`
- Dokumentation: `docs/teknisk-referens.md` (Graf-sidan)

**Gränssnitt:**
- Konsumerar: noderna och kanterna från uppgift 3.
- Producerar (i `viz.py`):
  - `NODE_COLORS["Uppslag"]` (en egen färg) och `NODE_ICONS["Uppslag"] = "folder"`.
  - `search_documents(session, query: str, limit: int = 20) -> list[dict]` — `{label: "Dokument", namn: "<nr> — <titel>" (som _doc_label), norm: stem}` för dokument där `nr` är lika med eller börjar med frågan, eller titeln innehåller den (skiftlägesokänsligt); `{label: "Uppslag", namn: kod, norm: kod}` för uppslag vars kod börjar med frågan (versaler, utan bindestreck mellan bokstäver och siffror, så att `D-4991` hittar `D4991-00`). Dokument först, kortaste namn först.
  - `fetch_document_ego(session, stem: str, limit: int = 60) -> tuple[dict | None, list[dict]]` — dokumentets egenskaper och rader `{kind, stem|kod|norm, namn, label}` för: uppslag, hänvisningar ut och in, förälder och delar, andra dokument i samma uppslag (högst `limit`), och nämnda entiteter (`NÄMNER`).
  - `fetch_uppslag_ego(session, kod: str, limit: int = 60) -> list[dict]` — dokumenten i uppslaget.
  - `assemble_document_graph(center: dict, rows: list[dict]) -> tuple[list[dict], list[dict]]` — ren; nod-id `doc:<stem>`, `upp:<kod>` och entiteternas `norm`; kantetiketter `ingår i`, `hänvisar till`, `del av`, `nämner`; samma nod- och kantform som `assemble_graph`, så att `to_cytoscape_elements` kan återanvändas.
  - `document_meta_rows(doc: dict) -> list[tuple[str, str]]` — ren; i ordningen Nr, Titel, Arkiv, Dokumentdatum, Beställt, Upplagt, Anmärkning, Sidor; tomma värden utelämnas.
- `3_Graf.py`: sökningen visar entiteter, dokument och uppslag i samma träfflista. Ett dokument som center visar metadata (`document_meta_rows`) med knappen **Öppna PDF** (befintliga `_casebook_ui.find_pdf` + `open_pdf_in_browser`) och dokumentgrafen; ett uppslag visar sina dokument. Dubbelklick på ett dokument i grafen öppnar PDF:en som i dag; dubbelklick på ett uppslag eller en entitet gör den till center. Saknas dokumentlagret (inga `Dokument` med `arkiv`) visas `Dokumentnoderna är inte inlästa — kör Ladda dokumentnoder i Admin.`

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def test_document_meta_rows_order_and_skip_empty():
    doc = {"nr": "1", "titel": "PM", "arkiv": "palme", "dokumentdatum": None,
           "bestallt": "2020-06-12", "upplagt": "2020-07-14", "anmarkning": "", "sidor": 10}
    assert document_meta_rows(doc) == [("Nr", "1"), ("Titel", "PM"), ("Arkiv", "palme"),
        ("Beställt", "2020-06-12"), ("Upplagt", "2020-07-14"), ("Sidor", "10")]

def test_assemble_document_graph_nodes_and_edges():
    center = {"stem": "A", "nr": "1", "titel": "PM"}
    rows = [{"kind": "uppslag", "kod": "D4991-00"}, {"kind": "hanvisar_ut", "stem": "B", "namn": "2 — X"},
            {"kind": "nämner", "norm": "stig engström", "namn": "Stig Engström", "label": "Person"}]
    nodes, edges = assemble_document_graph(center, rows)
    assert {n["id"] for n in nodes} == {"doc:A", "upp:D4991-00", "doc:B", "stig engström"}
    assert {(e["source"], e["target"], e["label"]) for e in edges} == {
        ("doc:A", "upp:D4991-00", "ingår i"), ("doc:A", "doc:B", "hänvisar till"),
        ("doc:A", "stig engström", "nämner")}

@pytest.mark.neo4j
def test_search_documents_by_nr_title_and_uppslag(neo4j_session): ...
    # efter inläsning av två dokument (uppgift 3:s write_document_layer): "1" hittar nr 1 först,
    # en titeldel hittar dokumentet, "D-4991" hittar uppslaget D4991-00, en stam med ' och — fungerar
@pytest.mark.neo4j
def test_fetch_document_ego_returns_all_kinds(neo4j_session): ...
```

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_viz.py -v`
Förväntat: FAIL (funktionerna saknas).

- [ ] **Steg 3: Implementera** `viz.py` enligt gränssnittet, sedan sidan.

- [ ] **Steg 4: Kör testerna** — `.venv/bin/pytest tests/test_viz.py -v` → PASS; Neo4j-testerna mot testcontainern → PASS.

- [ ] **Steg 5: Prova sidan**

Starta `./web.sh` (efter uppgift 3 steg 7), öppna Graf, sök `1295` (dokument och delar), `D-4991` (uppslag) och ett namn (entitet). Förväntat: metadata och **Öppna PDF** för dokumentet, uppslagets dokument listas, entitetssökningen fungerar som förut.

- [ ] **Steg 6: Hela sviten och dokumentation** — `.venv/bin/python scripts/test.py --static` utan fel; Graf-avsnittet i `docs/teknisk-referens.md` uppdaterat.
