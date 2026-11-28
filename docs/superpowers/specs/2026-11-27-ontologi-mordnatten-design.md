# Ontologi i Neo4j — dokumentlager och mordnatten

Datum: 2026-11-27. Status: utkast för ägarens godkännande.

## Syfte

Arkivet ska gå att fråga om **struktur**, inte bara söka i som text. Ägaren vill tre saker:

1. **Bättre svar i Utredning.** Modellen ska hitta rätt dokument och rätt uppgifter snabbare och fortfarande citera sidan.
2. **Hitta motsägelser och tidslinjer** som sparade, sökbara uppgifter, inte som något en LLM lyfter fram i stunden (det gör Jämförelse i dag).
3. **Visualisera** dem.

Det första området är **mordnatten** (observationer kring skotten och flykten, 23.00–23.40) med **signalement**. En persons berättelse över tid (samma vittne i flera förhör) kommer senare och ska kunna byggas på samma modell.

## Mål

- Alla dokument och sidor finns som noder i Neo4j med sin metadata, så att grafen är en del av applikationen och inte ett sidospår.
- Händelser, platser, personer, observationer och signalement extraheras ur sidorna som uppgifter med källa (dokument, sida) och granskningsstatus.
- Motsägelser och tidslinjer kan sökas fram med Cypher och visas i en vy för mordnatten.
- Modellen i Utredning får verktyg som läser ontologin och pekar ut källsidor.

## Icke-mål

- Personers berättelser över tid (B) — modellen ska tillåta det, men inget byggs för det nu.
- Titeltolkning av palme-filnamn (datum och personer i titeln), syskonkoder (`T-8517-00, -A`) och wpu-titlar i LanceDB (se planen för filnamn).
- Att ersätta textsökningen. LanceDB förblir textindexet.
- Att Jämförelse-sidan ändras.

## Anti-mål (blir ett misslyckande även om det fungerar tekniskt)

- **Grafen används som belägg.** Ontologin är LLM-extraherad; den pekar ut var något står. Modellen och vyerna ska alltid gå till sidans text före ett citat.
- **Maskade uppgifter avslöjas.** En maskad person får aldrig kopplas ihop med en namngiven person eller med en maskad person i ett annat dokument, varken av extraktionen, inläsningen eller en sökning. Innehållet bakom en maskning gissas aldrig. **Undantag (ägarbeslut 2026-11-27):** observationer i kopior av samma dokument får kopplas med `SAMMA_UPPGIFT` även när vittnet är maskat i den ena kopian och namngivet i den andra, eftersom båda versionerna är publicerade av arkiven. Undantaget gäller bara `SAMMA_UPPGIFT`; en `MaskadUppgift` får fortfarande aldrig `SAMMA_PERSON` eller någon annan identitetskant.
- **Granskade beslut går förlorade** när en sida extraheras eller skannas om.
- **Påhittade motsägelser.** En skillnad i ordval eller en OCR-skada får inte visas som motsägelse; automatiska motsägelser kräver jämförbara värden (se nedan).
- **Kostnaden skenar.** Extraktion av hela arkivet ska inte kunna startas av misstag.

## Begränsningar (projektets regler)

- Ny operation kräver post i `src/operations/registry.py`, `scripts/X.py` och `./X.sh` i samma ändring; Admin och CLI genereras ur registret.
- All SQLite-SQL i `src/db/` med re-export i `src/db/__init__.py`; ny toppnivåmodul i `py-modules`.
- MCP-verktyg finns i både `src/rag/mcp_server.py` (Claude-vägen) och `Utredning.OPENAI_TOOLS` (OpenAI-vägen); samma verktygsmängd bevakas av `tests/test_mcp_server.py`. Verktygssvar som pekar på källor har `source:`-rader i samma format som `search_archive`, så att `citations` och PDF-öppnaren fungerar.
- Loggning med `ctx.log` (jobb) och `web_log.get_logger` (sidor); inga hemligheter eller hela texter i loggar.
- Dokumentationen (`docs/teknisk-referens.md`, `AGENTS.md`, `docs/kom-igang.md` vid nya krav) uppdateras i samma ändring.
- Ingen `git commit`/`git push` av agenter.

## Angreppssätt som övervägdes

