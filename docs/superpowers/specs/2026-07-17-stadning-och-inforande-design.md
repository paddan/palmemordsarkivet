# Stadning och inforande av forbattrringar

**Datum:** 2026-07-17
**Status:** Godkand scope: Alt B

## Bakgrund

Projektet har vuxit fran pipeline-skript till ett komplett lokalt
utredningsverktyg med RAG, MCP, kunskapsgraf, karta och utredningsparm. Den
tekniska genomgangen 2026-07-17 hittade bade uppenbart lokalt brus och nagra
storre forbattrringsomraden. Anvandaren valde Alt B: bred dokumentationsstadning
plus en plan for inforande av forbattrringarna.

## Scope

Stadningen omfattar:

- Ignorerat lokalt brus: Python-cache, `.pytest_cache`, `.DS_Store` och
  editable-installationens egg-info.
- Aktiva shell-/Python-filer med gamla beskrivningar av artefakter eller
  kataloger.
- Historiska superpowers-planer/specifikationer dar gamla sokvagar som
  `src/webui.py`, `generated/state.db` och `files_wpu` forsvagrar sokbarheten.

Stadningen ska inte:

- Radera pipeline-data i `generated/`, `downloaded/`, `graphify-out/`, `.venv`,
  Neo4j-data eller tessdata.
- Andra pipeline-beteende.
- Revidera historiska planer sa hart att deras ursprungliga beslut forsvinner.
  Nar en plan ar historisk men fortfarande innehaller borttagna skript ska den
  markas som historisk i stallet.

## Inforandeprinciper

Forbattrringarna ska inforas i separata, testbara batchar:

1. Saker HTML-rendering for modell- och arkivsvar.
2. Validering av MCP-/tool-parametrar.
3. Reproducerbar dev-verifiering och CI/testscript.
4. Versionsstyrda SQLite-migrationer.
5. Gradvis uppdelning av `src/Utredning.py`.
6. Paketerings/importstadning.
7. Liten RAG-evalsvit med golden-fragor.
8. Battre LLM-batchsparning i state.db.

Varje batch ska uppdatera anvandarvand dokumentation bara nar beteende,
kommandon eller utvecklarflode faktiskt andras.

## Verifiering

Efter stadning ska minst foljande koras:

- `git status --short`
- `find`-kontroll for lokalt cachebrus
- `.venv/bin/pytest tests/`
- Syntaxkompilering av andrade Python-filer om pytest inte tackar importytan

`ruff` och `mypy` far inte anges som verifierade innan dev-beroenden finns i
miljon.
