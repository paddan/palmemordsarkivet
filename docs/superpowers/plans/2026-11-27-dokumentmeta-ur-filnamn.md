# Dokumentmetadata ur filnamn — implementationsplan

> **För agentiska arbetare:** KRÄVD UNDER-SKILL: använd superpowers:subagent-driven-development (rekommenderas) eller superpowers:executing-plans för att genomföra planen uppgift för uppgift. Stegen använder kryssrutor (`- [ ]`).

**Mål:** Ingest tolkar filnamnen rätt för både palme- och wpu-dokument och sparar den tolkade metadatan per dokument i state.db, som underlag för ontologin.

**Arkitektur:** En ny Streamlit-fri modul `src/dokumentmeta.py` tolkar en filstam efter form (datum, sidantal, koder) i stället för position. Ingest fyller tabellen `document_meta` i state.db för alla textfiler vid varje körning (ren strängtolkning, ingen embedding). LanceDB behåller bara `nr` och `titel`, byte-identiska med i dag; de fyra felaktigt tolkade kolumnerna tas bort.

**Teknik:** Python 3.11+, SQLite (`src/db/`), LanceDB 0.41, pytest.

**Spec:** ingen separat spec — underlaget är avsnittet *Underlag* nedan (mätt 2026-11-27 mot `downloaded/files`, `generated/text` och kalkylarket som `download.py` läser).

## Underlag

- Palme-filnamnen byggs av `download.build_filename` som `Nummer — Titel — Beställt — Upplagt/uppdaterat — Anmärkning — Antal sidor`, men **tomma kolumner hoppas över**, och namnet kapas vid 180 tecken (`maxlen`).
- `ingest.parse_filename` tolkar efter position. Formerna efter titeln i `downloaded/files` (D = datum, S = siffror, t = text): `DtS` 2 140, `DDtS` 851, `DS` 475, `Dt` 255, `Dtt` 42, `DDt` 16, `DDS` 13, `D` 11, `DDtt` 5, `tS` 1, tomt 2. Bara `DDtS` tolkas rätt i dag.
- Kalkylarket (3 834 rader med länk): Beställt och Upplagt ifyllda 890, **bara Upplagt 2 941**, bara Beställt 2, inget 1. Ett ensamt datum är alltså Upplagt.
- Ingen titel innehåller `—`. Anmärkningen är aldrig bara siffror. Antal sidor är tomt (252) eller `-` (66) när det saknas; anmärkningen är `-` i 3 fall.
- Nummerformer: `N.N` 1 902, `N` 1 889, `N,N` 31, `N.N.N` 8, `N-N` 2, `N.,N` 1, `NA` 1.
- 53 palme-stammar i `generated/text` har 180 tecken eller fler, dvs. är avkortade; några slutar mitt i ett datum (`2020-09-`).
- wpu-stammar i `generated/text` (6 821): `Pol-<datum>_…` 5 911, övriga 910 (`1986-03-01_Aftonbladet`, `Leif_Wilhelm_Berg_890405`). Datumformer: fullständigt 5 833, `YYYY-MM-DD` 72, `NNNN-MM-DD` 4, `NNNN-NN-DD` 2. Efter datumet följer ofta ett klockslag (`1245_`, `1330-1423_`) och en uppslagskod (`E9979-00-A`), ibland `OKÄND_UPPSLAGSKOD_`.
- `download_wpu.py` har redan `PALME_ID_RE`/`WPU_ID_RE`, `IdKey` och `palme_id_keys`/`wpu_id_keys`, som `merge_wpu.py` använder för att para palme- och wpu-dokument.
- Av LanceDB:s namnkolumner läses bara `nr` och `titel` (träffhuvudet i `ask.py:326`); `anmarkning` väljs i `ask.SELECT_COLS` och `Utredning._load_fuzzy_corpus` men används inte.

## Globala villkor

