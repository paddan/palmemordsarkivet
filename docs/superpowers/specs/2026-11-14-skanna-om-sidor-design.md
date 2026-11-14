# Skanna om sidor under en tröskel med valfri motor — design

Datum: 2026-11-14. Status: utkast för godkännande.

## Syfte

Ägaren vill kunna skanna om **alla sidor med låg kvalitetspoäng** i arkivet med en
motor hen väljer — Tesseract, Surya (lines), Surya layout eller en LLM-profil som
läser sidbilden — **oavsett vilken motor som läste sidan tidigare**. Förebilden är
InfoScry, där en läsmetod kan vara en bildmodellprofil och läget "kontrollera och
förbättra" läser om sidor och behåller det bästa.

I dag kan detta inte göras:

- Full OCR:s Surya-redo tar bara sidor under tröskeln **som saknar `pdf_pages`-rad**.
  En sida som någon sidmotor läst en gång hoppas över för alltid (22 697 sidor är
  lästa av Surya lines).
- Motorvalet i redo är bara Surya lines/layout.
- Det finns ingen motor som låter en LLM läsa sidbilden; `llm-correct` rättar bara
  redan OCR:ad text.

Omfattning i dag (tröskel → sidor/dokument, utan bildsidor): 50 → 2 234/512,
60 → 3 714/1 054, 70 → 7 926/2 384.

## Mål

1. En ny operation `rescan-pages` ("Skanna om sidor") i registret, med Admin-kort,
   CLI (`scripts/rescan_pages.py`) och `./rescan_pages.sh`.
2. Urval: sidor i `quality_pages` med `score < tröskel`, inte bildsidor, oavsett
   tidigare motor.
3. Valbar motor: `tesseract`, `vision`, `surya`, `surya-layout`, `llm-vision`
   (samma lista som `rescan-page` plus den nya motorn).
4. **Den bättre läsningen behålls** (ägarbeslut). Den sämre ligger i historiken
   (`pdf_page_readings`) och kan återställas i Sidgranskning.
5. Ny motor `llm-vision` i `ocr_pages.py`: sidbilden skickas till en namngiven LLM-profil
   som skriver av sidan som Markdown.

## Icke-mål

- Ändra Full OCR, `run-pipeline` eller standardmotorn (`lines` är kvar, se
  pilotbeslutet i `docs/surya-layout-pilot.md`).
- Rättning av befintlig text med LLM (det är `llm-correct`).
- Kostnadsbokföring i `llm_usage` för bakgrundsjobb (gäller redan inte för andra
  jobb; räknaren är uttryckligen en undre gräns).

## Anti-mål (misslyckande även om det "fungerar")

- En massomskanning som gör sidor **sämre** utan att det syns eller går att ångra.
- En körning som tar samma sidor om och om igen (en sida som fortfarande ligger
  under tröskeln efter omskanning får inte väljas igen med samma motor).
- Raderade dubbletter (`page_review.is_removed_duplicate`) som får en ny, ofullständig
  textfil — samma fel som just rättades i `rescan-page`.
- Sidbilder som skickas till en extern tjänst utan att operatören ser det.
- En okänd eller bildlös LLM-profil som tyst faller tillbaka på en annan motor.

## Vald ansats: ny operation (A)

**A — ny operation `rescan-pages` (vald).** Egen operation som återanvänder
`rescan-page`s byggstenar (arkivering, återställning, `merge_and_normalize` med
`only_pages`, underprocess `ocr_pages.py --pages … --force`). Full OCR rörs inte.

**B — bygga ut Full OCR:s redo-steg (förkastad).** Inget nytt kort, men redo-steget
är redan tungt (lines/layout, exitkod 3, fallback), Tesseract och LLM passar dåligt
i ett "Surya-redo", historiken finns inte där, och varje pipelinekörning bär på
reglaget.

**C — köra `rescan-page` per sida (förkastad).** Ett jobb åt gången och
embedding-modellen laddas om för varje sida.

## Beteende

### Parametrar (registret)

| Parameter | Typ | Standard | Betydelse |
|---|---|---|---|
| `engine` | choice | `surya-layout` | `tesseract`, `vision`, `surya`, `surya-layout`, `llm-vision` |
| `profile` | str | tom | LLM-profil; krävs när `engine=llm-vision`, ignoreras annars |
| `threshold` | float | 70 | sidor med lägre poäng väljs |
| `source` | choice | `any` | `any`, `files` (palme), `wpu` |
| `max_pages` | int | 0 (alla) | tak — för att prova en motor på några sidor först |
| `dry_run` | bool | false | lista urvalet (antal sidor/dokument, motorfördelning) utan att skanna |
| `reindex` | bool | true | indexera de berörda dokumenten i slutet |
| `jobs` | int | 4 | samtidiga dokument för `llm-vision`; övriga motorer kör ett dokument åt gången |
| `dpi` | int | 300 | renderings-dpi, som `rescan-page` |

