# Ontologi: extraktion för mordnatten — implementationsplan

> **För agentiska arbetare:** KRÄVD UNDER-SKILL: använd superpowers:subagent-driven-development (rekommenderas) eller superpowers:executing-plans för att genomföra planen uppgift för uppgift. Stegen använder kryssrutor (`- [ ]`).

**Mål:** En LLM läser utvalda sidor (piloten: EA-serierna, 100 sidor) och extraherar observationer från mordnatten — tid, plats, ankarhändelse, vittne och signalement — som sparas rått i SQLite och läses in i Neo4j som ogranskade uppgifter med källa.

**Arkitektur:** Ett nytt paket `src/ontologi/` med fyra ansvar: konfiguration ur `data/ontologi/` (`config.py`), tolkning och kontroll av LLM-svaret (`extraction.py`, ren), urval av sidor (`selection.py`, ren) och inläsning i Neo4j (`load_graph.py`). Operationen `extract-mordnatten` binder ihop dem: urval → LLM per sida → råsvar i SQLite → inläsning. Inläsningen kan köras om utan nya LLM-anrop.

**Teknik:** Python 3.11+, SQLite (`src/db/`), Neo4j 5 (drivrutin 6), Claude Agent SDK / OpenAI-kompatibla API:er via LLM-profiler (`config.resolve_runtime_profile`), pytest.

**Spec:** `docs/superpowers/specs/2026-11-27-ontologi-mordnatten-design.md` — delprojekt 2 (avsnitten Mordnattslagret, Granskningsstatus, Extraktion). Bygger på dokumentlagret från `docs/superpowers/plans/2026-11-27-neo4j-dokumentlager.md` (klart och inläst).

## Globala villkor

- Kommentarer, docstrings, loggrader och dokumentation på svenska.
- Ingen `git commit`/`git push` av agenter — användaren committar med `/cap`.
- All SQLite-SQL i `src/db/` med re-export i `src/db/__init__.py`; schemaändring som ny migration i `src/db/_schema.py`.
- Ny operation kräver registerpost, `scripts/X.py` och `./X.sh` i samma ändring, och `OPERATION_HELP` med en punkt per visat fält.
- Cypher alltid parametriserad. Ontologins etiketter (`Observation`, `Vittne`, `MaskadUppgift`, `Händelse`, `Kartplats`, `ObserveradPerson`, `Egenskap`, `Nyckelperson`) är skilda från kunskapsgrafens `Person`/`Plats`/`Organisation`; ingen kod i planen rör `NÄMNER`/`RELATERAR` eller dokumentlagrets kanter utom att läsa `Sida`.
- **Maskning:** en `MaskadUppgift` gäller bara inom sitt dokument (`id` = stam + lokalt id) och får aldrig `SAMMA_PERSON` eller annan identitetskant. Ett namn som liknar en platshållare (`[MASKAD]`, `NN`, `okänd`, `X`) blir aldrig ett `Vittne`. Innehållet bakom en maskning gissas aldrig.
- **Ingen påhittad uppgift når grafen:** en observation vars `utdrag` inte står på sidan förkastas; ett vittnesnamn som inte står på sidan behandlas som okänt; `KAN_VARA` skapas bara mot `Nyckelperson` och bara när ett av dess alias står i utdraget.
- Ny observation får status `ogranskad`. Status sätts aldrig tillbaka till `ogranskad` för en granskad observation (`godkänd`, `avvisad`, `rättad`).
- Hemligheter (API-nycklar) loggas aldrig; hela sidtexter loggas aldrig.
- LLM-anrop i tester mockas alltid; ingen test gör nätverksanrop.
- Neo4j-tester märks `@pytest.mark.neo4j` och använder fixturen `neo4j_session`.
- Verifiering: `.venv/bin/python scripts/test.py --static` grönt efter varje uppgift.

## Fokus vid granskning

