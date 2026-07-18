# SQLite-migrationer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gör `src/db.py` schemauppgraderingar versionsstyrda och testbara med en gammal SQLite-fixture.

**Architecture:** `init_schema()` behåller dagens färska schema men delegerar äldre uppgraderingar till en liten migrationslista. Migreringar körs i stigande version, är idempotenta och dokumenteras via `schema_version` samt `PRAGMA user_version`.

**Tech Stack:** Python 3.11+, sqlite3, pytest.

## Global Constraints

- Commita eller pusha inte; användaren kör `/cap` när hen är redo.
- Kommentarer och docstrings i projektkod är på svenska.
- Uppdatera användarvänd dokumentation och AGENTS.md om ändringen påverkar dem.
- Återinför inte borttagna legacy-skript som `migrate_to_db.py`.

---

### Task 1: Migrationstest och gammal fixture

**Files:**
- Create: `tests/fixtures/state_db_v4.sql`
- Create: `tests/fixtures/state_db_v5_missing_surya.sql`
- Modify: `tests/test_db.py`

**Interfaces:**
- Consumes: `db.init_schema`.
- Produces: testkrav för `schema_version(conn) -> int`, legacy-migrering och nyare-schema-skydd.

- [x] **Step 1: Skriv failing test**

Lägg till fixtures med schema version 4 respektive en äldre version 5 utan
`surya_failed_at`, samt tester som importerar `schema_version`, migrerar
databaserna och testar nyare-schema-skydd.

- [x] **Step 2: Verifiera röd fas**

Run: `.venv/bin/pytest tests/test_db.py::test_init_schema_migrates_legacy_v4_fixture -q`

Expected: failar eftersom `schema_version` saknas.

### Task 2: Versionsstyrd runner

**Files:**
- Modify: `src/db.py`

**Interfaces:**
- Produces:
  - `schema_version(conn: sqlite3.Connection) -> int`
  - `MIGRATIONS` med version 6-migreringen för OCR-felkolumnerna.

- [x] **Step 1: Implementera helper-funktioner**

Skapa helpers för att säkerställa `schema_version`, läsa tabeller/kolumner,
lägga till saknade kolumner och registrera tillämpad version.

- [x] **Step 2: Koppla in i `init_schema()`**

Färsk databas får `SCHEMA_SQL` och aktuell version direkt. Befintlig äldre
databas kör pending migrations; nyare databas kastar `RuntimeError`.

- [x] **Step 3: Verifiera grön fas**

Run: `.venv/bin/pytest tests/test_db.py::test_init_schema_migrates_v5_database_missing_surya_column tests/test_db.py::test_init_schema_migrates_legacy_v4_fixture tests/test_db.py::test_init_schema_rejects_newer_database -q`

Expected: testerna passerar.

### Task 3: Dokumentation och verifiering

**Files:**
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`
- Modify: `docs/superpowers/plans/2026-07-17-stadning-och-inforande.md`

**Interfaces:**
- Produces: dokumenterad migrationsmodell och markerar första Task 7-batchen som genomförd.

- [x] **Step 1: Uppdatera dokumentation**

Beskriv `schema_version`, `PRAGMA user_version` och att `init_schema()` kör
pending migrations. Lägg till de gamla state-db-fixturerna i filöversikten.

- [x] **Step 2: Kör verifiering**

Run:

```bash
.venv/bin/pytest tests/test_db.py tests/test_packaging.py -q
python3 -m compileall src/db.py
git diff --check
```

Expected: testerna passerar, compileall exit 0 och diff-check är tyst.
