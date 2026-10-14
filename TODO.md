# TODO — fynd från kodgranskning 2026-10-13

Ärenden från den breda kodgranskningen (arkitektur, prestanda, buggletande) av
hela `src/`, `scripts/` och `tests/`. Ingen diff/branch-specifik granskning.
Statisk analys (`scripts/test.py --static`) var helt ren vid granskningstillfället:
835 tester gröna, ruff och mypy utan anmärkning.

Numreringen matchar granskningsrapporten. Status uppdateras manuellt när ett
ärende är klart (`[ ]` → `[x]`).

## Implementationsplan

**Fas 1 — Snabba fixar, billiga och berör permanent lagrad data (gör nu):**
ärende 1, 2, 5, 6. Alla är lokala ändringar utan arkitekturpåverkan.

**Fas 2 — Testtäckning för redan korrekt men overifierad kod:**
ärende 3, 4. Ingen produktionskod ändras, bara nya tester.

**Fas 3 — UI-robusthet (kräver lite mer eftertanke kring Streamlit session_state):**
ärende 7, 14.

**Fas 4 — Städning och dokumentationsdrift (gör vid närliggande arbete, inget bråttom):**
ärende 8–13, 15, 16.

Beroenden: inga ärenden blockerar varandra. 2 bör göras tillsammans med sin
regressionstest (naturlig del av samma ändring, inte en separat Fas 2-post).

---

## Fas 1

### 1. [Allvarlig] 0-byte textfil kastar bort OCR-resultat tyst
- **Fil:** `src/merge_pages.py:46`
- **Problem:** `merge_text(original='', page_updates={...})` returnerar `""` direkt
  när en befintlig `.txt`-fil är 0 bytes. `create_missing=True` skyddar bara mot
  att filen saknas helt, inte mot att den finns men är tom.
- **Konsekvens:** I `run_surya_fallback` (`src/operations/ocr.py:89-118`) markeras
  dokumentet `mark_surya_failed` + `mark_tesseract_blacklisted` — permanent
  svartlistat — trots att Surya lyckades och texten ligger oanvänd i `pdf_pages`.
- **Fix:** Behandla "inget att slå ihop mot" som avsaknad av *både* `original`
  och `page_updates`, inte bara tom `original`-sträng.
- **Verifiering:** Regressionstest i `tests/test_merge_pages.py`: tom
  `text/<stem>.txt` + ett `page_updates`-objekt ska ge sammanslagen text, inte `""`.
- [x] Klar

### 2. [Allvarlig] build_user_words-cache nollställs av en borttagen fil
- **Fil:** `src/build_user_words.py:130`
- **Problem:** `gone = set(cached_mtimes) - {f.name for f in all_files}` → hela
  `counter` och `cached_mtimes` nollställs så fort en fil försvinner, i stället
  för att bara dra bort dess bidrag.
- **Konsekvens:** `merge_wpu` raderar rutinmässigt förlorarens textfil vid
  wpu/palme-dubbletter i varje full pipeline-körning — den "inkrementella"
  cachen gör i praktiken en full omläsning av alla ~8 665 dokument varje gång.
- **Fix:** Lagra per-fil ordräkningar (inte bara en global summa) så en
  borttagen fil kan subtraheras exakt utan att tömma hela cachen.
- **Verifiering:** Nytt `tests/test_build_user_words.py` (finns inte idag):
  happy path, inkrementell körning efter en borttagen fil (cachen ska behållas
  för kvarvarande filer), saknad `text_dir`, hunspell-fallback.
- [x] Klar

### 5. [Allvarlig] Dokumentation pekar på scripts/ask.py som inte finns
- **Fil:** `docs/teknisk-referens.md:541`
- **Problem:** Hänvisar till `scripts/ask.py --mcp`; rätt sökväg är `src/rag/ask.py`.
- **Fix:** Ändra till `.venv/bin/python src/rag/ask.py --mcp`.
- **Verifiering:** Manuell körning av det uppdaterade kommandot.
- [x] Klar

### 6. [Allvarlig] Fel LLM-profil attribueras i Utredningspärmen
- **Fil:** `src/Utredning.py:1907`, `:1678`
- **Problem:** Sparade poster i `casebook_entries` använder sidofältets
  *aktuella* `llm_profile` vid sparningstillfället, inte profilen som faktiskt
  genererade svarstexten.
- **Konsekvens:** Fråga med profil A → byt till B utan ny fråga → spara i pärm
  → posten märks felaktigt `backend=B, model=B`. Dataintegritetsbrist i en app
  byggd för källspårbarhet.
- **Fix:** Fånga profilnamn/backend vid svarsgenereringen och lagra det
  tillsammans med svaret i session_state, så sparningen läser därifrån istället
  för det globala sidofältsvalet.
- **Verifiering:** AppTest-scenario: generera svar med profil A, byt till B,
  spara, kontrollera att `casebook_entries.model` fortfarande är A.
- [x] Klar

## Fas 2

### 3. [Allvarlig] Central felloggningsfunktion saknar test
- **Fil:** `src/errors_log.py`
- **Problem:** `log_error()` — formatet Admin → Logg läser — har ingen egen test.
- **Fix:** Nytt `tests/test_errors_log.py`: format (`ISO8601\tcomponent\titem\tmessage`),
  nyradsersättning med `" | "`, rotation vid 10 MB.
- [x] Klar