1. **LLM:en hittar på ett citat eller ett namn som inte står på sidan** — observationen förkastas respektive vittnet blir okänt, med en debugrad. Test i uppgift 2.
2. **En sida extraheras om efter omskanning (ny text)** — ogranskade observationer från sidan ersätts, granskade blir `inaktuell` och de nya läggs bredvid; samma text två gånger skapar inga dubbletter. Test i uppgift 4.
3. **Samma vittnesnamn i två dokument och ett maskat vittne** — `SAMMA_PERSON` föreslås mellan de namngivna, aldrig för det maskade. Test i uppgift 4.
4. **Trasigt eller avklippt LLM-svar (ingen JSON, fel typer, `min > max`)** — sidan sparas med noll observationer och en varning, körningen fortsätter. Test i uppgift 2.
5. **Profilen saknar nyckel eller svarar 401/429** — saknad nyckel stoppar före första anropet; 429 försöks om; 401/403 stoppar körningen utan att markera sidor som klara. Test i uppgift 5.

---

### Task 1: Ontologins konfiguration i `data/ontologi/`

**Filer:**
- Skapa: `data/ontologi/schema.json`, `data/ontologi/ankarhandelser.json`, `data/ontologi/nyckelpersoner.json`, `data/ontologi/urval.json`
- Skapa: `src/ontologi/__init__.py`, `src/ontologi/config.py`
- Test: `tests/test_ontologi_config.py`

**Gränssnitt:**
- Producerar:
  - `SIGNALEMENT_TYPER = ("längd", "ålder", "kroppsbyggnad", "hår", "klädsel", "färg", "övrigt")`; `NUMERISKA = {"längd": "cm", "ålder": "år"}`.
  - `@dataclass(frozen=True) Ankarhandelse(id: str, namn: str, ordning: int, tid_fran: str, tid_till: str, beskrivning: str)`.
  - `@dataclass(frozen=True) Nyckelperson(id: str, namn: str, alias: tuple[str, ...])`.
  - `@dataclass(frozen=True) Urval(serier: tuple[str, ...], sokfragor: tuple[str, ...], stammar: tuple[str, ...], sidor: tuple[tuple[str, int], ...], max_sidor: int)`.
  - `@dataclass(frozen=True) OntologiConfig(schemaversion: int, schema: dict, handelser: tuple[Ankarhandelse, ...], nyckelpersoner: tuple[Nyckelperson, ...], urval: Urval, platser: list[dict])`.
  - `load_config(root: Path = ROOT) -> OntologiConfig` — läser de fyra filerna och `data/karta/platser.json`; höjer `ValueError` med filnamn och orsak vid: dubblett-id, tid som inte är `HH:MM`, `tid_fran > tid_till`, okänd nyckel, `max_sidor < 1`, `schemaversion` som inte är ett positivt heltal.
- Filinnehåll:
  - `schema.json`: `{"schemaversion": 1, "schema": <JSON Schema>}` för svaret `{"observationer": [ {tid_text, tid_fran, tid_till, plats, handelse_id, vittne: {namn, maskad, kategori, roll}, observerade: [ {beskrivning, signalement: [ {typ, varde_text, min, max, enhet} ], kan_vara: [id]} ], utdrag, osakerhet} ]}`; `tid_*` `HH:MM` eller `null`; `vittne.kategori` ∈ `namn|adress|telefon|personnummer|okänd`; `typ` ∈ `SIGNALEMENT_TYPER`.
  - `ankarhandelser.json` (utkast, märkt `"utkast": true`, ägaren rättar tider och namn): `palmes-lamnar-hemmet` 20:30–20:40, `ankomst-grand` 20:45–20:55, `grand-slut` 23:05–23:12, `vandring-sveavagen` 23:10–23:21, `skotten` 23:21–23:22, `flykt-tunnelgatan` 23:21–23:23, `flykt-trapporna` 23:22–23:24, `flykt-malmskillnadsgatan` 23:23–23:30, `forsta-polis` 23:22–23:26, `ambulans` 23:24–23:30, `sabbatsberg` 23:28–23:40.
  - `nyckelpersoner.json`: `garningsmannen` (alias: "gärningsmannen", "mördaren", "mannen som sköt", "skytten", "gärningsman"), `olof-palme` ("Olof Palme", "statsministern", "Palme"), `lisbet-palme` ("Lisbet Palme", "fru Palme", "hustrun").
  - `urval.json`: `{"serier": ["EAE","EAD","EAC","EAA","EA","EAF","EAB"], "sokfragor": ["skotten på Sveavägen", "mannen sprang uppför trapporna Tunnelgatan", "såg en man vid Dekorima"], "stammar": [], "sidor": [], "max_sidor": 100}`.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def test_repo_config_loads():
    cfg = load_config()
    assert cfg.schemaversion == 1 and len(cfg.handelser) == 11
    assert cfg.urval.max_sidor == 100 and "EAE" in cfg.urval.serier
    assert {p.id for p in cfg.nyckelpersoner} == {"garningsmannen", "olof-palme", "lisbet-palme"}