`OPERATION_HELP` och `FORM_LAYOUT` i `admin_ui.py` får poster för alla fält;
`profile` blir en rullista över profiler (samma mönster som andra LLM-operationer).

### Urval

1. Rader i `quality_pages` med `score < threshold` och `COALESCE(image_page,0)=0`.
2. Bort: dokument där `is_removed_duplicate` är sant, och dokument vars PDF saknas.
3. Bort: `source`-filtret.
4. Bort: **sidor som redan lästs med vald motor** — den gällande `pdf_pages`-raden
   eller någon rad i `pdf_page_readings` för sidan har samma `engine`. Matchningen
   sker på `engine` ensam, utom för `llm-vision` där även `engine_identity`
   (profil + modell) måste stämma, så att en annan modell får läsa om. En ny
   llama-server-version för `surya-layout` räknas *inte* som ny motor. Det är den
   här regeln, tillsammans med att kvalitetspoängen uppdateras, som hindrar att
   samma sidor tas om varje körning. Befintliga `engine='llm'`-rader kommer från
   `llm-correct` (texträttning) och påverkar inte urvalet för `llm-vision`.
5. Ordning: lägst poäng först, så att `max_pages` tar de sämsta sidorna.
6. Debugloggning: antal per steg och de första exemplen på bortvalda sidor med skäl.

### Per dokument

Sidorna grupperas per dokument. För varje dokument:

1. Arkivera gällande läsning för varje vald sida (`_archive_current`, flyttad till
   en gemensam modul).
2. Kör `ocr_pages.py --in <pdf> --pages <lista> --force --engine <motor>`
   (+ `--profile` för `llm-vision`) via `ctx.run_process` — underprocess, så att
   SIGTERM-hanteraren dödar Surya-workern vid avbrott.
3. **Jämför** per sida: gamla läsningens text (den arkiverade) mot nya
   `pdf_pages.text`, båda normaliserade och poängsatta med **samma**
   `score_text(..., use_hunspell=False)`. Ny poäng ≥ gammal → behåll den nya.
   Ny poäng < gammal → arkivera den nya i historiken och återställ den gamla
   (samma byggsten som `rescan-page --restore-reading`, men den gamla läsningen
   arkiveras inte en gång till — den finns redan från steg 1). Den underkända nya
   läsningen sparas som **en** historikrad med det nya skälet `rejected`
   (schemamigrering av `pdf_page_readings.reason`, version + fixtures), och
   Sidgranskningens historik visar den som "underkänd vid jämförelse". Var den
   gamla läsningen hel-fils-OCR (ingen `pdf_pages`-rad) får sidan ändå en
   `pdf_pages`-rad med den gamla texten och motorn `hel-fils-OCR`, så att Full
   OCR:s Surya-redo inte läser om tusentals underkända sidor nästa körning
   (avvikelse från `--restore-reading`, som tar bort raden). Logga båda poängen per sida
   (debug) och summera per dokument (info: "3 förbättrade, 1 behöll gammal").
4. `merge_and_normalize(stem, only_pages=<de sidor som fick ny läsning>)`.
5. Felhantering: en sida som inte gick att läsa tar bort sin nyss arkiverade
   historikrad och lämnar den gällande läsningen orörd; operationen **fortsätter**
   med nästa sida/dokument (utfallet per sida avgörs av `pdf_pages`-radens
   ögonblicksbild före och efter, så `ocr_pages` behöver ingen ny utdata).
   Operationen avbryts med `OperationFailed` vid rc 3 (Surya saknas), vid
   autentiserings- eller kvotfel från LLM-tjänsten (401/402/403/429 efter
   återförsök) och efter **5 dokument i rad** som misslyckats helt. Jobbet slutar
   med fel om någon sida misslyckades, efter att allt annat körts.

### I slutet

- Kvalitetssteget körs för de berörda dokumenten (`quality.run_quality` med
  `files_from`/motsvarande), även med `--no-reindex`, så att `quality_pages`,
  Sidgranskningens poäng, `--dry-run` och nästa körnings urval stämmer.
- Med `reindex`: `db.delete_ingest` för berörda dokument och ingest med
  `only_stems=<berörda>` (parametern som infördes för `rescan-page`).
- Sammanfattning: sidor valda, skannade, förbättrade, oförändrade (gammal behölls),
  misslyckade; för `llm-vision` även tokens per riktning ur API:ets usage.

### Motorn `llm-vision` i `ocr_pages.py`