1. **Bygga ut kartmodellen** (`map_observations` + kandidatkön). Minst ny kod, men modellen är "en person på en koordinat" (lat/lon obligatoriska) och räcker inte för signalement eller B.
2. **Ny påståendemodell i state.db**, med Karta och Neo4j som vyer. Ett gemensamt underlag i SQLite, men grafsökningarna (tidslinjer, motsägelser, närhet) blir SQL-joins eller en synkad kopia.
3. **Ontologin i Neo4j som en integrerad del av applikationen.** Grafsökningar där de hör hemma, samma databas som Graf-sidan och svarsgrafen redan använder.

**Valt: 3**, ägarens beslut. Delningen nedan är min avvägning inom det valet: SQLite behåller det som är pipeline-tillstånd och billigt att räkna om, Neo4j äger ontologin.

## Beslut

### Neo4j blir obligatoriskt

- `web.sh` startar Neo4j (`neo4j.sh start`) innan Streamlit, och Admin visar status som i dag.
- Sidor och verktyg som läser ontologin ska klara att Neo4j inte svarar: de visar/returnerar ett tydligt meddelande ("kunskapsgrafen svarar inte — starta den i Admin"), och den vanliga sökningen fungerar som förut.
- `docs/kom-igang.md` får podman/Neo4j som krav i stället för valfritt.
- **Säkerhetskopiering:** eftersom granskningsstatus bara finns i Neo4j får delprojekt 1 operationerna `neo4j-backup` och `neo4j-restore`. Kopian är en **export via Cypher** av det som inte går att bygga om ur SQLite (status, rättade värden, `KAN_VARA`/`SAMMA_PERSON`, koordinater angivna i granskningen) till `generated/neo4j-backup/granskning-<tidsstämpel>.json` — inte `neo4j-admin dump`, som i gratisutgåvan kräver att databasen stoppas. Allt annat byggs om ur SQLite och filerna. Admin visar när senaste kopian togs. *Min avvägning.*
- **Tester:** rena funktioner (tolkning, radomvandling, motsägelselogik på Python-nivå där den finns) testas utan Neo4j. Cypher-frågor testas mot en riktig Neo4j: i CI som en tjänstecontainer (`neo4j:5`) i `.github/workflows/ci.yml`, lokalt markerade `@pytest.mark.neo4j` och bortvalda när ingen databas nås. *Min avvägning.*

### Lagring

| Var | Vad |
|---|---|
| SQLite (`state.db`) | pipeline-tillstånd; `document_meta` (filnamnsplanen); LLM:ens **råa** extraktionssvar per sida med sidans texthash, modell och profil (så att en sida aldrig betalas två gånger) |
| Neo4j | ontologin: dokumentlagret, mordnattslagret, granskningsstatus. **Auktoritativ** för status och granskning. |
| LanceDB | textindexet, oförändrat |

Kopplingen mellan Neo4j och LanceDB är `(:Sida {stem, sida})` ↔ LanceDB:s `source` (`<stem>.txt`) + `page`.

### Dokumentlagret

```
(:Dokument {stem, arkiv, nr, titel, bestallt, upplagt, anmarkning,
            antal_sidor, dokumentdatum, avkortat})
  -[:HAR_SIDA]->      (:Sida {stem, sida})
  -[:INGÅR_I]->       (:Uppslag {kod})          kod = kanonisk nyckel, t.ex. "D4991-00"
  -[:HÄNVISAR_TILL]-> (:Dokument)               ur "se 531.1" (palme-nr)
  -[:DEL_AV]->        (:Dokument)               1295.24 → 1295 (närmaste existerande förälder)
```

- Källa: `document_meta` (byggs av filnamnsplanen) och textfilernas sidantal (`\f`-delning, som ingest).
- Alla ~10 000 dokument och ~47 000 sidor läses in. Inläsningen är idempotent (`MERGE` på `stem` respektive `(stem, sida)`), skriver om egenskaperna när metadatan ändrats och tar bort dokument och sidor som inte längre finns.
- Körs som egen operation (`load-documents`) och automatiskt sist i `run-pipeline` efter ingest. *Min avvägning: eget steg i stället för inuti ingest, så att ingest inte kräver Neo4j.*
- `HÄNVISAR_TILL` och `DEL_AV` skapas bara när målets `nr` finns. Ett nr som förekommer flera gånger (t.ex. en kopia) kopplas till alla med det numret.
- **Ingen `SAMMA_SOM`-kant.** `merge_wpu` sparar inte vilket par som jämfördes (bara `wpu_decisions.pdf_stem`), och den sämre dubbletten saknar text. Palme- och wpu-dokument med samma uppslag möts i stället via `(:Uppslag)`.
- Den befintliga kunskapsgrafen (`NÄMNER`/`RELATERAR`, granskning och `graph_sync`) finns kvar som ett lager av entitetsomnämnanden på samma `Dokument`-noder. Dokumentinläsningen rör aldrig kanter med `graph_owner`, och `graph_sync` rör aldrig dokumentlagrets egenskaper eller kanter. `_MENTION_CYPHER`s `ON CREATE SET d.nr, d.titel` behålls (dokumentlagret skriver över med samma värden).