def test_duplicate_event_id_is_rejected(tmp_path): ...   # kopiera repo-filerna till tmp, duplicera ett id → ValueError som nämner ankarhandelser.json
def test_bad_time_is_rejected(tmp_path): ...             # "23.21" → ValueError "HH:MM"
def test_reversed_window_is_rejected(tmp_path): ...      # tid_fran 23:30, tid_till 23:20 → ValueError
```

- [ ] **Steg 2: Kör och se dem falla** — `.venv/bin/pytest tests/test_ontologi_config.py -v` → FAIL (`ModuleNotFoundError: ontologi`).
- [ ] **Steg 3: Implementera** filerna och `config.py` enligt gränssnittet.
- [ ] **Steg 4: Kör testerna** → PASS; sedan `.venv/bin/python scripts/test.py --static` utan fel.

---

### Task 2: Tolkning och kontroll av LLM-svaret (ren logik)

**Filer:**
- Skapa: `src/ontologi/extraction.py`
- Test: `tests/test_ontologi_extraction.py`

**Gränssnitt:**
- Konsumerar: `OntologiConfig`, `SIGNALEMENT_TYPER`, `NUMERISKA` (uppgift 1); `map_extract.build_place_index` och `map_extract.match_place` (befintliga, återanvänds för platsmatchning).
- Producerar:
  - `PLATSHALLARE_RE` — matchar `[MASKAD]` (alla skiftlägen och blanksteg inom hakarna), `NN`, `N.N.`, `okänd`, `okänd person`, `X`, `XX` som hela namnet.
  - `normalize_text(text: str) -> str` — gemener, `\s+` → ett mellanslag, strip.
  - `text_hash(page_text: str) -> str` — sha256-hex av `normalize_text(page_text)`.
  - `SYSTEM_PROMPT: str` — instruktion på svenska: extrahera bara iakttagelser kring mordnatten, `utdrag` ordagrant ur sidan (högst 300 tecken), namn bara om de står på sidan, `[MASKAD]` ⇒ `maskad: true` och aldrig ett gissat namn, `handelse_id` bara ur listan, `kan_vara` bara ur listan och bara när texten själv pekar ut personen, längd/ålder som intervall; returnera bara JSON enligt schemat. Prompten byggs av `build_prompt(page_text: str, cfg: OntologiConfig) -> str` som lägger händelselistan (id, namn, tidsfönster) och nyckelpersonerna (id, namn) före sidtexten.
  - `@dataclass(frozen=True) ParsedPage(observationer: list[dict], varningar: list[str])`.
  - `parse_response(raw: str, page_text: str, cfg: OntologiConfig) -> ParsedPage` — regler:
    - Ingen JSON-objekt / ogiltig JSON / `observationer` inte en lista → inga observationer, en varning.
    - En observation förkastas (varning) om `utdrag` saknas eller `normalize_text(utdrag)` inte är delsträng av `normalize_text(page_text)`.
    - `tid_fran`/`tid_till` som inte är `HH:MM` → `None` (varning); om bara en finns sätts den andra lika; `tid_fran > tid_till` → båda `None` (varning).
    - `handelse_id` som inte finns i konfigurationen → `None` (varning).
    - `plats` matchas med `match_place` mot platserna; resultatet sparas som `plats: {namn_rå, kartplats_namn, lat, lon, match}`.
    - Vittne: tomt namn, ett namn som matchar `PLATSHALLARE_RE`, eller `maskad: true` ⇒ `{"maskad": True, "namn": None, ...}`; ett namn som inte står på sidan (normaliserad delsträng) ⇒ `namn: None`, `maskad: False` (varning). `kategori` utanför listan → `"okänd"`.
    - Signalement: `typ` utanför `SIGNALEMENT_TYPER` → `"övrigt"`; för `längd`/`ålder` tas `min`/`max` som tal (int eller float), `min > max` byts inte utan båda blir `None` (varning), `enhet` sätts från `NUMERISKA`; för övriga typer är `min`/`max` `None`.
    - `kan_vara`: behåll id:n som finns i `nyckelpersoner` och vars alias (normaliserat) står i utdraget; övriga tas bort (debug-varning).
    - Varje behållen observation får `index` (0-baserad ordning bland de behållna).

- [ ] **Steg 1: Skriv de fallerande testerna** (en `cfg` byggd ur repo-filerna)

```python
PAGE = "Vittnet Anna Berg såg kl 23.21 en man i mörk rock, ca 180-185 cm, springa in på Tunnelgatan. [MASKAD] uppgav att gärningsmannen var cirka 40 år."