### 4. [Allvarlig] OCR-orkestrering körs aldrig av testsviten
- **Fil:** `src/ocr_pages.py` (`run_ocr_pages`, `detect_redactions_file`)
- **Problem:** Bara lågnivåhjälpare testas isolerat; `test_ocr_operation.py`
  mockar bort subprocessanropet helt.
- **Fix:** Lägg till ett integrationstest som kör `run_ocr_pages` mot en liten
  fixture-PDF och verifierar Surya-fallback-logiken (en sida redan försökt i
  `pdf_pages` ska inte köras om).
- [x] Klar (avgränsat till `detect_redactions_file`-orkestreringen; `run_ocr_pages`s
  OCR-motordispatch kvarstår otestad, se uppföljning nedan)

## Fas 3

### 7. [Allvarlig] Obegränsad chatthistorik för OpenAI-kompatibla backends
- **Fil:** `src/Utredning.py:916`, `:1245-1390`
- **Problem:** `openai_chat_messages` växer okontrollerat för DeepSeek/OpenAI/
  Ollama-profiler (Claude-vägen slipper detta via `resume=session_id`).
- **Konsekvens:** Kostnad och tokenanvändning växer linjärt per tur tills
  modellens kontextgräns nås; konversationen blir successivt obrukbar utan
  förvarning.
- **Fix:** Egen uppgift — kräver beslut om kompakteringsstrategi (t.ex. glidande
  fönster + sammanfattning av äldre turer). Inte en enradsfix.
- [x] Klar (implementerad som trimning vid turgräns, 20 turers fönster)

### 14. [Mindre] Två selectbox-val saknar skydd mot tyst reset
- **Fil:** `src/Utredning.py:519` (`mcp_web_search_profile`),
  `src/pages/7_Karta.py:416-439` (`karta_source_pick`)
- **Problem:** Saknar samma skyddsmönster mot Streamlits tysta
  fallback-till-första-alternativ som redan finns för `llm_profile` och
  `karta_selected_observation`.
- **Fix:** Applicera samma `if ... not in options: reset`-mönster.
- [x] Klar

## Fas 4

### 8. [Mindre] Jobblogg kan få omkastad radordning vid parallella jobb
- **Fil:** `src/operations/context.py:41`
- **Fix:** Lås kring `JobSink.write_log`, eller skriv radnummer/tidsstämpel med
  tillräcklig upplösning för att sortera vid läsning.
- [x] Klar

### 9. [Mindre] Verktygs-synktest kollar bara namn, inte parametrar
- **Fil:** `tests/test_mcp_server.py:370`
- **Fix:** Utöka `test_openai_verktygsscheman_matchar_mcp_verktygen` till att
  jämföra defaults/gränser (t.ex. `top_k`, `top_n`, `max_results`), inte bara
  verktygsnamnen.
- [x] Klar

### 10. [Mindre] Upprepad okachad diskläsning per websökning
- **Fil:** `src/rag/mcp_server.py:199`
- **Fix:** Cacha `generated/llm_config.json`-innehållet per process (med enkel
  mtime-koll) istället för att läsa om det vid varje delanrop.
- [x] Klar. Notering: `set_web_search_profile` invaliderar inte cachen —
  ofarligt idag eftersom Utredning.py alltid anropar `set_web_search_budget`
  (som nollställer cachen) före `set_web_search_profile` i varje fråga, men
  en framtida ändrad anropsordning kan ge en inaktuell profil inom samma fråga.

### 11. [Mindre] Efterkontroll kan inte återställa en commit
- **Fil:** `src/graph/review_service.py:275`
- **Notering:** Avsiktligt designval enligt AGENTS.md. Ingen kodändring krävs
  nu, men dokumentera risken tydligare i koden/AGENTS.md om en framtida
  multi-writer-situation blir aktuell.
- [x] Klar / bedömd som ej aktuell — inget kodändring gjord (se notering ovan)

### 12. [Mindre] Entrypoint-test verifierar mängd, inte koppling
- **Fil:** `tests/test_scripts.py`
- **Fix:** Skärp `test_every_operation_is_reachable_from_a_shell_shortcut` till
  att verifiera att `scripts/<id>.py` faktiskt anropar `run("<id>")` för sitt
  eget id, inte bara att id:t förekommer någonstans i trädet.
- [x] Klar

### 13. [Mindre] AGENTS.md:s filträd saknar åtta src-moduler
- **Fil:** `AGENTS.md`
- **Fix:** Lägg till `backends.py`, `citations.py`, `config.py`,
  `download_wpu.py`, `llm_config_cli.py`, `llm_usage.py`, `merge_wpu.py`,
  `ocr_db_helper.py` i katalogöversikten.
- [x] Klar

### 15. [Mindre] PDF-uppslagslogik triplicerad, en kopia död kod
- **Fil:** `src/Utredning.py:255-269`, `src/casebook_ui.py:26-40`,
  `src/pages/3_Graf.py:74-80`
- **Fix:** Låt `Utredning.py` och `3_Graf.py` importera `find_pdf`/`find_txt`
  från `casebook_ui.py` istället för egna kopior. Ta bort den döda `find_txt`
  i `Utredning.py`.
- [x] Klar

### 16. [Mindre] Docstring överdriver cachens räckvidd
- **Fil:** `src/Utredning.py:275-287`
- **Fix:** Ändra docstring från "cachas per session" till "cachas processvitt
  (delat mellan samtidiga Streamlit-sessioner)".
- [x] Klar