- Kommentarer, docstrings och loggrader på svenska.
- Ingen `git commit`/`git push` (AGENTS.md) — användaren kör `/cap` själv. Commit-stegen nedan ersätts av att köra testerna.
- All SQL i `src/db/`; nya db-funktioner re-exporteras i `src/db/__init__.py` (både import och `__all__`).
- Ny toppnivåmodul läggs i `py-modules` i `pyproject.toml`.
- LanceDB:s `nr` och `titel` ska vara **exakt** samma som dagens `parse_filename` gav för varje stam — citat, träffhuvuden och `citations.resolve_truncated` bygger på dem.
- Loggning via `ctx.log(..., level="debug")` som summeringar med första exemplen, aldrig en rad per fil.
- Verifiering: `.venv/bin/python scripts/test.py --static` (pytest + ruff + mypy) ska vara grönt efter varje uppgift.

## Utanför planen

- Datum och personer i palme-titlarna ("PM daterad 1987-02-09", "om Åke Lindsten") — kräver titeltolkning, tas i ontologiplanen.
- Syskonkoder i förkortad form (`T-8517-00, -A` och `E-637-04, -05, -06`): bara den första, fullständiga koden blir en nyckel.
- wpu-titlar i LanceDB:s `titel` (i dag tom för wpu) — kräver omskrivning av ~6 000 dokuments rader.
- Att läsa kalkylarket som källa. De 2 stammar med bara Beställt får fel fält och de 53 avkortade tappar sina sista fält; det accepteras.

## Fokus vid granskning

1. **Stam avkortad mitt i ett datum** (`… — 2020-09-15 — 2020-09-`): det halva datumet ska kastas med en varning och får aldrig bli anmärkning. Test i uppgift 1.
2. **Avkortad stam som slutar på siffror**: sidantalet kan vara avklippt (`4` av `48`) och ska bli `None` med en varning. Test i uppgift 1.
3. **Kommatecken i numret** (`281,10`): `nr` står kvar orört, och hänvisningar som `se 281,10, 707` delas bara på komma följt av blanksteg. Test i uppgift 1.
4. **Gammal LanceDB-tabell medan Streamlit kör**: sidans urval får inte kräva `anmarkning`, varken före eller efter att ingest tagit bort kolumnen. Test i uppgift 4.
5. **`only_stems` från omskanning** får uppdatera sin egen rad men aldrig rensa bort andra dokuments rader. Test i uppgift 4.

---

### Task 1: Modulen `dokumentmeta` och palme-tolkningen

**Filer:**
- Skapa: `src/dokumentmeta.py`
- Ändra: `src/download_wpu.py:36-92` (flytta ID-hjälparna, re-exportera)
- Ändra: `pyproject.toml` (`py-modules`: `"dokumentmeta"`)
- Test: `tests/test_dokumentmeta.py`

**Gränssnitt:**
- Producerar:
  - `MAX_STEM_CHARS = 180` (samma som `download.build_filename(maxlen=180)`)
  - `PALME_ID_RE`, `WPU_ID_RE`, `IdKey`, `palme_id_keys(filename: str) -> set[IdKey]`, `wpu_id_keys(filename: str) -> set[IdKey]` — flyttade oförändrade från `download_wpu.py`, som importerar dem tillbaka som uttrycklig re-export (`from dokumentmeta import palme_id_keys as palme_id_keys, …`, samma stil som `src/db/__init__.py`), så `merge_wpu.py` och `tests/test_download_wpu.py` inte ändras.
  - `uppslag_key_str(key: IdKey) -> str` — `f"{prefix}{base}-{version:02d}"` plus `f"-{suffix}"` om suffix finns; `("E", 63, 1, "")` → `"E63-01"`.
  - `@dataclass(frozen=True) class DokumentMeta` med fälten `pdf_stem: str`, `arkiv: Literal["palme", "wpu"]`, `nr: str | None`, `titel: str`, `bestallt: str | None`, `upplagt: str | None`, `anmarkning: str | None`, `antal_sidor: int | None`, `dokumentdatum: str | None`, `uppslag: tuple[str, ...]`, `hanvisningar: tuple[str, ...]`, `avkortat: bool`, `varningar: tuple[str, ...]` samt `to_row(self) -> dict[str, Any]` (alla fält utom `varningar`, tupler som listor).
  - `parse_stem(stem: str) -> DokumentMeta` — palme om stammen innehåller `" — "`, annars wpu (wpu-delen kommer i uppgift 2; till dess ger den `arkiv="wpu"`, `titel=stem`, övriga tomma).
  - `lance_name_fields(stem: str) -> dict[str, str]` — `{"nr": …, "titel": …}` exakt som dagens `parse_filename`: delarna är `stem.split(" — ")` med strip, `nr` = första delen (eller `stem[:40]` om den är tom), `titel` = andra delen eller `""`.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
