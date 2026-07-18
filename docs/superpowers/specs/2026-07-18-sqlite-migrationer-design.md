# SQLite-migrationer

**Datum:** 2026-07-18
**Status:** Godkänd genom användarens "implementera" för 2026-07-17-spåret.

## Bakgrund

`src/db.py` äger hela SQLite-schemat för pipeline-state, utredningspärm,
kunskapsgraf och karta. Hittills har `init_schema()` skapat färskt schema med
`CREATE TABLE IF NOT EXISTS` och lagt till några sena kolumner via ad hoc
`ALTER TABLE`. Det fungerar för färska databaser men gör det svårt att förstå
vilka ändringar som hör till vilken schemaversion.

## Design

Inför en liten versionsstyrd migrationsrunner i `src/db.py`.

- `schema_version(conn)` returnerar högsta tillämpade schemaversionen.
- `init_schema(conn)` skapar färskt schema direkt när databasen är tom.
- Befintliga databaser får först saknade tabeller/index via det aktuella
  `SCHEMA_SQL`, och därefter körs pending migrations i versionsordning.
- Databaser med högre version än kodens `SCHEMA_VERSION` vägras med
  `RuntimeError`, så äldre kod inte tyst skriver mot ett nyare schema.
- `schema_version`-tabellen fortsätter lagra en rad per version som historik.
- `PRAGMA user_version` speglas till aktuell version för enkel inspektion med
  SQLite-verktyg.

Första migreringen är version 6 och samlar de befintliga OCR-felkolumnerna i
`pdf_files`: `tesseract_done_at`, `tesseract_failed`,
`tesseract_blacklisted_at` och `surya_failed_at`. Migreringen är idempotent och
kontrollerar kolumnlistan innan `ALTER TABLE`.

## Avgränsning

Det här ändrar inte pipeline-beteende eller tabellernas aktuella form. Det
lägger bara en tydligare uppgraderingsväg runt samma schema. Äldre
engångsskript som `migrate_to_db.py` återinförs inte.

## Testning

En fixture `tests/fixtures/state_db_v4.sql` beskriver en äldre databas med
`schema_version = 4` och `pdf_files` utan OCR-felkolumner. En andra fixture
`tests/fixtures/state_db_v5_missing_surya.sql` fångar databaser som redan är
märkta version 5 men saknar den sena `surya_failed_at`-kolumnen. Testerna
verifierar att `init_schema()` migrerar dem till aktuell version, bevarar
befintlig data, sätter `PRAGMA user_version` och vägrar en databas med version
högre än koden.