def test_valid_observation_is_kept_with_index(): ...      # en observation med utdrag ur PAGE → 1 kvar, index 0, tid 23:21–23:21
def test_invented_quote_is_dropped():                     # utdrag som inte står på sidan
    assert parse_response(json.dumps({"observationer": [{"utdrag": "Han flydde med bil", ...}]}), PAGE, cfg).observationer == []
def test_invented_name_becomes_unknown(): ...             # vittne.namn "Bo Ek" → namn None, maskad False, en varning
def test_placeholder_name_becomes_masked(): ...           # "[MASKAD]", "NN", "okänd" → maskad True, namn None
def test_kan_vara_requires_alias_in_quote(): ...          # "garningsmannen" behålls för utdraget med "gärningsmannen", tas bort annars; okänt id tas bort
def test_height_interval_and_reversed_interval(): ...     # längd 180–185 → min 180, max 185, enhet "cm"; min 190 max 180 → None, None
def test_unknown_event_and_bad_time(): ...                # handelse_id "påhittad" → None; tid "kvart över" → None
def test_garbage_response_gives_no_observations():
    r = parse_response("Tyvärr kan jag inte", PAGE, cfg)
    assert r.observationer == [] and r.varningar
def test_place_is_matched_to_catalog(): ...               # plats "Tunnelgatan" → match "exact" eller "fuzzy", lat/lon satta
def test_text_hash_ignores_whitespace():
    assert text_hash("a  b\n") == text_hash("A b")