from dokumentmeta import lance_name_fields, parse_stem, uppslag_key_str

def _legacy(stem):  # kopia av dagens ingest.parse_filename, bara nr/titel
    parts = [p.strip() for p in stem.split(" — ")]
    nr = parts[0] or stem[:40]
    return {"nr": nr, "titel": parts[1] if len(parts) > 1 else ""}

def test_two_dates_are_ordered_then_published():
    m = parse_stem("280.8 — Om vykort till Regeringskansliet — 2020-08-15 — 2020-10-21 — HI-7504-10 — 2")
    assert (m.arkiv, m.nr, m.bestallt, m.upplagt, m.anmarkning, m.antal_sidor) == \
        ("palme", "280.8", "2020-08-15", "2020-10-21", "HI-7504-10", 2)
    assert m.uppslag == ("HI7504-10",)

def test_single_date_is_published_not_ordered():
    m = parse_stem("1361 — Iakttagelser av tre män på Västerlånggatan under veckan innan mordet — 2021-12-11 — Z-8624 — 8")
    assert (m.bestallt, m.upplagt, m.anmarkning, m.antal_sidor) == (None, "2021-12-11", "Z-8624", 8)

def test_date_and_pages_without_remark():
    m = parse_stem("1295.347 — Ledningsgruppen diverse dokument 14 — 2022-03-14 — 19")
    assert (m.upplagt, m.anmarkning, m.antal_sidor) == ("2022-03-14", None, 19)

def test_date_and_remark_without_pages():
    m = parse_stem("2060 — Grandbesökare — 2023-08-30 — L-1172-00")
    assert (m.anmarkning, m.antal_sidor, m.uppslag) == ("L-1172-00", None, ("L1172-00",))

def test_dash_placeholder_means_unknown_pages():
    m = parse_stem("667.1 — Förhör med statsminister Ingvar Carlsson, annan maskning — 2021-10-19 — T-8517-00, -A, se 1181 — -")
    assert (m.anmarkning, m.antal_sidor, m.hanvisningar) == ("T-8517-00, -A, se 1181", None, ("1181",))

def test_references_split_on_comma_space_only():
    m = parse_stem("29.2 — Utlåtande från NFC — Se 29, 564, 281,10, 678.1 — 7")
    assert (m.upplagt, m.hanvisningar, m.antal_sidor) == (None, ("29", "564", "281,10", "678.1"), 7)

def test_comma_in_number_is_kept_verbatim():
    assert parse_stem("281,10 — Titel — 2021-01-01 — 3").nr == "281,10"

def test_truncated_stem_drops_half_date():
    head, tail = "120 — ", " — 2020-09-15 — 2020-09-"
    stem = head + "x" * (180 - len(head) - len(tail)) + tail
    assert len(stem) == 180
    m = parse_stem(stem)
    assert m.avkortat and m.anmarkning is None and m.upplagt == "2020-09-15" and m.varningar

def test_truncated_stem_does_not_trust_trailing_digits():
    stem = "32 — " + "y" * 150 + " — 2020-06-22 — 2020-07-14 — Alla fantombilder — 4"
    m = parse_stem(stem)
    assert len(stem) >= 180 and m.antal_sidor is None and m.varningar