### Mordnattslagret

```
(:Observation {id, tid_från, tid_till, tid_text, osäkerhet, utdrag, status,
               granskad_at, extraktion_hash, schemaversion})
  -[:KÄLLA]->        (:Sida)
  -[:LÄMNAD_AV]->    (:Vittne | :MaskadUppgift)     vittnet, alltid per dokument
  -[:AVSER]->        (:Händelse {id, namn, ordning, tid_från, tid_till})   ankarhändelse, valfri
  -[:PLATS]->        (:Kartplats {id, namn, lat, lon})
  -[:OBSERVERADE]->  (:ObserveradPerson {id, beskrivning})   en per observation
(:ObserveradPerson)-[:SIGNALEMENT]->(:Egenskap {typ, värde_text, min, max, enhet})
(:ObserveradPerson)-[:KAN_VARA {status}]->(:Nyckelperson {id, namn})
(:Vittne {id, stem, namn, namn_norm})-[:SAMMA_PERSON {status}]->(:Vittne)
(:Observation)-[:SAMMA_UPPGIFT]->(:Observation)
(:MaskadUppgift {stem, lokalt_id, kategori, roll})
```

- **Egna etiketter, skilda från kunskapsgrafen.** Ontologin använder `Vittne`, `Kartplats` och `Nyckelperson` i stället för kunskapsgrafens `Person {norm}` och `Plats {norm}`. Skälet: `graph_sync` (`review_service.replace_graph`) tar bort fristående noder med sina etiketter, och kunskapsgrafen slår ihop personer på normaliserat namn. Ontologins etiketter ingår aldrig i `graph_sync`s hanterade etiketter. *Min avvägning.*
- **Vittnen är per dokument.** Ett namngivet vittne blir en `Vittne`-nod per dokument (`id` = stam + normaliserat namn). Att två vittnesnoder är samma person är ett påstående: inläsningen föreslår `SAMMA_PERSON` (status `ogranskad`) när `namn_norm` är lika, och granskningen godkänner eller avvisar. Filtret "vittne" grupperar på godkända `SAMMA_PERSON` plus lika `namn_norm`. `MaskadUppgift` får aldrig `SAMMA_PERSON`. Det här är också grunden för B senare. *Min avvägning.*
- **`KAN_VARA` föreslås av extraktionen bara när texten själv pekar ut personen** ("gärningsmannen", "mannen som sköt", ett namn på listan) och bara mot `Nyckelperson`-noder i `data/ontologi/nyckelpersoner.json` (Gärningsmannen, Olof Palme, Lisbet Palme med flera; ägaren redigerar). Andra mål ignoreras med en debugrad. Granskningen kan lägga till, godkänna och avvisa. *Min avvägning:* signalementsmotsägelser syns direkt men bara mot en kuraterad lista, så att LLM:en inte hittar på identiteter.
- **Kopior.** Observationer från olika dokument med i stort sett samma utdrag (normaliserad text, likhet ≥ 0,9) och samma händelse kopplas med `SAMMA_UPPGIFT` och paras aldrig som motsägelse; de visas som en uppgift med två källor. *Min avvägning.* Kopplingen görs även när maskningen skiljer mellan kopiorna, och då visas båda källorna sida vid sida (ägarbeslut, se anti-målen).

- **Tid** lagras som ett fönster (`tid_från`/`tid_till`, `HH:MM`, basdatum 1986-02-28 som i Karta) plus originaltexten (`tid_text`: "strax efter 23.20"). Saknas tid är fönstret tomt och observationen syns i listor men inte på tidslinjen.
- **Plats** (`Kartplats`) matchas mot `data/karta/platser.json` (exakt/fuzzy/ingen, som kartans extraktor i dag). En okänd plats får inga koordinater förrän någon anger dem i granskningen; den syns i tidslinjen men inte på kartan.
- **Ankarhändelser** ligger i `data/ontologi/ankarhandelser.json` (10–15 händelser med namn, ordning och ungefärligt tidsfönster). Jag skriver ett utkast; ägaren rättar det. Listan är redigerbar data, inte kod.
- **Identitet är ett påstående.** Att en observerad person är t.ex. "Gärningsmannen" är en `KAN_VARA`-kant med status, aldrig en sammanslagning av noder.
- **Maskning.** `[MASKAD]` i vittnesrollen blir `MaskadUppgift` med `lokalt_id` inom dokumentet, kategori (namn, adress, telefon, personnummer, okänd) och roll när blanketten eller repliken visar den. Den får inga kanter till noder utanför sitt dokument; inläsningen kontrollerar det och avvisar annars raden med en loggrad. Namn som liknar platshållare (`[MASKAD]`, `NN`, `okänd`) blir aldrig `Person`-noder.