```

- [ ] **Steg 2: Kör och se dem falla** — `.venv/bin/pytest tests/test_ontologi_extraction.py -v` → FAIL.
- [ ] **Steg 3: Implementera** enligt gränssnittet.
- [ ] **Steg 4: Kör testerna** → PASS; `.venv/bin/python scripts/test.py --static` utan fel.

---

### Task 3: Råtabellen i SQLite och urvalet av sidor

**Filer:**
- Ändra: `src/db/_schema.py` (migration 15), `src/db/pipeline.py`, `src/db/__init__.py`
- Skapa: `src/ontologi/selection.py`
- Test: `tests/test_db_ontology_extractions.py`, `tests/test_ontologi_selection.py`, `tests/test_db.py` (versionslistan)

**Gränssnitt:**
- Konsumerar: `Urval`, `OntologiConfig.schemaversion` (uppgift 1); `text_hash` (uppgift 2); `db.list_document_meta` och `graph.document_rows.count_pages` (befintliga).
- Producerar:
  - Tabell `ontology_extractions(pdf_stem TEXT NOT NULL, sida INTEGER NOT NULL, text_hash TEXT NOT NULL, schemaversion INTEGER NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL, raw TEXT NOT NULL, parsed_json TEXT NOT NULL, n_observationer INTEGER NOT NULL, extracted_at TEXT NOT NULL, PRIMARY KEY (pdf_stem, sida))`; `SCHEMA_VERSION = 15`.
  - `db.upsert_ontology_extraction(conn, row: Mapping[str, Any]) -> None` (sätter `extracted_at`, kodar `parsed` som JSON), `db.get_ontology_extraction(conn, pdf_stem, sida) -> dict | None` (`parsed` avkodad), `db.list_ontology_extractions(conn) -> list[dict]` (sorterad på stam, sida).
  - `series_of(kod: str) -> str` — de inledande versalerna i en uppslagsnyckel (`"EAE621-00"` → `"EAE"`, `"EA9982-00-D"` → `"EA"`).
  - `@dataclass(frozen=True) PageRef(pdf_stem: str, sida: int, skal: str)` där `skal` ∈ `serie|sok|uttrycklig`.
  - `select_pages(metas: list[dict], page_texts: Mapping[str, list[str]], urval: Urval, extractions: Mapping[tuple[str, int], dict], *, schemaversion: int, reviewed: set[tuple[str, int]], search_hits: list[tuple[str, int]], max_pages: int, force: bool) -> tuple[list[PageRef], dict[str, int]]`:
    - Kandidater: alla sidor i dokument vars `uppslag` har en kod med `series_of(kod)` exakt i `urval.serier` (skäl `serie`); `search_hits` (skäl `sok`); `urval.stammar` (alla sidor) och `urval.sidor` (skäl `uttrycklig`). Ett dokument utan text (saknas i `page_texts`) hoppas över.
    - Bortval: sidor med färre än 100 alfanumeriska tecken; sidor med en extraktion där `text_hash` och `schemaversion` stämmer; sidor med en extraktion med annan `schemaversion` men samma `text_hash` som finns i `reviewed` — utom med `force`. Ändrad text (annan `text_hash`) väljs alltid.
    - Ordning: uttryckliga först, sedan sök, sedan serie; inom varje grupp (stam, sida). Avkortas till `max_pages`. Statistiken räknar per bortvalsskäl och per urvalsskäl.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def test_series_of():
    assert series_of("EAE621-00") == "EAE" and series_of("EA9982-00-D") == "EA" and series_of("D4991-00") == "D"

def test_series_selects_only_exact_series(): ...      # dokument med EAE-kod väljs, D-kod inte, "EAEX" inte för serien "EAE"
def test_short_pages_are_skipped(): ...               # sida med 50 alfanumeriska tecken väljs inte
def test_same_text_and_version_is_not_reselected(): ...
def test_changed_text_is_reselected_even_if_reviewed(): ...
def test_new_schema_version_skips_reviewed_unless_force(): ...
def test_order_and_max_pages(): ...                    # uttrycklig före sök före serie; max_pages=2 → 2 sidor
def test_db_roundtrip_and_overwrite(conn): ...         # upsert två gånger för samma (stam, sida) → en rad, senaste värden, parsed avkodad
def test_fresh_db_is_version_15(tmp_path): ...
```

Lägg 14 i den förväntade versionslistan i `tests/test_db.py` och ändra hårdkodade `14` på samma sätt som när version 14 lades till.

- [ ] **Steg 2: Kör och se dem falla.**
- [ ] **Steg 3: Implementera** migration, db-funktioner och `selection.py`; dokumentera tabellen i state-db-avsnittet i `docs/teknisk-referens.md`.
- [ ] **Steg 4: Kör testerna** → PASS; `.venv/bin/python scripts/test.py --static` utan fel.

---

### Task 4: Inläsning av observationer i Neo4j

**Filer:**
- Skapa: `src/ontologi/load_graph.py`
- Test: `tests/test_ontologi_load_graph.py`