def test_lance_fields_match_legacy_parser():
    for stem in ("1 — PM — 2020-06-12 — 2020-07-14 — E-63-1, se 531.1 — 10",
                 "2208 — Presskonferens — 2025-04-30", "281,10 — T",
                 "Pol-1986-03-04_E9979-00-A_VITTNESFÖRHÖR-Hans-Johansson", "ABC_Oxen_skiss"):
        assert lance_name_fields(stem) == _legacy(stem)

def test_uppslag_key_str():
    assert uppslag_key_str(("E", 63, 1, "")) == "E63-01"
    assert uppslag_key_str(("EDE", 9980, 0, "A")) == "EDE9980-00-A"
```

- [ ] **Steg 2: Kör testerna och se dem falla**

Kör: `.venv/bin/pytest tests/test_dokumentmeta.py -v`
Förväntat: FAIL med `ModuleNotFoundError: No module named 'dokumentmeta'`

- [ ] **Steg 3: Implementera modulen**

Palme-tolkningen (algoritmen som testerna inte helt bestämmer):

```
parts = [p.strip() for p in stem.split(" — ")]
nr, titel, rest = parts[0], (parts[1] if len(parts) > 1 else ""), parts[2:]
avkortat = len(stem) >= MAX_STEM_CHARS
om avkortat och rest[-1] är ett påbörjat men ofullständigt datum (^\d{4}(-\d{0,2}){0,2}$ men inte ^\d{4}-\d{2}-\d{2}$):
    släpp rest[-1], varning "avkortat datum"
datum = inledande delar i rest som är ^\d{4}-\d{2}-\d{2}$, högst två
    två → bestallt, upplagt; ett → upplagt (kalkylarket: 2 941 mot 2)
om sista kvarvarande del är ^\d+$ eller "-":
    avkortat och siffror → antal_sidor None, varning "sidantal kan vara avklippt"
    annars siffror → int, "-" → None
anmarkning = " — ".join(kvar) eller None; fler än en del → varning
uppslag = sorterade uppslag_key_str(k) för k i palme_id_keys(anmarkning or "")
hanvisningar = token ur anmärkningen efter (?i)\bse\s+, delade på r",\s+",
    tagna så länge de matchar ^\d+(?:[.,]\d+)*[A-Za-z]?$
dokumentdatum = None för palme
```

Logga ingenting i modulen; varningarna returneras och loggas av ingest.

- [ ] **Steg 4: Kör testerna**

Kör: `.venv/bin/pytest tests/test_dokumentmeta.py tests/test_download_wpu.py tests/test_packaging.py -v`
Förväntat: PASS

- [ ] **Steg 5: Statisk kontroll**

Kör: `.venv/bin/python scripts/test.py --static`
Förväntat: inga fel.

---

### Task 2: wpu-tolkningen

**Filer:**
- Ändra: `src/dokumentmeta.py`
- Test: `tests/test_dokumentmeta.py`

**Gränssnitt:**
- Konsumerar: `DokumentMeta`, `WPU_ID_RE`, `wpu_id_keys`, `uppslag_key_str` från uppgift 1.
- Producerar: `parse_stem` för stammar utan `" — "` med `arkiv="wpu"`, `nr=None`, `bestallt`/`upplagt`/`anmarkning`/`antal_sidor`=None, `hanvisningar=()`.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def test_wpu_full_date_code_and_title():
    m = parse_stem("Pol-1986-03-04_E9979-00-A_VITTNESFÖRHÖR-Hans-Johansson")
    assert (m.arkiv, m.nr, m.dokumentdatum, m.uppslag, m.titel) == \
        ("wpu", None, "1986-03-04", ("E9979-00-A",), "VITTNESFÖRHÖR-Hans-Johansson")

def test_wpu_time_token_stays_in_title():
    m = parse_stem("Pol-1989-06-14_1245_KD11596-27_Telförhör_med_Jimmy_Lindström_om_Algot_Åsell")
    assert m.uppslag == ("KD11596-27",)
    assert m.titel == "1245 Telförhör med Jimmy Lindström om Algot Åsell"

def test_wpu_unknown_date_and_partial_date():
    assert parse_stem("Pol-YYYY-MM-DD_E167-01_Per_Vallin").dokumentdatum is None
    assert parse_stem("Pol-1986-MM-DD_Mötesprotokoll").dokumentdatum == "1986"
    assert parse_stem("Pol-1986-03-DD_Mötesprotokoll").dokumentdatum == "1986-03"

def test_wpu_invalid_date_is_none_with_warning():
    m = parse_stem("Pol-1986-02-30_Mötesprotokoll")
    assert m.dokumentdatum is None and m.varningar

def test_wpu_unknown_code_prefix_is_removed():
    m = parse_stem("Pol-YYYY-MM-DD_OKÄND_UPPSLAGSKOD_VITTNESFÖRHÖR-Nicola-Fauzzi")
    assert (m.uppslag, m.titel) == ((), "VITTNESFÖRHÖR-Nicola-Fauzzi")

def test_wpu_without_code():
    m = parse_stem("Pol-1986-11-24_Mötesprotokoll-Ledningsgruppen")
    assert (m.uppslag, m.titel) == ((), "Mötesprotokoll-Ledningsgruppen")

def test_non_pol_stems():
    m = parse_stem("1986-03-01_Aftonbladet")
    assert (m.dokumentdatum, m.titel) == ("1986-03-01", "Aftonbladet")
    m = parse_stem("Leif_Wilhelm_Berg_890405")
    assert (m.dokumentdatum, m.titel, m.uppslag) == (None, "Leif Wilhelm Berg 890405", ())
```