### Granskningsstatus

- Status: `ogranskad` (standard), `godkänd`, `avvisad`, `rättad` (godkänd med ändrade värden; originalvärdena sparas på noden), `inaktuell`.
- **Ogranskade uppgifter räknas** i tidslinjen, motsägelserna och Utredning, men är tydligt märkta. Alla vyer och verktyg har ett filter **bara granskade** (`godkänd` + `rättad`). Ägarens beslut.
- **Omextraktion:** `extraktion_hash` är hash av sidans normaliserade text; schemaversionen sparas separat. Ny extraktion av en sida med ändrad text: ogranskade observationer från sidan tas bort och ersätts; granskade blir `inaktuell`, och de nya observationerna läggs bredvid som `ogranskad`. Status sätts aldrig tillbaka till `ogranskad` för en granskad observation.
- **`inaktuell` räknas inte** i tidslinjen, motsägelserna eller filtret. Den syns i en egen granskningslista "Källtexten har ändrats" där man kan avvisa den gamla eller godkänna den nya. *Min avvägning.*
- **Ny schemaversion** gör inga granskade observationer inaktuella. Den extraherar om sidor vars observationer alla är ogranskade; sidor med granskade observationer tas bara om med `--force`. *Min avvägning.*

### Motsägelser

Automatiska motsägelser kräver jämförbara värden. *Min avvägning:*

| Slag | Villkor |
|---|---|
| Tid | två observationer `AVSER` samma händelse och deras tidsfönster överlappar inte (med en tolerans i minuter, standard 2) |
| Plats | två observationer `AVSER` samma händelse och platserna ligger längre isär än en gräns i meter (standard 150) — kräver koordinater |
| Signalement | två `ObserveradPerson` `KAN_VARA` samma `Nyckelperson` (status inte `avvisad`), samma `Egenskap.typ`, och numeriska intervall (`min`–`max`) som inte överlappar — längd och ålder |

Kategoriska egenskaper (klädsel, färg, hår) visas som **spridning** sida vid sida, inte som motsägelse. Avvisade och inaktuella observationer räknas aldrig, och observationer kopplade med `SAMMA_UPPGIFT` paras inte med varandra.

### Extraktion

- Schema för LLM:ens svar i `data/ontologi/schema.json` (JSON Schema), versionerat; versionen ingår i `extraktion_hash`.
- Operation `extract-mordnatten` (namn får justeras): LLM-profil (`--profile`, som `graph-review-llm`), urval, `--dry-run` (listar sidor och uppskattar kostnad ur profilens priser), `--max-pages`, `--jobs` (4, nätverksbunden). Admin kräver bekräftelse före start, som `rescan-pages`.
- **Urval** av sidor: (1) dokument vars `document_meta.uppslag` hör till en lista med uppslagsserier för mordplatsens vittnen i `data/ontologi/urval.json`, (2) sidor som träffas av sökfrågor i samma fil (hybridsökning i LanceDB), (3) en lista med uttryckliga stammar/sidor. Ägaren redigerar filen.
- **Piloten (ägarbeslut 2026-11-27):** uppslagsserierna `EAE`, `EAD`, `EAC`, `EAA`, `EA`, `EAF` och `EAB` (vittnen kring mordplatsen; ~1 080 dokument enligt filnamnen) står först i `urval.json`. Första körningen tar **100 sidor** och föregås av `--dry-run` som visar urval och uppskattad kostnad; `--max-pages` har standardvärdet 100 tills ägaren ändrar det. Utfallet granskas manuellt innan urvalet vidgas.
- Råsvaret sparas per sida i SQLite (ny tabell, schemaversion följer `_schema.py`). En sida med oförändrad texthash och schemaversion extraheras inte igen.
- Inläsning till Neo4j är ett eget steg i operationen, så att inläsning kan köras om utan nya LLM-anrop.