**Gränssnitt:**
- Konsumerar: `OntologiConfig` (uppgift 1); radformen från `db.list_ontology_extractions` (uppgift 3) med `parsed["observationer"]` i formen från `parse_response` (uppgift 2); fixturen `neo4j_session`.
- Producerar:
  - `ONTOLOGY_CONSTRAINTS: list[str]` — unikhet på `Observation.id`, `Vittne.id`, `MaskadUppgift.id`, `Händelse.id`, `Kartplats.id`, `ObserveradPerson.id`, `Egenskap.id`, `Nyckelperson.id` (`IF NOT EXISTS`).
  - `sync_reference_nodes(session, cfg: OntologiConfig) -> None` — `MERGE` `Händelse` (id, namn, ordning, tid_fran, tid_till), `Nyckelperson` (id, namn) och `Kartplats` (id = slug av namnet, namn, lat, lon) ur konfigurationen.
  - `observation_id(stem: str, sida: int, index: int, hash_: str) -> str` → `f"{stem}#{sida}#{index}#{hash_[:12]}"`.
  - `write_page(tx, extraction: dict) -> dict[str, int]` — en sida i en transaktion:
    1. Befintliga observationer med `KÄLLA` till sidan (`Sida.id` = `f"{stem}#{sida}"`) och **annan** `extraktion_hash`: de med status `ogranskad` tas bort tillsammans med sina `ObserveradPerson` och `Egenskap`; övriga sätts till `inaktuell`.
    2. Observationer med samma hash finns redan → inget skrivs om (idempotent via `MERGE` på id).
    3. Nya: `MERGE (o:Observation {id})` med `tid_fran`, `tid_till`, `tid_text`, `osakerhet`, `utdrag`, `extraktion_hash`, `schemaversion` och `ON CREATE SET o.status = 'ogranskad'`; `KÄLLA`→`Sida`; `LÄMNAD_AV`→ `Vittne {id: stem+'#'+namn_norm}` (namn, namn_norm, stem) eller `MaskadUppgift {id: stem+'#m'+index}` (stem, lokalt_id, kategori, roll) — vittne utan namn och utan maskering får ingen kant; `AVSER`→`Händelse` om id finns; `PLATS`→`Kartplats` om matchad, annars egenskapen `plats_ra` på observationen; `OBSERVERADE`→`ObserveradPerson {id: obs_id+'#p'+i}` → `SIGNALEMENT`→`Egenskap {id: person_id+'#s'+j}` och `KAN_VARA {status:'ogranskad'}`→`Nyckelperson`.
    - Saknas `Sida`-noden (dokumentlagret inte inläst) → `ValueError` med stam och sida.
  - `propose_links(session) -> dict[str, int]` — `SAMMA_PERSON {status:'ogranskad'}` mellan `Vittne` med samma `namn_norm` i olika dokument om ingen kant finns (aldrig `MaskadUppgift`); `SAMMA_UPPGIFT` mellan observationer i olika dokument som `AVSER` samma händelse och vars normaliserade utdrag har `difflib.SequenceMatcher` ≥ 0,9 (beräknas i Python ur en fråga per händelse).
  - `load_ontology(session, extractions: list[dict], cfg: OntologiConfig, *, ctx) -> dict[str, int]` — constraints, `sync_reference_nodes`, `write_page` per sida (`session.execute_write`), `propose_links`; loggar en info-rad `Ontologi: N sidor, M observationer (K nya, I inaktuella, B borttagna), S SAMMA_PERSON, U SAMMA_UPPGIFT` och varningar för sidor utan `Sida`-nod (räknas, körningen fortsätter).

- [ ] **Steg 1: Skriv de fallerande testerna** — fake-session-tester utan Neo4j (alla satser parametriserade, ingen sats innehåller `NÄMNER`/`RELATERAR`/`HAR_SIDA` i `DELETE`/`SET`, en transaktion per sida) och Neo4j-tester:

```python
@pytest.mark.neo4j
def test_load_creates_observation_graph(neo4j_session): ...
    # dokumentlager för stam "A" (1 sida) via graph.load_documents.write_document_layer; en extraktion med
    # ett namngivet vittne, händelse "skotten", plats Tunnelgatan, en observerad person med längd 180–185
    # och kan_vara garningsmannen → 1 Observation status 'ogranskad', KÄLLA→Sida A#1, AVSER, PLATS, Egenskap min 180
@pytest.mark.neo4j
def test_reload_same_text_is_idempotent(neo4j_session): ...
@pytest.mark.neo4j
def test_changed_text_replaces_unreviewed_and_marks_reviewed_stale(neo4j_session): ...
    # två observationer, sätt den ena till 'godkänd'; ladda ny extraktion med annan hash
    # → den ogranskade borta, den godkända 'inaktuell', nya observationer 'ogranskad'
@pytest.mark.neo4j
def test_same_person_proposed_but_never_for_masked(neo4j_session): ...
    # vittne "Anna Berg" i dokument A och B, maskat vittne i C → en SAMMA_PERSON A–B, ingen kant till MaskadUppgift
@pytest.mark.neo4j
def test_near_identical_quotes_get_samma_uppgift(neo4j_session): ...
@pytest.mark.neo4j
def test_missing_page_node_is_skipped_with_warning(neo4j_session): ...
```