- [ ] **Steg 2: Kör testerna och se dem falla**

Kör: `.venv/bin/pytest tests/test_dokumentmeta.py -k "wpu or non_pol" -v`
Förväntat: FAIL (wpu ger än så länge bara `titel=stem`).

- [ ] **Steg 3: Implementera wpu-delen**

Regler:
- `^Pol-(\d{4}|YYYY)-(\d{2}|MM)-(\d{2}|DD)_(.*)$`. Datum med reducerad precision: alla tre kända → validera med `datetime.date.fromisoformat` (ogiltigt → `None` + varning); år och månad → `"YYYY-MM"`; bara år → `"YYYY"`; `YYYY` → `None`.
- I resten: ta bort ett inledande `OKÄND_UPPSLAGSKOD_`. Uppslagskoden är den första `WPU_ID_RE`-träffen som börjar direkt i resten eller efter ett klockslag `^\d{4}(?:-\d{4})?_`; dess nycklar (`wpu_id_keys` på träffens text) blir `uppslag`, och koden plus följande `_` tas bort ur titeln. Klockslaget står kvar.
- Övriga stammar: ett inledande `^\d{4}-\d{2}-\d{2}_` blir `dokumentdatum` och tas bort; ingen kod tolkas.
- `titel`: `_` → mellanslag, strip. Bindestreck lämnas (de används både som mellanslag och i namn).

- [ ] **Steg 4: Kör testerna**

Kör: `.venv/bin/pytest tests/test_dokumentmeta.py -v`
Förväntat: PASS

- [ ] **Steg 5: Kontrollera mot riktiga stammar**

Kör:
```bash
.venv/bin/python -c "import sys,collections;sys.path.insert(0,'src');import pathlib,dokumentmeta as d;ms=[d.parse_stem(p.stem) for p in pathlib.Path('generated/text').glob('*.txt')];print(collections.Counter(m.arkiv for m in ms));print('wpu med datum',sum(1 for m in ms if m.arkiv=='wpu' and m.dokumentdatum));print('wpu med kod',sum(1 for m in ms if m.arkiv=='wpu' and m.uppslag));print('palme anm=siffror',sum(1 for m in ms if m.anmarkning and m.anmarkning.isdigit()));print('lance avviker',sum(1 for p in pathlib.Path('generated/text').glob('*.txt') if d.lance_name_fields(p.stem)!={'nr':([x.strip() for x in p.stem.split(' — ')][0] or p.stem[:40]),'titel':([x.strip() for x in p.stem.split(' — ')]+[''])[1]}))"
```
Förväntat: inget undantag; ungefär 3 540 palme och 6 821 wpu; wpu med datum ≥ 5 833; wpu med kod ungefär 5 000–5 300 (`WPU_ID_RE` kräver tvåsiffrig version, så `EAD1094-C` räknas inte); `palme anm=siffror 0`; `lance avviker 0`.