Namnet är inte `llm`: det används redan av `llm-correct` för rättad text utan
identitet, och historiken och urvalet måste kunna skilja en bildavläsning från en
texträttning.

- Ny `--engine llm-vision` med `--profile <namn>`. Profilen löses med
  `config.resolve_runtime_profile`; okänd profil eller okänt backend → fel före
  första sidan, aldrig tyst fallback.
- Sidbilden (samma rendering som övriga motorer, PNG, nedskalad om den överstiger
  en storleksgräns) skickas som bild i ett chattmeddelande. Prompten ber om en
  ordagrann avskrift på svenska som Markdown: bevara radbrytningar, rubriker och
  tabeller, gissa aldrig, skriv `[oläsligt]` för det som inte går att läsa och
  ingenting om svarta maskeringsfält (de infogas som `[MASKAD]` av den vanliga
  pixeldetekteringen).
- `[MASKAD]` infogas **i Markdown** av pixeldetekteringen, som eget stycke, på
  samma höjdproportionella sätt som `_merge_redaction_markers` gör för ren text
  (räknat på Markdown-raderna). `pdf_pages.text` härleds sedan ur den Markdownen
  (syntax borttagen), så att text och Markdown har samma markörer och ingest kan
  chunka från Markdown.
- `pdf_pages.markdown` = Markdown efter infogning; `pdf_pages.text` = samma
  innehåll som ren text, eftersom kvalitet, fuzzy-sökning och citat räknar på ren
  text (samma princip som `surya-layout`).
- `engine_identity` = `llm-vision:<profilnamn>:<modell>`.
- Inget PDF-textlager (modellen ger inga radkoordinater) — motsvarar
  `--no-update-pdf` för just denna motor.
- `stop_notice`-motsvarighet: ett svar som avslutats på grund av längd eller fel
  räknas som misslyckad sida, inte som en kort läsning.
- **Bildkontroll före körningen**: en liten testbild med känd text skickas till
  profilen; svarar modellen inte med texten avbryts operationen med ett begripligt
  fel ("profilen X kan inte läsa bilder"). Resultatet cachas inte mellan körningar.
- **Lokal eller extern** visas i Admin-hjälpen och loggas vid start
  ("sidbilder skickas till <host>"), avgjort från profilens endpoint
  (localhost/127.0.0.1 = lokal).
- Backends: OpenAI-kompatibla (OpenAI, OpenRouter, Ollama, custom) via
  `image_url`-innehåll. Claude-profiler stöds inte i första versionen och ger ett
  tydligt fel (Agent SDK-vägen saknar bildstöd för vårt anrop; *byggarens beslut
  om det visar sig enkelt att stödja via Messages API med API-nyckel*).
- Parallellitet: **över dokument**, inte inom — de flesta dokument har bara 1–3
  valda sidor. Operationen kör upp till `jobs` `ocr_pages`-underprocesser samtidigt
  för `llm-vision`; arkivering och jämförelse per dokument sker i operationens
  tråd före respektive efter dokumentets underprocess. Avbrott når alla
  underprocesser (som i dag via `ctx.run_process`). Övriga motorer kör ett dokument
  åt gången (Surya har en worker; Tesseract är snabb nog).
- Nycklar loggas aldrig; inga hela sidtexter i loggen.

## Begränsningar och kända risker

- **Poängen är en heuristik.** Den gynnar text med svenska ord och korta skräpord;
  en Markdown-tabell kan få lägre poäng än samma innehåll som löptext, och en LLM
  som "städar" kan få högre poäng än en ordagrann avskrift. Därför syns båda
  poängen per sida och allt går att återställa. Att hitta en bättre jämförelse är
  ett icke-mål för den här ändringen.
- `max_pages` och `dry_run` är operatörens kostnadsbroms för `llm-vision` mot externa
  profiler (≈8 000 sidor vid tröskel 70).
- Surya layout: ~5 s/sida enligt dagens loggar; 8 000 sidor ≈ 11 h.

## Byggarens val

- Exakt modulindelning för den gemensamma arkiverings/återställningskoden
  (förslag: `src/operations/page_readings.py`, som både `rescan.py` och den nya
  operationen använder).
- Promptens exakta ordalydelse och bildstorleksgränsen för `llm-vision`.
- Markdown → ren text (återanvänd befintlig hjälpare om en sådan finns).

## Dokumentation

`AGENTS.md` (operation, motor `llm-vision`, urvalsregeln mot upprepning, jämförelsen),
`docs/teknisk-referens.md` (nytt avsnitt med flaggtabell, nytt historikskäl `rejected`), `docs/kom-igang.md`
(kort rad om omskanning), samt Admin-hjälpen.