- [ ] **Steg 2: Kör och se dem falla** (Neo4j-testen mot en testcontainer på port 17687 om podman fungerar: `podman machine start`; `podman run -d --rm --name palme-neo4j-test -p 17687:7687 -e NEO4J_AUTH=neo4j/palme-ci-test neo4j:5`; `NEO4J_TEST_URI=bolt://localhost:17687 NEO4J_TEST_PASSWORD=palme-ci-test PALME_REQUIRE_NEO4J_TESTS=1 .venv/bin/pytest -m neo4j tests/test_ontologi_load_graph.py -v`; stoppa containern efteråt).
- [ ] **Steg 3: Implementera** enligt gränssnittet.
- [ ] **Steg 4: Kör testerna** (fake och Neo4j) → PASS; `.venv/bin/python scripts/test.py --static` utan fel.

---

### Task 5: Operationen `extract-mordnatten`

**Filer:**
- Skapa: `src/ontologi/llm.py`, `src/ontologi/run.py`, `scripts/extract_mordnatten.py`, `extract_mordnatten.sh`
- Ändra: `src/operations/registry.py`, `src/operations/adapters.py`, `src/admin_ui.py` (`OPERATION_HELP`)
- Test: `tests/test_ontologi_run.py`, `tests/test_ontologi_llm.py`
- Dokumentation: `docs/teknisk-referens.md` (ontologin: filer, operation, kostnadsbroms), `AGENTS.md` (Commands + ett kort designbeslut: råsvar i SQLite, grafen är pekregister, maskningsreglerna, ingen påhittad uppgift når grafen), `docs/kom-igang.md` (en rad om piloten)

**Gränssnitt:**
- Konsumerar: allt ovan; `config.load_profile`/`load_all`/`resolve_runtime_profile` och `backends.BACKENDS` (som `graph/review_llm.py:177-198`); `graph.viz.resolve_password`; `rag.ask.search_hybrid(table, model, q, top_k)` för sökfrågorna (öppna tabellen och embedding-modellen som `rag.ask` gör; ingen reranker; laddas bara när `urval.sokfragor` inte är tom).
- Producerar:
  - `llm.call_page(prompt: str, system: str, cfg: dict) -> tuple[str, dict]` — Claude (`claude_agent_sdk.query`, `max_turns=1`, inga verktyg) eller OpenAI-kompatibelt (`chat.completions`, `max_tokens=4096`, `response_format={"type": "json_object"}` om backend är `openai`); returnerar text och `{input_tokens, output_tokens}` (None om okänt). Återförsök: upp till 4 försök vid 429/5xx/nätfel med väntan 2, 4, 8 s; 401/402/403 eller 429 efter sista försöket → `LlmFatal`.
  - `class LlmFatal(Exception)`.
  - `run_extract_mordnatten(*, profile: str, max_pages: int, dry_run: bool, force: bool, load_only: bool, jobs: int, uri: str, user: str, text_dir: Path, context=None) -> int`:
    - Profil löses först; okänd profil, backend som inte är `claude`/`openai`, eller saknad nyckel → `OperationFailed` före all LLM-kontakt (inte vid `dry_run` eller `load_only`).
    - `load_only`: hoppar över urval och LLM; läser in alla rader i `ontology_extractions`.
    - Urval via `select_pages` (sökfrågorna körs med topp 20 träffar per fråga, `source` → stam, `page` → sida). `reviewed` hämtas ur Neo4j (observationer med status ≠ `ogranskad`, per `(stam, sida)`); svarar inte Neo4j sätts `reviewed` tomt med en varning.
    - `dry_run`: loggar antal sidor per urvalsskäl, de 10 första sidorna och en kostnadsuppskattning `(tecken/4 + 1500) * input_pris + 800 * output_pris` per sida ur profilens priser (eller `kostnad okänd`); skriver ingenting.
    - Annars: sidorna körs med `asyncio` och högst `jobs` samtidiga anrop; varje svar tolkas med `parse_response` och sparas med `upsert_ontology_extraction` **direkt efter svaret**; `LlmFatal` stoppar nya anrop och ger `OperationFailed` efter att redan sparade svar lästs in; `ctx.check_cancelled()` mellan sidor.
    - Därefter `load_ontology` för de sidor som extraherades i körningen (eller alla vid `load_only`). Neo4j nere → råsvaren ligger kvar, varning `Grafen svarar inte — kör igen med --load-only när Neo4j är igång.` och exitkod 0.
    - Slutrad: `Mordnatten: E sidor extraherade (T token in, U ut), O observationer, V varningar.`
  - Registret: `id="extract-mordnatten"`, `label="Extrahera mordnatten"`, `group="Extraktion och graf"`, `description="Extrahera observationer från mordnatten ur utvalda sidor och läs in dem i grafen."`; parametrar `profile` (`--profile`, str, "", "LLM-konfiguration"), `max_pages` (`--max-pages`, int, 100, "Max antal sidor"), `dry_run` (`--dry-run`, bool, False, "Visa urval och kostnad utan att köra"), `force` (`--force`, bool, False, "Extrahera om även granskade sidor vid ny schemaversion"), `load_only` (`--load-only`, bool, False, "Bara inläsning i grafen"), `jobs` (`--jobs`, int, 4, "Parallella LLM-anrop"), `uri`, `user` (som `load-documents`), `text_dir` (`--text-dir`, path, `ROOT/"generated"/"text"`, "Text-katalog"); `admin_visible=True`, `mutating=True`, `confirmation="Extraktionen skickar sidtext till den valda LLM-profilen och kostar pengar. Kör först med Visa urval och kostnad."`.