---

### Task 3: Tabellen `document_meta` i state.db

**Filer:**
- Ändra: `src/db/_schema.py` (`SCHEMA_VERSION = 14`, `DOCUMENT_META_SCHEMA_SQL` i `SCHEMA_SQL`, `_migration_014_document_meta`, ny post i `MIGRATIONS`)
- Ändra: `src/db/pipeline.py`, `src/db/__init__.py`
- Ändra: `tests/test_db.py:238` (lägg till `13` i den förväntade versionslistan)
- Ändra: `docs/teknisk-referens.md` (state-db-avsnittet)
- Test: `tests/test_db_document_meta.py`

**Gränssnitt:**
- Konsumerar: radformen från `DokumentMeta.to_row()` (uppgift 1) — db-lagret importerar inte `dokumentmeta`.
- Producerar:
  - Tabell `document_meta(pdf_stem TEXT PRIMARY KEY, arkiv TEXT NOT NULL CHECK(arkiv IN ('palme','wpu')), nr TEXT, titel TEXT NOT NULL, bestallt TEXT, upplagt TEXT, anmarkning TEXT, antal_sidor INTEGER, dokumentdatum TEXT, uppslag_json TEXT NOT NULL DEFAULT '[]', hanvisningar_json TEXT NOT NULL DEFAULT '[]', avkortat INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)`.
  - `upsert_document_meta(conn, rows: Iterable[Mapping[str, Any]]) -> int` — `uppslag`/`hanvisningar` (listor) kodas till JSON, `avkortat` till 0/1; returnerar antal rader; en transaktion och commit som `record_ingest`.
  - `prune_document_meta(conn, keep: Collection[str]) -> int` — tar bort rader vars stam inte finns i `keep`. Läs befintliga stammar och radera skillnaden med `executemany` (inte `NOT IN (?, …)` — ~10 000 parametrar).
  - `get_document_meta(conn, pdf_stem: str) -> dict[str, Any] | None` — listor avkodade, `avkortat` som `bool`.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
def test_fresh_db_has_document_meta(tmp_path): ...  # init_schema → tabellen finns, schema_version == 14
def test_migration_from_13_adds_table(tmp_path): ...  # databas på v13 (följ mönstret i tests/test_db.py:230-260) → tabellen finns efter init_schema
def test_upsert_roundtrip_and_overwrite(conn):
    row = {"pdf_stem": "1 — PM", "arkiv": "palme", "nr": "1", "titel": "PM", "bestallt": None,
           "upplagt": "2020-07-14", "anmarkning": "E-63-1, se 531.1", "antal_sidor": 10,
           "dokumentdatum": None, "uppslag": ["E63-01"], "hanvisningar": ["531.1"], "avkortat": False}
    assert db.upsert_document_meta(conn, [row]) == 1
    assert db.get_document_meta(conn, "1 — PM")["uppslag"] == ["E63-01"]
    db.upsert_document_meta(conn, [{**row, "antal_sidor": 11}])
    assert db.get_document_meta(conn, "1 — PM")["antal_sidor"] == 11
def test_prune_keeps_listed_stems(conn): ...  # tre rader, keep två → 1 raderad, de två finns kvar
def test_get_missing_returns_none(conn): assert db.get_document_meta(conn, "saknas") is None
```

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_db_document_meta.py -v`
Förväntat: FAIL (`AttributeError: module 'db' has no attribute 'upsert_document_meta'`).