### Utredning

- Tre nya verktyg i båda vägarna:
  - `timeline(handelse | person | plats, fran, till, bara_granskade)` — observationer sorterade på tid.
  - `find_contradictions(handelse | person, slag, bara_granskade)` — par med skäl och båda källorna.
  - `document_context(source)` — uppslag, hänvisningar och delar för ett dokument.
- Svaren har `source:`-rader i samma format som `search_archive`, status per uppgift och en rad om att grafen inte är belägg. Promptens verktygsreferens (`prompts.MCP_SYSTEM_PROMPT` och skrivhjälpen) uppdateras: läs sidan med `get_page` före citat.
- Claude-vägen når Neo4j från subprocessen via miljövariabler (`NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD`, samma som Graf-sidan) — lösenordet loggas aldrig.
- **Sökinställningar** (båda lägena) får filter på ankarhändelse och uppslag: grafen ger stammar, LanceDB söker bland dem (samma väg som facetterna: `facets.sources_where_clause`).
- **Svarsgrafen** visar de citerade dokumenten med deras observationer och uppslag.

### Vyn Mordnatten (ersätter Karta)

- Tidslinje 23.00–23.40 per ankarhändelse/vittne (granskad ● / ogranskad ○), en karta som följer tidslinjen (befintlig TimestampedGeoJson) och en motsägelselista med källor och PDF-knappar (`casebook_ui.render_pdf_opener`).
- Filter: bara granskade, händelse, vittne, person.
- Granskning på plats: godkänn, avvisa, rätta (tid, plats, värden, `KAN_VARA`), ange koordinater för okänd plats. Skrivningen till Neo4j sker via en tunn modul, inte i sidan.
- `map_observations`, `map_observation_candidates` och `map_observation_extractions` är tomma i dag. De tas bort i en schemamigration när vyn ersätter Karta, och `extract_map_observations` ersätts av extraktionen ovan (operation, script och genväg tas bort i samma ändring). `data/karta/platser.json` blir platsregistret; `data/karta/rorelser.json` (tom) tas bort.
- Graf-sidan får de nya nodtyperna (Dokument med metadata, Uppslag, Observation, Händelse); klick öppnar PDF:en på rätt sida.

## Delprojekt och ordning

Varje delprojekt får en egen implementationsplan och ska fungera för sig.

1. **Neo4j som fast del + dokumentlagret.** `web.sh`, statuskontroll, `neo4j-backup`/`neo4j-restore`, CI-tjänst och testmarkering, `load-documents`, Graf-sidan visar dokumentnoder med metadata. Kräver att filnamnsplanen (`docs/superpowers/plans/2026-11-27-dokumentmeta-ur-filnamn.md`) är klar.
2. **Ontologischema och extraktion för mordnatten.** `data/ontologi/` (schema, ankarhändelser, urval), råtabellen i SQLite, `extract-mordnatten` med inläsning, maskningsregler, omextraktion.
3. **Vyn Mordnatten.** Tidslinje, karta, motsägelser, granskning, borttagning av de tomma karttabellerna och kartextraktorn, nya nodtyper i Graf.
4. **Utredning.** De tre verktygen i båda vägarna, prompt och skrivhjälp, filter i sökinställningarna, svarsgrafen.

Ett första synligt resultat efter delprojekt 2: en tidslinje i Graf-sidan eller en enkel tabell, innan vyn i 3 är klar.

## Lämnat till byggaren

- Exakt Cypher, index och constraints (utöver unikheten på `Dokument.stem`, `Sida(stem, sida)`, `Uppslag.kod`, `Händelse.id`, `Observation.id`).
- Exakta fält i `schema.json` utöver modellen ovan, och hur `Observation.id` bildas (stabilt per sida och post).
- Komponent för tidslinjen (Plotly eller liknande som redan finns i beroendena går före ett nytt beroende).
- Hur Neo4j-anslutningen delas mellan sidor (en cachad drivrutin per process).

## Risker

- **Extraktionskvalitet.** Svagare modeller tolkar tid och plats fel; därför granskningsstatus, utdrag och filtret. Piloten i delprojekt 2 ska köras på ett litet urval först och jämföras manuellt.
- **Neo4j-data utan kopia.** Mildras med `neo4j-backup` och att råsvaren finns kvar i SQLite (allt utom granskningen kan byggas om).
- **Tvetydiga ankarhändelser.** En observation kan passa flera; `AVSER` är valfri och granskningen kan ändra den.