- [ ] **Steg 1: Skriv de fallerande testerna** (LLM, sökning och Neo4j ersätts med fakes)

```python
def test_missing_api_key_fails_before_any_call(...): ...       # profil med api_key_env utan värde → OperationFailed, fake-LLM aldrig anropad
def test_dry_run_writes_nothing_and_logs_cost(...): ...        # inga rader i ontology_extractions, loggrad med "sidor" och kostnad eller "kostnad okänd"
def test_each_answer_is_saved_before_load(...): ...            # 3 sidor → 3 rader, load_ontology anropad med 3
def test_fatal_llm_error_stops_and_keeps_saved(...): ...       # andra anropet höjer LlmFatal → OperationFailed, första sidans rad finns
def test_neo4j_down_keeps_raw_and_returns_zero(...): ...       # load_ontology höjer ServiceUnavailable → rc 0, varning med "--load-only"
def test_load_only_skips_llm(...): ...
def test_retry_on_429_then_success(...): ...                   # tests/test_ontologi_llm.py: fake-klient 429, 429, ok → text, tre försök (sleep patchad)
def test_401_is_fatal_without_retry(...): ...
```

Befintliga `tests/test_scripts.py`, `tests/test_operation_registry.py` och `tests/test_admin_ui.py` ska falla tills registret, scriptet, genvägen och hjälptexten finns.

- [ ] **Steg 2: Kör och se dem falla.**
- [ ] **Steg 3: Implementera** enligt gränssnittet; dokumentera.
- [ ] **Steg 4: Kör testerna** → PASS; `.venv/bin/python scripts/test.py --static` utan fel.
- [ ] **Steg 5: Torrkörning mot riktiga data (läser bara, ingen LLM)**

`.venv/bin/python scripts/extract_mordnatten.py --dry-run --profile "<profil>"` — förväntat: urvalet räknas per skäl (serie/sök/uttrycklig), 100 sidor, kostnadsuppskattning. Rapportera siffrorna.

- [ ] **Steg 6: Piloten (bara efter användarens klartecken — kostar pengar)**

`.venv/bin/python scripts/jobs.py start extract-mordnatten --profile "<profil>"`. Förväntat: 100 sidor extraherade, observationer i grafen med status `ogranskad`. Kontroll: `MATCH (o:Observation) RETURN o.status, count(*)`, och att inga `MaskadUppgift` har `SAMMA_PERSON`.