- [ ] **Steg 3: Implementera schema, migration och funktioner** enligt gränssnittet; re-exportera de tre funktionerna i `src/db/__init__.py`.

- [ ] **Steg 4: Dokumentera** tabellen i state-db-avsnittet i `docs/teknisk-referens.md`: kolumnerna, att den fylls av ingest ur filnamnen och att den är ren tolkning (ingen källtext).

- [ ] **Steg 5: Kör testerna**

Kör: `.venv/bin/pytest tests/test_db_document_meta.py tests/test_db.py tests/test_db_package.py tests/test_sql_boundary.py -v`
Förväntat: PASS

---

### Task 4: Ingest fyller `document_meta`, och LanceDB tappar de felaktiga kolumnerna

**Filer:**
- Ändra: `src/rag/ingest.py` (ta bort `NAME_FIELDS`/`parse_filename`; `_chunk_schema`, `_oppna_tabell`, `run_ingest`, ny `_uppdatera_dokumentmeta`)
- Ändra: `src/rag/ask.py:92` och `src/Utredning.py:289` (ta bort `"anmarkning"` ur kolumnlistorna)
- Ändra: `docs/teknisk-referens.md` (ingest-avsnittet, filöversikten: `dokumentmeta.py`), `AGENTS.md` (se steg 6)
- Test: `tests/test_ingest_document_meta.py`

**Gränssnitt:**
- Konsumerar: `dokumentmeta.parse_stem`, `dokumentmeta.lance_name_fields` (uppgift 1–2); `db.upsert_document_meta`, `db.prune_document_meta` (uppgift 3).
- Producerar:
  - `LANCE_NAME_FIELDS = ("nr", "titel")` och `DROPPED_NAME_FIELDS = ("bestallt", "upplagt", "anmarkning", "antal_sidor")` i `ingest.py`.
  - `_uppdatera_dokumentmeta(state_conn, text_dir: Path, only_stems: Collection[str] | None, ctx) -> int` — tolkar `sorted(text_dir.glob("*.txt"))` (eller bara `only_stems`), upsertar, och rensar med `prune_document_meta` **bara** när `only_stems is None`. Returnerar antal rader.

- [ ] **Steg 1: Skriv de fallerande testerna**

```python
class _Ctx:
    def __init__(self): self.lines = []
    def log(self, msg, level="info"): self.lines.append((level, msg))

def test_meta_refresh_covers_all_text_files(tmp_path, monkeypatch): ...
    # STATE_DB i tmp; två .txt (en palme-stam, en wpu-stam) → 2 rader, rätt arkiv och upplagt
def test_meta_refresh_prunes_removed_files(tmp_path, monkeypatch): ...
    # rad för stam som inte längre finns som .txt försvinner vid full körning
def test_only_stems_updates_one_and_prunes_nothing(tmp_path, monkeypatch): ...
    # tre rader finns; only_stems={en} → den uppdateras, de andra två finns kvar
def test_meta_refresh_logs_summary_and_warnings(tmp_path, monkeypatch): ...
    # en avkortad stam → en info-rad med antal per arkiv och en debug-rad som nämner varningen
def test_old_table_loses_misparsed_columns(tmp_path):
    # skapa en lancedb-tabell med gamla schemat (liten vektordimension räcker) och en rad;
    # _oppna_tabell(db, False, _Ctx()) → inga DROPPED_NAME_FIELDS kvar, nr/titel och raden finns kvar
def test_new_table_schema_has_only_nr_and_titel():
    names = ingest._chunk_schema().names
    assert "nr" in names and "titel" in names and not set(ingest.DROPPED_NAME_FIELDS) & set(names)
def test_chunk_rows_keep_legacy_nr_and_titel():
    # _chunka_fil med meta=lance_name_fields(stem) → varje rad har exakt nyckelmängden
    # {"text","source","page","chunk_idx","mtime","nr","titel"}
def test_select_columns_exclude_dropped_fields():
    from rag import ask
    assert not set(ingest.DROPPED_NAME_FIELDS) & set(ask.SELECT_COLS)
```

Lägg också till i `tests/test_utredning_ui.py` (eller där fuzzy-korpusen redan testas) att `_load_fuzzy_corpus`s kolumnlista saknar `anmarkning`, om den går att nå utan att starta sidan; annars räcker `ask.SELECT_COLS`-testet plus en `grep`-kontroll i steg 5.

- [ ] **Steg 2: Kör och se dem falla**

Kör: `.venv/bin/pytest tests/test_ingest_document_meta.py -v`
Förväntat: FAIL (`AttributeError: … '_uppdatera_dokumentmeta'`).

- [ ] **Steg 3: Implementera**

- `_chunk_schema` använder `LANCE_NAME_FIELDS`; `run_ingest` skickar `meta = lance_name_fields(f.stem)` till `_chunka_fil`.
- `_oppna_tabell`: finns någon av `DROPPED_NAME_FIELDS` i en befintlig tabell → `table.drop_columns([...de som finns])` och `ctx.log("Migrerar tabell: tar bort namnfält som tolkades efter position (…)")`. Samma ställe som dagens mtime-migrering.
- `run_ingest`: anropa `_uppdatera_dokumentmeta` direkt efter `state_db.init_schema(state_conn)` och **före** embedding-modellen laddas, så metadatan uppdateras även när inget behöver indexeras.
- Loggning i `_uppdatera_dokumentmeta`: en info-rad `Dokumentmetadata: N palme, M wpu, K avkortade, P utan sidantal, R borttagna`; varningarna grupperade per text på debug-nivå med antal och högst fem stammar.

- [ ] **Steg 4: Kör testerna**

Kör: `.venv/bin/pytest tests/test_ingest_document_meta.py tests/test_reingest.py tests/test_chunk.py tests/test_rescan_operation.py tests/test_operation_adapters.py tests/test_utredning_ui.py -v`
Förväntat: PASS

- [ ] **Steg 5: Hela sviten och statisk kontroll**

Kör: `.venv/bin/python scripts/test.py --static`
Förväntat: inga fel. Kör också `grep -rn "anmarkning\|parse_filename\|NAME_FIELDS" src tests` — förväntat: bara `DROPPED_NAME_FIELDS`, testerna och `dokumentmeta`-fältet `anmarkning`.

- [ ] **Steg 6: Dokumentera**

- `docs/teknisk-referens.md`: ingest-avsnittet — att filnamnen tolkas efter form av `dokumentmeta.parse_stem`, att ett ensamt datum är *Upplagt/uppdaterat*, att `document_meta` fylls för alla textfiler före indexeringen och att LanceDB bara bär `nr`/`titel`; filöversikten får `src/dokumentmeta.py`.
- `AGENTS.md`: i state-db-stycket nämns `document_meta`; ett kort designbeslut under *Non-obvious Design Decisions*: filnamnen tolkas efter form, inte position (tomma kalkylarkskolumner hoppas över i `build_filename`); LanceDB:s `nr`/`titel` ska förbli byte-identiska med den gamla tolkningen eftersom citaten bygger på dem; `document_meta` är källan för övriga fält.

- [ ] **Steg 7: Verifiera mot riktiga data (efter att användaren godkänt en ingest-körning)**

Kör: `.venv/bin/python scripts/ingest.py --limit 1`
Förväntat: loggen visar `Migrerar tabell: tar bort namnfält …` (första gången) och `Dokumentmetadata: ~3540 palme, ~6821 wpu, ~53 avkortade, …`. Kontrollera sedan:

```bash
sqlite3 "file:generated/db/state.db?immutable=1" "select arkiv, count(*), sum(upplagt is not null), sum(antal_sidor is not null), sum(uppslag_json != '[]') from document_meta group by arkiv"
```

Förväntat: palme-raderna har `upplagt` för nästan alla (alla utom de utan datum), wpu-raderna har uppslag för ungefär 5 300. Starta om `./web.sh` och gör en sökning i RAG-läget — träffhuvudena ska se ut som förut.
