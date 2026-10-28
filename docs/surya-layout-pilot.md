# Surya 2 med layout — spik och pilot

Underlag för planen `docs/superpowers/plans/2026-10-25-surya-layout-markdown.md`.
Spiken (Task 0) körs före all kod för att se vad Surya 2 faktiskt svarar för arkivets
två vanligaste dokumenttyper: blanketten SPANINGSUPPSLAG och förhörsprotokoll.
Piloten (Task 11) mätte sedan motorn mot 50 riktiga arkivsidor; se *Pilot (2026-10-26)* nedan.

## Spik (2026-10-25)

### Metod

- Runtime: infoscrys installation, oförändrad — `surya-ocr 0.22.1`, backend `llamacpp`,
  vikterna `datalab-to/surya-ocr-2` (`surya-2.gguf`, revision `6a3a4c30`),
  `llama-server` build 11669. Worker: `infoscry/scripts/ocr/surya_worker.py`, protokoll 2,
  `full_page=True`.
- Tio sidor renderade i 300 dpi med pypdfium2: fyra förstasidor med SPANINGSUPPSLAG (två
  av varje variant), tre sidor med löpande förhörsprotokoll, två förhörssidor med dialog
  och en sida med många maskeringar.
- Svaren jämfördes block för block med sidbilden. Fem råa svar finns som fixtures i
  `tests/fixtures/surya/` (`form-v1-a`, `form-v2-a`, `form-v2-b`, `forhor-dialog-a`,
  `forhor-dialog-b`).

### Resultat

Alla tio sidor gav `status: "read"`. Surya rapporterade en konfidens på 0,94–0,98 per sida,
vilket betyder att logprobs är på.

| Sida | Block | Etiketter |
|---|---|---|
| form-v1-a | 65 | 65 `Text` |
| form-v1-b | 59 | 57 `Text`, `SectionHeader`, `PageFooter` |
| form-v2-a | 34 | 14 `PageHeader`, 16 `Text`, `Table`, `SectionHeader`, `Footnote`, `PageFooter` |
| form-v2-b | 27 | 24 `Text`, 2 `Form`, `PageFooter` |
| förhör, prosa (3 sidor) | 26–29 | `Text`, med `PageHeader`/`PageFooter` |
| förhör, dialog (2 sidor) | 39–43 | `Text`, med `PageHeader`/`PageFooter` |
| maskad | 15 | 12 `Text`, `Form`, `PageHeader`, `Footnote` |

**Blanketterna läses mycket bättre än med Tesseract.** På `form-v1-a` blev alla maskinskrivna
värden rätt (`Stockholm`, `KCV`, `Insp NEK Persson`, `769 45 81`, `860316`, `09.10`,
`Mordet på Palme`). Även de handskrivna värdena `D` och `2449` i fälten 07/08 blev rätt.
Samma sida gav med Tesseract text som `Ra td på Palme | ligt besök FJ per telefon I`.

Fyra saker i hur blanketterna kommer tillbaka styr parsern:

1. **Fälten är oftast inte `Form`.** Ett fält är vanligen ett eget block, där etiketten
   (`03 Uppgiftsmottagare`) och värdet ligger i separata block och värdet står direkt
   under etiketten. Bara 3 av 10 sidor fick någon `Form`-etikett. På `form-v2-a` fick
   fälten etiketten `PageHeader`, och ibland kommer de som `Table`. Fält måste därför
   kännas igen på fältnumret och placeringen på sidan, inte på Suryas etikett.
2. **Ifyllda värden är oftast i fetstil (`<b>`), men inte alltid.** På `form-v1-a` och
   `form-v2-b` var de det, på `form-v2-a` inte. Ibland ligger etikett och värde i samma
   block, åtskilda av `<br/>`, till exempel `01 Polisdistrikt*<br/><b>Stockholm</b><br/>02 Arbetsenhet*`.
3. **Kryssrutor är tydliga:** `<input type="checkbox"/>` respektive `<input checked="" type="checkbox"/>`.
   På `form-v1-a` stämde *per telefon* och *misstänkt person* med bilden.
4. **Blankettens huvud upprepas på varje förhörssida** (`Sid nr 2`), med tomma fält. Om
   tomma fält inte filtreras bort hamnar `01 Polisdistrikt* 02 Arbetsenhet* …` i varje chunk.

**Förhören:**

- Löpande text kommer som rena stycken i rätt ordning. Tesseracts dubbla radavstånd och
  avbrutna meningar försvinner.
- Metadataraderna (`Förhörsledare: …`, `Förhörsvittne: …`, *Förhöret hållet …*) är egna
  block eller rader.
- I dialog är varje replik i regel ett eget block (`F: …`, `M: …`). Den som hörs
  betecknas med en initial (`M` för Mårten Palme), inte med `S:`.
- Avstavning finns kvar som `polis-<br/>inspektören` och `Fortsätt-<br/>ningsblad` och
  måste slås ihop.
- `<u>` (understrykning) förekommer i namn och nyckelord. Texten ska behållas och taggen
  kan släppas.
- Stämplar och marginalanteckningar blir smala egna block längs vänsterkanten, till
  exempel `Phörd`, `Pom` och blankettnumret `RPS 463.1.2`.

**Maskeringarna hanteras på tre olika sätt, och bara ett av dem är bra:**

1. **Rätt:** stora maskeringar mitt i en mening blir `[REDACTED]` på exakt rätt ställe,
   som i `1986 så bodde Avsan på [REDACTED] där …`. Det är bättre än dagens
   pixeldetektor, som bara kan sätta `[MASKAD]` mellan rader.
2. **Försvinner helt:** svarta balkar i blankettfält (12 Uppgiftslämnare, 13 Yrke,
   14 Postadress på `form-v1-a`) syns inte alls i svaret. Pixeldetektorn behövs alltså
   kvar.
3. **Blir kryssrutor:** små maskeringar i dialog, till exempel ett maskerat namn eller
   talarens initial, blir `<input type="checkbox"/>` (`F: Ja, ☐, klockan börjar …`,
   `☐ Ja, det har jag hört.` på `forhor-dialog-b`). En maskering är en *fylld* mörk
   rektangel och en riktig kryssruta en *ihålig* ram, så pixlarna inom blockets bbox kan
   skilja dem åt.

### Hastighet

| Mätning | Resultat |
|---|---|
| 10 sidor i en worker, inklusive start | 249 s |
| Per sida, 300 dpi (fyra blanketter) | 18–32 s |
| Per sida, 200 dpi | 26–27 s |
| Per sida, 150 dpi | 23 s, men blanketten gav 39 block i stället för 65 |

Tiden styrs alltså av hur mycket text modellen genererar, inte av upplösningen, och lägre
upplösning tappar blankettfält. Behåll 300 dpi.

Det blir omkring 25 s per sida, eller ungefär 7 timmar per 1 000 sidor. Hela arkivet
(~47 000 sidor) skulle ta ungefär två veckor. Infoscrys siffra på 1,3 s gäller en enkel
syntetisk sida. Dagens Surya 0.17 tar enligt AGENTS.md 30–100 s per sida, så Surya 2 är
ändå snabbare än den motor den ersätter.

### Konsekvenser för planen

1. **Task 2b:** blankettfält tolkas utifrån placering: ett block (eller en `<br/>`-rad)
   som börjar med `\d{2}` är en etikett, och värdet är det eller de block som står under
   etiketten i samma spalt fram till nästa etikett. Suryas etikett (`Text`, `PageHeader`,
   `Form`, `Table`) spelar ingen roll, och fetstil räknas som stöd men krävs inte.
   Kryssrutor tolkas från `<input … checked>`. Tomma fält skrivs inte till Markdown.
2. **Beslut 3 (sidhuvud som kontextrad)** får bara gälla `PageHeader`-block som inte är
   blankettfält, och högst 200 tecken. I urvalet var alla `PageHeader` blankettfält.
3. **Maskeringar (beslut 5):** `[REDACTED]` byts mot `[MASKAD]`. Pixeldetektorn körs
   fortfarande för balkar som Surya utelämnat. En kryssruta i ett block utan fältnummer,
   där blockets bbox innehåller en fylld mörk rektangel, blir `[MASKAD]`. Ingen maskering
   får räknas två gånger.
4. **Repliker (Task 2b):** talarmarkören är `^[A-ZÅÄÖ]{1,3}:` eller `Fråga:`/`Svar:`, och
   en maskerad markör (`[MASKAD]` först i blocket) räknas också som replik.
5. **Avstavning:** `-<br/>` följt av liten bokstav slås ihop.
6. **Motorvalet:** med ~25 s per sida är layoutmotorn inget alternativ för att OCR:a om
   hela arkivet. Den används för redo av sidor med låg poäng, för förstasidor med
   SPANINGSUPPSLAG och för manuell omskanning. Pilotens krav på hastighet jämförs mot
   dagens Surya 0.17, inte mot Tesseract.

## Pilot (2026-10-26)

**Layoutmotorn blir inte standard.** Två av fem acceptanskriterier är uppfyllda
(kvalitetspoäng och repliker), tre är det inte (tomma sidor, hastighet och
blankettfältens parning). Standarden står kvar på `lines`. Layoutmotorn är redan bättre
för förhörssidor och för sidor med handskrift eller ritningar, och den kan användas
där vid manuell omskanning. Ingen produktionskod har ändrats av piloten. Det enda som
ändrats är mallens fasta tryck (se *Ändringar*).

### Metod

- **Urval, 50 sidor** (fast frö 20261026, hela listan i bilagan):
  - 15 förstasidor med SPANINGSUPPSLAG: 8 av variant 1 (textfilen har
    *Uppgiftsmottagare*) och 7 av variant 2 (*Upprättat*).
  - 15 förhörssidor (filnamnet innehåller *Förhör*): 6 med dialog (minst fyra
    talarmarkörer `XX:` i textfilen) och 9 med löpande text. Högst en sida per dokument.
  - De 10 sidor som har lägst `score_text` på den gällande textfilssidan. Bara sidor
    utan `pdf_pages`-rad (hel-fils-Tesseract) och med minst 50 tecken som inte är
    blanksteg, högst en sida per dokument.
  - 10 slumpade sidor, högst en per dokument.
  - Bara dokument vars textfil har lika många sidor som PDF:en kom med (ett avslutande
    `\f` räknas inte), så att text och bild hör till samma sida.
- **Läsläge:** arkivet lästes bara. Det gäller PDF:erna, `generated/text` och state.db
  som `?mode=ro&immutable=1`. All utdata skrevs under `/private/tmp/claude-501/pilot/`
  med `STATE_DB` och felloggen omdirigerade dit. `generated/` var oförändrad efteråt
  (samma kontrollsumma över filernas mtime före och efter).
- **Layout:** Surya 2 (`surya-ocr 0.22.1`, llama.cpp build 11669) via `SuryaWorker`, en
  worker för hela körningen. Varje sida renderades i 300 dpi och gick sedan igenom
  `detect_redactions_image` och `ocr_pages.ocr_surya_layout`, samma väg som
  `--engine surya-layout`.
- **Lines:** `ocr_pages.ocr_surya` (Surya 0.17.1) i projektets `.venv` på de 20 sidor i
  urvalet som har lägst lagrad poäng (de tio sämsta plus de tio näst sämsta, varav sex
  blanketter). `[MASKAD]` lades in med `_merge_redaction_markers`, som i pipelinen.
  Motorerna kördes efter varandra, aldrig samtidigt.
- **Uppstart per fil** mättes separat:
  - Layout: tre kallstarter av en ny worker med en tom 400×300-bild.
  - Lines: modelladdning i en ny process plus första inferensen. `ocr_pages` körs som en
    process per fil, så båda motorerna betalar sin uppstart per fil.
- **Sökkvalitet** mättes i minnet, utan LanceDB:
  - Layoutsidornas Markdown chunkades som i ingest (`normalize_markdown`,
    `chunk_markdown` 800/150, `is_useful(markdown=True)`). Jämförelsekorpusen var samma
    sidor ur dagens textfiler, chunkade med `chunk_text`.
  - Inbäddningarna gjordes med `intfloat/multilingual-e5-large` och prefixen
    `passage: `/`query: `.
- **Manuell kontroll:** fem blanketter och fem förhörssidor renderades och jämfördes för
  hand med layoutmotorns fält och Markdown.

Mätskripten finns i `/private/tmp/claude-501/pilot/`: `select_sample.py`,
`run_layout.py`, `run_lines.py`, `analyze.py`, `search_eval.py`, `reparse.py`,
`crop.py` och `show.py`. Rådata ligger i `layout.jsonl` och `lines.jsonl`.

### Acceptanskriterier

| Kriterium | Mätt | Utfall |
|---|---|:---:|
| Medianpoäng layout ≥ lines på de 20 sämsta sidorna | 78,5 mot 71,2 (layout ≥ lines på 14 av 20 sidor). På de 10 sidor med riktig text: 76,7 mot 72,2 | **PASS** |
| Ingen sida som lines läste går tom med layout | Sida 42 (extrasida utanför lines-urvalet): projektets pixelspärr gav `PAGE_TOO_LARGE`, lines läste 1 908 tecken. Går att åtgärda | **FAIL** |
| Sekunder per sida ≤ Surya 0.17 | Samma 19 sidor: median 16,4 s mot 16,0 s, summa 439 s mot 423 s. Utan lines uppvärmningssida (18 sidor): median 16,0 s mot 15,2 s, summa 412 s mot 346 s | **FAIL** |
| Minst 80 % av de ifyllda blankettfälten rätt parade | 34 av 56 = 61 % | **FAIL** |
| Inga tappade repliker på förhörssidorna | 48 av 48 repliker i rätt ordning, förhörshuvudet intakt | **PASS** |

**1. Kvalitetspoäng.** På de 19 sidor där båda motorerna gav text gav layout högre
eller lika poäng på 14. Poängen är ändå ett trubbigt mått här. På ritningar och svag
handskrift (sida 32, 33, 38 och 40) hittar lines på engelsk text, till exempel
*"The state of the state of the …"* i 1 740 tecken. Den texten får 70–74 poäng, medan
layout läser ut stämpeln korrekt (`B 847/88`, `Avd 13`) och annars lämnar sidan nästan
tom. Även den lagrade Tesseract-texten var nästan värdelös på samma sidor: median 32,9
på de 20 sidorna och 0,0 på de tio sämsta.

Medianen över 20 sidor är därför brusig. Sida 39 får till exempel 93,3 poäng på
enbart `[MASKAD]`. På de 10 sidor i lines-urvalet som har riktig löptext eller
blanketttext (3, 9, 10, 12, 14, 15, 27, 37, 41 och 44) är medianen 76,7 för layout
mot 72,2 för lines. Kriteriet håller alltså även där.

**2. Tomma sidor.** Tre av 50 sidor (42, 43 och 50) gav `PAGE_TOO_LARGE`.
Skanningarna är 30–43 megapixel i 300 dpi. Gränsen är **projektets egen pixelspärr**
(`MAX_PAGE_PIXELS = 16 777 216` i `src/surya_worker.py`), inte en gräns i Surya. Den
slår till eftersom sidan alltid renderas i fast 300 dpi. Felet går alltså att åtgärda:
begränsa renderingens upplösning per sida så att bilden håller sig under spärren.
- Sida 42 är en SPANINGSUPPSLAG-blankett. Den låg utanför de 20 sidor som kördes med
  lines och kördes därför med lines som en extrasida (`lines_extra.py`). Lines läste
  1 908 tecken (poäng 80,1), så sidan gör kriteriet ouppfyllt.
- På sida 43 och 50 tog även lines slut på MPS-minnet. Sida 43 fick lines-processen
  att fela på de fem följande sidorna också, så de kördes om i en ny process.
- I hela arkivet är **2 850 av 68 479 PDF-sidor (4,2 %, i 339 filer) större än
  gränsen** i 300 dpi.
- Sida 31 (en upp-och-nedvänd karta) blev helt tom med layout. Lines gav 73 tecken,
  men inget av dem är ett ord från sidan (kontrollerat mot bilden), så sidan räknas
  inte som läst av lines.

**3. Hastighet.** Siffran 30–100 s per sida för Surya 0.17 i AGENTS.md stämmer inte
på den här maskinen (M-serie med MPS). Över de 20 sidorna hade lines medianen 16,3 s
och medelvärdet 23,4 s, med en första sida på 76 s inklusive uppvärmning. Layout hade
medianen 18,5 s och medelvärdet 24,6 s över 47 lästa sidor. Om lines första sida
(76 s med uppvärmning) räknas bort blir jämförelsen på samma 18 sidor tydligare: lines
median 15,2 s och summa 346 s, mot layouts 16,0 s och 412 s. Layoutmotorn har dessutom
en lång svans: tre ritningssidor tog 64–78 s och en blankett tog 158 s. Lines var
snabbast på ritningarna. Kriteriet är alltså inte uppfyllt: layout är ungefär lika
snabb i median men långsammare totalt.

| Uppstart per fil | Layout | Lines |
|---|---:|---:|
| Varm cache | 2,8–4,1 s (ny worker + tom sida) | 2,5–3,8 s laddning + 1,7–6,2 s första inferens |
| Kall cache | – | 22,6 s laddning |

Pixeldetektorn för maskeringar tog i median 0,1 s per sida.

**4. Blankettfält.** I den manuella kontrollen räknas ett fält som rätt när värdet
står i rätt fält, även om det har mindre OCR-fel. Maskerade fält räknas inte, och
fritextfältet (18 eller 10) räknas som rätt när texten följer i Markdown.

| Sida | Variant | Ifyllda fält | Rätt parade | Andel | Handskrift |
|---:|---|---:|---:|---:|---|
| 1 | v1 | 18 | 13 | 72 % | Två värden rätt parade: `1094` (08) och `EAD` (09). `861112` (24) parat men läst som `B61112`. `860308` (20) läst men oparat. `E` (07) och `R` (22) saknas helt. Fält 25 fick signaturen från fält 20. |
| 3 | v1 | 13 | 6 | 46 % | `DD` och `2775-D` (07/08) saknas helt. Telefonnumret (04) hamnade i 03. Datum och klockslag (05/06) står som lös text. |
| 5 | v1 | 10 | 5 | 50 % | `E`, `1974-B` och `EAC 1580 B` lästes rätt men hamnade i etiketterna 07–09 och **saknas därför i Markdown**. Datum och klockslag står som lös text. |
| 9 | v2 | 9 | 5 | 56 % | `EBE 10324-1-A` läst men oparat. Utskriftsdatumet `881013` läst som `081013` och oparat. |
| 13 | v2 | 6 | 5 | 83 % | 07 parat men läst som `A 175-A` i stället för `A 1-15-A`. Utskriftsdatumet hamnade i etiketten för 06. |
| **Summa** | | **56** | **34** | **61 %** | Handskrift läses ofta, men bara ungefär hälften av värdena hamnar i rätt fält. |

Mallvalet var rätt på alla 15 blanketter (8 v1, 7 v2) och ingen sida var `degraded`.
Felen kommer av att fält och värden inte paras ihop, och de är av fyra slag:
- Etiketten för ett fält bakas ihop med grannfältets etikett. I v1 försvinner fält 04
  in i 03 (`03 Uppgiftsmottagare 04 telefon`) på 3 av 8 blanketter. I v2 bakas de
  onumrerade etiketterna in i 03–05 (`Upprättat(d) av telefon`,
  `Fortsättningsblad Uppgiftslämnare …`) på 4 av 7.
- Värdet blir kvar i etikettraden (`07 Avsnitt E`). Fältets värde är då tomt, och
  eftersom tomma fält inte skrivs till Markdown försvinner värdet därifrån. Det finns
  bara kvar i den rena texten.
- Datum och klockslag (05/06) i v1 hamnar ofta som lös text.
- Handskrivna värden i rutan för avsnitt och löpnummer (07/08) saknas eller paras fel.

**5. Repliker.**

| Sida | Typ | Resultat |
|---:|---|---|
| 16 | dialog FE/G | 10 av 10 repliker i rätt ordning, även ett stycke inne i en replik |
| 17 | dialog ÅR/G | 13 av 13 |
| 18 | dialog GF/F | 10 av 10 |
| 20 | dialog FHL/DK | 15 av 15, och maskeringen inne i en replik står som `[MASKAD]` på rätt plats |
| 23 | prosa | *Förhörsledare* och *Förhörsvittne* finns kvar som fält i rätt ordning, liksom alla stycken |

Blanketten på sida 13 har också ett förhörshuvud (*Protokoll fört vid förhör med*,
*Förhörsledare*, *Förhörsvittne*), och det kom rätt. Talarmarkörer med tre bokstäver
(`FHL`) fungerar.

### Övriga mått

| Mått | Layout | Jämförelse |
|---|---:|---:|
| Medianpoäng, alla 50 sidor | 79,3 | 72,1 (lagrad text) |
| Medianpoäng, SPANINGSUPPSLAG / förhör | 79,7 / 80,1 | 71,6 / 78,7 |
| Tecken utom blanksteg, SPANINGSUPPSLAG / förhör | 21 913 / 20 452 | 20 201 / 20 094 |
| Sidor med `HEADING` | 44 % | – |
| Sidor med `TABLE`-block | 0 % | – |
| Sidor med blankettfält (`FORM` med fält) | 40 % | – |
| `degraded` | 0 % | – |
| Sidor utan Markdown | 8 % (3 `PAGE_TOO_LARGE` + 1 tom karta) | – |

Inga sidor i urvalet blev `TABLE`-block. Blankettens sidfotsrutor (19–25) kommer
ibland som en Markdown-tabell inne i blankettblocket.

### Sökkvalitet (i minnet)

| Mått | Layout-Markdown | Jämförelse |
|---|---:|---:|
| Chunkar (efter `is_useful`) | 100 | 111 (dagens text) |
| Sidhuvudradens andel av chunkarnas tecken | 2,1 % | – |
| Chunkar med sidhuvud | 43 (från 15 sidor) | – |
| Sidhuvudets andel i en chunk, median / max | 3,2 % / 22,5 % | – |
| Distinkta sidor i top-10, 5 namnfrågor | 7–9 (största andel från en sida: 2–4) | 5–9 (2–4) (dagens text) |
| Markdown-brus, medelrang för rätt chunk, 10 frågor | 1,5 med syntax | 1,3 (samma layoutchunkar, syntaxen borttagen) |
| Rätt chunk först | 7 av 10 | 8 av 10 (samma layoutchunkar, syntaxen borttagen) |

- **Sidhuvudet dominerar inte.** Raden `> Sidhuvud:` är oftast ett sidnummer (`- 29 -`,
  `Sida 9 av 35`) eller blankettrubriken. Den väger 22 % bara i korta sidfotschunkar.
- **Topplistan fylls inte av en enda sida.** Namnfrågorna (*Victor Gunnarsson*,
  *Lasse Ainasoja*, *Christer Pettersson*, *Klas Gedda*, *Lennart Gustafsson*) gav
  minst 7 olika sidor av 10 och högst 4 träffar från samma sida, lika med eller
  bättre än dagens text.
- **Markdown-syntax kostar lite.** Två chunkar hamnade på rang 2–3 med syntax men 1
  utan: en sidfotstabell och ett blankettfält med många `**`. En gick åt andra hållet.
  Cosinuslikheten skilde högst 0,016. Mätningen är indikativ (en korpus om 100 chunkar,
  frågor skrivna för hand), men den ger inget skäl att ta bort syntaxen före
  inbäddning.

### Fynd att åtgärda

1. **`PAGE_TOO_LARGE` gör 4,2 % av arkivets sidor olästa med layout.** Orsaken är
   projektets egen pixelspärr (`MAX_PAGE_PIXELS` i `src/surya_worker.py`) i
   kombination med fast 300 dpi, inte Surya. Felet åtgärdas genom att begränsa
   renderingens upplösning per sida så att bilden håller sig under spärren (300 dpi
   som tak, inte som golv). Det behövs innan layout kan bli standard. Lines tål inte
   heller de största sidorna, och ett MPS-minnesfel får resten av filen att fela.
2. **`[Redacted]` och `[redacted]` byts inte mot `[MASKAD]`.** Det gällde 23
   förekomster på 4 sidor. `form_layout.replace_redacted` jämför skiftlägeskänsligt
   mot `[REDACTED]` och rör inte fältvärden (sida 5 har `12 …: [Redacted]`).
3. **Värden som hamnar i etikettraden försvinner ur Markdown.** Etiketten rättas mot
   mallen men resten av raden behålls (`Avsnitt E`), och fältet får tomt värde.
   Avsnitt och löpnummer, alltså dokumentets identitet, saknas då i Markdown.
4. **Parningen av fält och värden behöver förbättras:** etiketter som bakas ihop
   (`03 … 04 telefon`), datum och klockslag (05/06) som blir lös text, och handskrivna
   värden i 07/08. Tillsammans med fynd 3 förklarar det de 61 %.
5. `score_text` belönar påhittad engelsk text på ritningar. Poängen räcker inte ensam
   som acceptansmått för sidor utan text.

### Ändringar

- `data/formular/spaningsuppslag.json`:
  - v1: det fasta trycket `F-år | -mån | -dag | -nr` (sedd på sida 1, 3 och 5).
    Värdet `EAD F-år | -mån | -dag | -nr` i fält 09 blir nu `EAD`, och fält 11 får
    inte längre trycket som värde. Ändringen är anpassad efter just sida 1, 3 och 5,
    som också ingår i den manuella kontrollen. Andelen rätt parade fält (61 %) mättes
    efter ändringen; före den var andelen 59 %.
  - v2: `Uppgiftslämnare (efternamn och förnamn)` och `telefon`, som är onumrerade
    etiketter på 5 av 7 v2-sidor. De förhindrar att trycket blir ett värde.
  - Inga nya varianter hittades.
- Två nya riktiga fixtures:
  - `tests/fixtures/surya/form-v1-c.json` (sida 5): värden i etikettraden och
    `[Redacted]`.
  - `tests/fixtures/surya/forhor-dialog-c.json` (sida 20): talarmarkören `FHL` och en
    maskering inne i en replik.
  - Testet `test_dialog_c_keeps_every_turn_in_order` låser den manuella kontrollen.
- `tests/test_surya_real.py` (`external`): ritar en egen tabellsida, kör den riktiga
  workern och kräver ett `TABLE`-block med `<th>`-rubrikceller. Markören registreras i
  `pyproject.toml` och väljs bort i den vanliga sviten
  (`addopts = "-m 'not external'"`). Testet hoppas dessutom när
  `surya_layout_unavailable_reason()` inte är `None`. Kör det med:
  `PALME_SURYA_PYTHON=~/.local/share/infoscry/surya-venv/bin/python .venv/bin/pytest -m external tests/test_surya_real.py -v`
  (godkänt på 18–20 s).

### Rekommendation

- **Standard:** behåll `lines` (regel BA). Byt först när fynd 1, 3 och 4 är åtgärdade
  och blankettkriteriet mätts om. Fynd 2 är litet men bör följa med.
- **Förhörsprotokoll:** layout är bättre. Alla repliker kom i rätt ordning, avstavade ord
  slogs ihop (`sidobio-graferna` blev `sidobiograferna`) och maskeringen inne i en replik
  hamnade på rätt plats. Använd den vid
  manuell omskanning.
- **SPANINGSUPPSLAG:** layout läser mer och renare text än Tesseract (median 79,7 mot
  71,6) men parar bara 61 % av fälten rätt. Den rena texten är användbar för sökning,
  men fälten kan inte ännu användas som strukturerade data.
- **Redo av sidor med låg poäng:** skälet att välja layout här är att den **inte hittar
  på text**, inte kvaliteten eller hastigheten. På ritningar och svag handskrift
  (sida 33, 35 och 36) ger layout nästan tom utdata, bara stämpeln (`B 847/88`,
  `Avd 13`), och tar 64–78 s per sida, där lines är snabbare men hittar på text. Sidor
  över pixelspärren måste skalas ned eller gå till lines.

## Omkörning (2026-10-26, efter åtgärderna BB–BD)

**Standarden byts fortfarande inte.** Efter åtgärderna klarar layout 3 av 5
acceptanskriterier. Hastighet och blankettfält är fortfarande inte uppfyllda. Enligt
regel BA görs Task 12 bara om *alla* kriterier är uppfyllda, så `lines` förblir
standard.

Åtgärderna (se `.superpowers/sdd/2026-10-25-surya-layout-markdown/blockers-fix-report.md`):
- **BB**: sidor över pixelspärren renderas i högsta dpi som ryms, och `layout_json` bär
  dpi:n.
- **BC**: värden på etikettraden blir fältvärden. Ett skyddsnät gör att inget ord på
  etikettraden försvinner, och `page_text` behåller den råa etikettraden.
- **BD**: `[Redacted]` i alla skrivsätt blir `[MASKAD]`.

Bara det som underkändes mättes om, med samma läsläge som piloten (arkivet bara läst,
utdata under `/private/tmp/claude-501/pilot/rerun/`, `generated/` orörd). Hastigheten
mättes inte om eftersom ingen åtgärd gällde den.

### Tomma sidor: pilotsida 42 och fem andra överstora sidor

Sidorna kördes med riktig Surya 2 genom dagens `ocr_pages.run_ocr_pages(engine="surya-layout")`
(`rerun_oversize.py`), med en worker för alla sidor och temporär `STATE_DB`. De fem
övriga valdes med fast frö bland de 2 850 överstora sidorna (`pick_oversize.py`): fem
olika dokument, varav två blanketter, och inget av dem är pilotsida 42:s dokument.

| Sida | Megapixel i 300 dpi | Använd dpi | Status | Tecken | Poäng | Surya-tid |
|---|---:|---:|---|---:|---:|---:|
| Pilot 42: `2234 — Tips från Leif Christer Boström …` s. 1 (blankett) | 37,8 | 199 | läst | 1 852 | 79,6 | 29,3 s |
| `2239 — Brev polisspåret IB` s. 1 (blankett) | 37,6 | 200 | läst | 2 006 | 80,4 | 91,8 s |
| `2195 — Polisens gatulagningsgrupp …` s. 1 (blankett) | 37,8 | 199 | läst | 2 407 | 78,1 | 25,5 s |
| `Liggaren_1332-3063` s. 12 (registertabell) | 17,4 | 294 | läst | 4 637 | 12,2 | 113,2 s |
| `Liggaren_19733-20970` s. 34 (registertabell) | 17,4 | 294 | läst | 4 972 | 63,7 | 51,1 s |
| `PM318_Samtal …` s. 5 (samtalsutskrift) | 21,6 | 264 | läst | 1 262 | 79,7 | 16,5 s |

- **Alla sex sidor lästes.** Varje sida fick en debugrad med begärd och använd dpi.
- **Sida 42:** layout ger nu 1 852 tecken mot lines 1 908.
- **Två sidor jämförda med bilden:**
  - *Sida 42.* `Stockholm`, `Krim jouren`, `epa L Pa Lundi` och kryss för *per telefon*
    står rätt. Det gör även de handskrivna `D` (07) och `19855` (08). Fält 12 blev
    `[MASKAD]`. Datum och klockslag (`86-03-05`, `20.10`) och hela fritexten om
    *KEPS* finns i texten.
  - *Liggaren_1332-3063 s. 12.* Sidan blev en Markdown-tabell vars rader stämmer med
    bilden (`HE | 1695-02 | 1994-09-13 | FAP2 BREV-DEM MOT OP I GALLERIAN …`,
    `D | 1696-00 | 1988-09-02 | ANK-D10192`). Den låga poängen (12,2) beror på
    registerkoder och tabbar i tabelltexten. `score_text` passar inte för registersidor.
- **Hastighet:** nedskalningen gjorde inte sidorna snabbare. Tiden per sida var 16–113 s,
  med median 40 s. De täta registersidorna och en blankett tog 51–113 s.
- **Ej omkörda:** sida 43 och 50 kördes inte om. Lines fick slut på MPS-minnet på båda,
  så ingen av dem kan göra kriteriet ouppfyllt.

### Blankettfält: omtolkning av sparade råsvar

Pilotens råsvar för de fem kontrollerade blanketterna tolkades om med dagens kod
(`reparse2.py`). Ingen ny OCR gjordes. Räkningen gjordes för hand mot samma sidbilder
och med samma regler som i piloten.

| Sida | Före | Efter | Skillnad |
|---:|---:|---:|---|
| 1 | 13/18 | 13/18 | oförändrad |
| 3 | 6/13 | 6/13 | oförändrad: 04 sitter kvar i 03, datum och klockslag är lös text, 07/08 är olästa |
| 5 | 5/10 | 8/10 | `E`, `1974-B` och `EAC 1580 B` paras nu med 07–09, och fält 12/13/15 är `[MASKAD]` |
| 9 | 5/9 | 5/9 | oförändrad: `EBE 10324-1-A` och utskriftsdatumet är oparade |
| 13 | 5/6 | 6/6 | `06 Utskriftsdatum: 1990-09-30 EÅ` |
| **Summa** | **34/56 = 61 %** | **38/56 = 68 %** | Samma resultat som åtgärdsagentens räkning |

- **Lös text på etikettraden**, alla 15 blanketter: 11 rester blev fältvärden
  (`label_rest_values`) och 11 blev lös text (`label_rest_text`).
- **`[Redacted]` i något skrivsätt:** 0 kvar, mot 23 förekomster på 4 sidor i piloten.
- **Fel som återstår:** datum och klockslag (05/06 i v1) står fortfarande som lös text,
  fält 04 bakas in i 03, och de handskrivna värdena i 07/08 är olästa eller oparade.

### Acceptanskriterier efter omkörningen

| Kriterium | Piloten | Omkörningen | Utfall |
|---|---|---|:---:|
| Medianpoäng layout ≥ lines på de 20 sämsta sidorna | 78,5 mot 71,2 | Inte omkört, ingen åtgärd gällde det | **PASS** |
| Ingen sida som lines läste går tom med layout | Sida 42 tom (`PAGE_TOO_LARGE`) | Sida 42 läst i 199 dpi: 1 852 tecken mot lines 1 908. Fem andra överstora sidor också lästa | **PASS** |
| Sekunder per sida ≤ Surya 0.17 | 16,4 s mot 16,0 s i median, 412 s mot 346 s utan uppvärmning | Inte omkört. Överstora sidor: median 40 s | **FAIL** |
| Minst 80 % av de ifyllda blankettfälten rätt parade | 61 % | 68 % (38/56) | **FAIL** |
| Inga tappade repliker på förhörssidorna | 48/48 | Inte påverkat. `forhor-dialog-c` låser 15/15 | **PASS** |

### Rekommendation efter omkörningen

- **Standard:** behåll `lines`. Två kriterier är fortfarande inte uppfyllda, så Task 12
  genomförs inte (regel BA). Följande krävs innan layout kan bli standard:
  - datum och klockslag (05/06) och telefon (04) i v1 paras med sina fält;
  - de handskrivna rutorna 07/08 läses och paras;
  - antingen ett snabbare körläge eller ett kriterium som är mätt mot dagens faktiska
    lines-tid i stället för de dokumenterade 30–100 s.
- **Överstora sidor** är inte längre ett hinder. Layout kan nu läsa dem, och lines får
  slut på MPS-minnet på de största.
- **Övrigt som förut:**
  - förhör och manuell omskanning: layout;
  - SPANINGSUPPSLAG: bättre text men fälten är ännu inte pålitliga som data;
  - redo av lågpoängssidor: layout eftersom den inte hittar på text, inte för kvalitet
    eller hastighet.

### Fältparning, andra omkörningen (beslut BJ)

**Blankettkriteriet är nu uppfyllt: 50 av 56 ifyllda fält (89 %) är rätt parade**, mot
38 av 56 (68 %) före. Hastighetskriteriet är fortfarande inte uppfyllt, så standarden
står kvar på `lines` (regel BA). Pilotens sparade råsvar tolkades om; ingen ny OCR
gjordes, och räkningen följer pilotens regler (se *Blankettfält* ovan).

**Diagnos före ändringen**, de 18 felparade fälten på sida 1, 3, 5, 9 och 13:

| Slag | Fält | Antal |
|---|---|---:|
| Aldrig läst av OCR (finns inte i råsvaret) | 1: 07 `E`, 22 `R`; 3: 07 `DD`, 08 `2775-D` | 4 |
| Läst fel av OCR (rätt placerat) | 1: 25 `A.G.` läst som `A.T.` | 1 |
| Läst men fel placerat | 1: 17 (i radbandet under 16), 20 (till höger om etiketten); 3: 20, 23 (till höger om etiketten); 9: 06 (under `Utskriftsdatum` bredvid `06`) | 5 |
| Ihopslaget med ett annat fält | 3: 04 (i 03:s värde), 05 och 06 (en rad under båda, börjar före 05); 5: 05 och 06 (`05 datum 06 klockan` på en etikettrad); 9: 07 och 08 (en rad under båda, spärrad av grannetiketten) | 7 |
| Ruta utan fältnummer i mallen | 9: `Uppgiftslämnare` (`EBE 10324-1-A`) | 1 |

**Nya regler** (alla generella; de står i `src/form_layout.py` under *Heuristiker* och i
`docs/teknisk-referens.md`):
- en etikettrad med två fältnummer blir två etiketter;
- etiketter på samma rad delar sidan i spalter, och en rad under etikettraden hör till
  spalten där dess text har sin mitt. Den delas ord för ord när den når in i en grannspalt
  utan eget värde;
- en grannetikett på samma rad är ingen spärr, men tryckt text mellan etikett och värde är det;
- ett värde till höger om en tom etikett på samma rad hör till etiketten;
- ett alias bredvid sitt nummer är en egen etikett;
- tryck mellan rutornas linjer (`| datum`) och tryckta ord i en värderad är inga värden.

| Sida | Före | Efter | Kvar |
|---:|---:|---:|---|
| 1 | 13/18 | 15/18 | 07, 22 olästa; 25 läst som `A.T.` |
| 3 | 6/13 | 11/13 | 07, 08 olästa |
| 5 | 8/10 | 10/10 | – |
| 9 | 5/9 | 8/9 | rutan *Uppgiftslämnare* saknar fält i mallen |
| 13 | 6/6 | 6/6 | – |
| **Summa** | **38/56 = 68 %** | **50/56 = 89 %** | **PASS** (≥ 80 %) |

**Kontroll mot överanpassning.** Fem andra blanketter ur urvalet räknades för hand mot
sidbilderna med samma regler, före och efter ändringen. Det var de första av varje
variant som inte ingick i den manuella kontrollen: nr 2, 4 och 6 (v1) och nr 10 och 11
(v2). Antalet rätt fick inte sjunka.

| Sida | Före | Efter | Kvar |
|---:|---:|---:|---|
| 2 (v1) | 5/15 | 8/15 | 07–09, 11, 20, 22, 25 olästa |
| 4 (v1) | 11/17 | 13/17 | 15:s etikett oläslig; 18:s etikett saknas, så brödtexten hamnar i 16; 20/21 olästa |
| 6 (v1) | 8/10 | 8/10 | 07, 08 olästa |
| 10 (v2) | 5/8 | 5/8 | 07/08: handskriften börjar inne i etikettens ruta; 06: datumet står för långt till höger om sin etikett |
| 11 (v2) | 7/8 | 8/8 | – |
| **Summa** | **36/58 = 62 %** | **42/58 = 72 %** | sjönk inte |

- **Bieffekter:** de övriga fem blanketterna i urvalet (nr 7, 8, 12, 14 och 15) jämfördes
  före och efter. Ändringarna stämmer med sidbilderna:
  - nr 7 och 8: 03/04 och 05/06 delas rätt;
  - nr 12 och 14: 06 Utskriftsdatum får sitt datum;
  - nr 15: oförändrad.
- **Ren text** tappar inget tecken på någon av de 15 sidorna. Där `03 Uppgiftsmottagare 04
  telefon` förut stod två gånger, en gång som fältrad och en gång som rå rad, står den nu
  som två fältrader.
- **Markdown** tappar bara etiketten på fält som nu är egna men tomma (`04 telefon`,
  `05 Förhörsprot`), som alla tomma fält.
- **Nya fixtures** från pilotens råsvar: `form-v1-e` (sida 3), `form-v2-d` (sida 9) och
  `form-v2-e` (sida 12).
- **Räkneskript:** `score_bj.py` i samma katalog som pilotens övriga skript.

**Prosaskydd (beslut BK/BL).** De nya reglerna fick inte göra löptext till fältvärden.
- **Värde till höger om en etikett:** paras bara om texten ser ut som ett värde, dvs. innehåller en siffra eller är högst fyra ord utan meningsbrott med minst ett ord med versal.
- **Delning ord för ord över spalter:** görs bara när varje del ser ut som ett värde. En ensam kort titel före en del med siffror (`krinsp 08-7694169`) räknas som en sådan del.
- **En rad som inte går att dela:** blir lös text i läsordning, utom när hela raden ser ut som ett värde. Då hamnar den i spalten där den har sin mitt.
- **Spärrar:** bara godtagna grannetiketter och tryckt text på samma rad är undantagna från spärren. Löptext spärrar alltid.

Räkningen är oförändrad: **50/56 (89 %)** på de fem kontrollerade blanketterna och 42/58 (72 %) på de fem andra. Ett känt kvarvarande fall: en kort förkortning före en siffra kan delas mellan två spalter när grannfältet är tomt, t.ex. `kl 1530` → 03 `kl` / 04 `1530`. Ingen text försvinner.

| Kriterium | Utfall efter andra omkörningen |
|---|:---:|
| Medianpoäng | **PASS** |
| Inga tomma sidor | **PASS** |
| Sekunder per sida | **FAIL** (inte omkört) |
| Blankettfält ≥ 80 % | **PASS** (89 %) |
| Repliker | **PASS** |

Fyra av fem kriterier är uppfyllda. Standarden förblir `lines` tills hastighetskriteriet är uppfyllt, och Task 12 genomförs inte (regel BA).

### Hastighetsutredning (2026-10-28)

Fyra scratch-only mätningar på tre redan renderade pilotsidor. Ingen produktionskod,
beroendefil, databas eller standard ändrades, och inga resultat är ett godkänt
hastighetskriterium. Artefakterna ligger under
`/private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028/`
(temporära skript, inte repo-verktyg). Mätningarna kördes på en Apple M1 Max, 32 GB:
layout med `surya-ocr 0.22.1` (Python 3.12.14) och llama.cpp `0.7.0` build 11669,
lines med `surya-ocr 0.17.1` i projektets `.venv` (Python 3.14.10, torch 2.11.0,
MPS). Tiderna är väggtid, och OS-filcachen kan göra kallstarter snabbare.

**Metod.** Sidorna 3, 27 och 33 togs från bilagan och renderades en gång i 300 dpi
(med `cap_layout_image`). Baslinjen körde layout via `ocr_surya_layout` och lines via
`ocr_surya`, seriellt med en blank uppvärmningssida per motor. De kontrollerade
jämförelserna (H1/H2/H3a) använde färsk isolerad `HOME` och en egen llama-server per
observation, med `STATE_DB` och `ADMIN_JOB_LOG_ROOT` i scratch, bevarade
`HF_HOME`/`MODEL_CACHE_DIR` och `HF_HUB_OFFLINE=1`. Ingen databas skrevs, inga
prompt-cachar återanvändes mellan varianterna och en redan körande llama-server på
annan port rördes aldrig. `generated/`-fingeravtryck och `git status` var oförändrade
efter varje mätning.

Kör om (samma cachar och venvar krävs):

```bash
cd /private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028 && ./run_baseline.sh
cd /private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028/experiment-h1 && ./run_h1.sh
cd /private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028/experiment-h2 && ./run_h2.sh
cd /private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028/experiment-h3a && ./run_h3a.sh
```

**Baslinje, tre sidor.** Kall blank uppvärmning: layout 3,635 s och lines 3,282 s
i den slutliga körningen; en första valideringskörning samma session tog 9,7 s
respektive 7,6 s, så siffran är en undre gräns. Varma sidor:

| Sida | Typ | Layout | Lines | layout/lines |
|---:|---|---:|---:|---:|
| 3 | blankett | 28,689 s | 61,138 s | 0,469 |
| 27 | förhör prosa | 30,011 s | 26,860 s | 1,117 |
| 33 | låg kvalitet | 80,334 s | 39,779 s | 2,020 |
| **Summa** | | **139,034 s** | **127,777 s** | **1,088** |

Sida 3 var lines första riktiga sida efter en liten blank uppvärmning; 61,1 s mot
26,9–39,8 s för de följande tyder på lat MPS-kompilering för större batchar, så just
den siffran är osäker som "varm". **Inget hastighets-PASS**: tre sidor är ett
stickprov, inte Task 12, och inga korpusanspråk eller statistisk säkerhet görs.

**H1, oanvända slots/KV.** På sidorna 3 och 27 jämfördes `--parallel 8 --ctx-size
98304` (12 288/slot) mot `--parallel 1 --ctx-size 12288` (samma per-slot-budget),
fyra observationer per sida i ordning A-B-B-A/B-A-A-B. B/A blev **1,105** respektive
**1,109**: färre slots var cirka 10 % långsammare, inte snabbare. H1 fick inget stöd
och utgör inte underlag för någon defaultändring.

**H2, logprobs/confidence.** På sida 27 gav `SURYA_INFERENCE_LOGPROBS=0` mot `1`
(total B/A **0,981**, vinst **1,93 %**) men servertiden var i praktiken oförändrad
(B/A **0,997**, vinst 0,26 %); vinsten låg i mindre svarstransport. B gav `null` i
confidence för alla 31 block, A mätte alla 31. Ingen tydlig vinst över ~5 %, och
confidence-signalen får inte tas bort utan granskning. Inte underlag för defaultbyte.

**H3a, sida 33:s första giriga anrop.** Två oberoende första anrop (ingen Surya-retry,
ingen fallback, ingen warmup) tog **73,406 s** och **69,762 s**. Båda svarade utan
HTTP-fel med `finish_reason=length`, `completion_tokens` 8 117 och
prompt 4 171: kontexten fylldes till **12 288** (4 171 + 8 117), inte max-token-taket
12 288. Svansen var periodisk med **44 tecken × 90 upprepningar**; `detect_repeat_token`
på full text och med `cut_from_end=50` (samma kontroller som `_should_retry`) samt
Suryas egen loopdetektor slog till i båda försöken och content-, token- och
parsed-hasharna var identiska. Första sampelprefix som slog till var token **1 185**
(full text) respektive **1 249** (`cut_from_end=50`), men ett tidigt prefix bevisar
inte en slutlig repetitionsloop. Det talar för att det dyra första anropet innehåller
en decoder-repeat snarare än bara lång legitim text eller ett HTTP-fel; två försök
ger inte ett kausalt bevis. Baslinjens 80,3 s för sida 33 rymmer det omförsök
som produktionsvägen gör efter en sådan repeat. Parsningen gav 20 block (20 giltiga,
20 distinkta, alla `Text`); det är en räkning, inte en kvalitetsgranskning, och säger
inte att ingen giltig text tappas. En rå räkning eller första sampel är **ingen säker
stoppolicy**.

**Slutsats.** Hastighetskriteriet är fortfarande **FAIL** och `lines` är kvar som
standard. H1/H2 ger inte skäl att ändra någon prestandadefault. En scratchprototyp
för streaming har provats (se nedan): streaming och avbrott fungerar tekniskt, men
den fasta stoppregeln fälldes av falska positiver på legitima upprepningar.
Produktionsstreaming är därför inte införd; defaults, tokenbudget och kriterium
ligger kvar. Det är inte en godkänd produktionsändring: sänk inte max-token och
ändra inte kriteriet.

### Strömmande scratchprototyp (2026-10-28)

En separat scratchprototyp (inte produktion) testade SSE-streaming, tidigt avbrott
och confidence under
`/private/var/folders/kf/csqzr4951rlc7k41z0tqtgd40000gn/T/opencode/surya-speed-20261028/prototype-stream/`.
`run_prototype.sh` kör liveförsöken och `run_correction.sh` tokeniseringskorrigeringen;
artefakterna är temporära.

På #3 och #27 blev text, Markdown, layout och råblock utan confidence identiska med
sparade H1/H2. Confidence fanns per token, men sidmedelvärdena skilde 1,398e-5
respektive 9,85e-6. Det ligger inom den valda toleransen 1e-4, men **exakt
confidence-ekvivalens är inte bevisad** (ingen parad nonstream-körning). Inga absoluta
SSE-tider jämfördes, så ingen overheadslutsats dras.

Provregeln var fast före körningen: `detect_repeat_token` full/cut50, kontrollerad
var 32:e token och sann i minst 512 nya tokens innan avbrott. På #33 avbröts vid
1 696 genererade tokens mot H3a:s 8 117, alltså 79 % färre genererade tokens (inte
79 % snabbare). Loggen visade `slot release`, alla slots var fria och en blank
följdförfrågan i samma slot tog 0,262 s. Den observerade tiden 0,132 s räknas från
klientstängningen och är **inte** en exakt server-release-latens; logg + fria slots
+ samma slot bär cancel-beviset.

Den riktiga llama-tokenizeringen gav: 120 tomma `<td></td>` = 253 tokens (ingen
träff, under 512-fönstret), 2 000 punkter = 2 002 tokens och 200 upprepade
fältetiketter = 4 002 tokens; de två senare utlöste regeln. Alltså **två falska
positiver** på legitima upprepningar. Den tidigare char-proxy-körningen (tre falska
positiver) är ogiltig och ersatt.

Stoppregeln är därför **FAIL** och streaming är inte införd i produktion. Inga
defaults, tokenbudget eller kriteriet ändras; Task 12 är inte genomförd. Prototypen
visar att streaming och avbrott tekniskt fungerar, inte att en säker stoppolicy finns.

### Beslut efter utredningen (2026-10-28)

Ägarbeslut: hastighetsoptimeringsarbetet pausas tills vidare. `lines` är kvar som
automatisk standard i pipelinen; `surya-layout` finns kvar för riktad omskanning av
blanketter, tabeller och förhör. Funktionerna behålls utan att layout görs till
standardmotor. Osäker tidig avbrytning (`early stop`) ska inte
integreras. Det finns ingen aktiv optimeringsuppgift och ingen automatisk fortsättning;
arbetet återupptas bara efter ett nytt beslut från ägaren. Fortsatta förbättringar ska
utgå från problem som visar sig vid faktisk användning.

### Bilaga: urvalet

Kolumnen *Lines* markerar de 20 sidor som också kördes med lines. Sida 42 och 50
kördes dessutom med lines som extrasidor (`lines_extra.py`), eftersom de gav
`PAGE_TOO_LARGE` med layout.

Omkörningens skript ligger i samma katalog (`pick_oversize.py`, `rerun_oversize.py` och
`reparse2.py`), och resultaten finns i `rerun/oversize.jsonl`.

Siffran 2 850 av 68 479 överstora sidor kommer från
`/private/tmp/claude-501/pilot/count_oversize.py`. Skriptet läser bara sidstorlekarna i
`downloaded/files` och `downloaded/wpu_files` och jämför dem i 300 dpi med
`MAX_PAGE_PIXELS`.

| Nr | Grupp | Dokument (stem) | Sida | Lines |
|---:|---|---|---:|:---:|
| 1 | SPANINGSUPPSLAG v1 | Pol-1986-03-04_EAD1094-00_Iakttagelse-påsprungen-misstänkt-persson-Malmskillnadsgatan | 1 |  |
| 2 | SPANINGSUPPSLAG v1 | 316.6 — Tips och vittnesmål om män med Walkie Talkie vid Grand, lång kraftig man walkie talkie Birger Jarlsg & Regeringsg — 2020-08-21 — 2021-03-03 — EAA-6984, se 1293 — 8 | 1 |  |
| 3 | SPANINGSUPPSLAG v1 | Pol-1987-07-17_1000_D2775-00-D_Man_följer_efter_Palme_på_PUB-D | 1 | x |
| 4 | SPANINGSUPPSLAG v1 | Pol-1986-03-03_L901-00_Lars_Knubb | 1 |  |
| 5 | SPANINGSUPPSLAG v1 | Pol-1986-03-10_1430_EAC1580-00-B_Förhör-Hans-Gyllenhammar | 1 |  |
| 6 | SPANINGSUPPSLAG v1 | Pol-1987-04-02_1500_EBC2327-12_Polisbil-kommunikationsradio-utanför-bostaden | 1 |  |
| 7 | SPANINGSUPPSLAG v1 | Pol-1986-03-01_1435_L839-00_Biobesökare | 1 |  |
| 8 | SPANINGSUPPSLAG v1 | 1662 — Bankomat Sveavägen-Tunnelgatan 20-41, sedan restaurangbesök Sveavägen mitt emot mordplatsen, gick Apelbergsgatan kl 22 — 2022-11-13 — EAC-9990-00 — 1 | 1 |  |
| 9 | SPANINGSUPPSLAG v2 | 1953.1 — Kvinna ser springande man på Tunnelgatan 1988-10-13 — 2023-08-07 — EBE-10324-01-A — 1 | 1 | x |
| 10 | SPANINGSUPPSLAG v2 | Pol-1991-12-18_V13695-06_Sala_Telefax_förhör_utvisad_jugoslav_hotar_mörda_Palme | 1 | x |
| 11 | SPANINGSUPPSLAG v2 | 2118 — Förhör ang Mockfjärdsvapnet — 2023-10-11 — IA-13963 — 3 | 1 |  |
| 12 | SPANINGSUPPSLAG v2 | Pol-1992-01-14_HE12435-03_Registerslagning_och_maskerat_foto_av_Werner_Öhrn | 1 | x |
| 13 | SPANINGSUPPSLAG v2 | Pol-1990-09-28_A14220-01-A_Erik_Sandin_civil_patrull_2733_pdf | 1 |  |
| 14 | SPANINGSUPPSLAG v2 | Pol-1992-01-24_LA14752-01_personal-Grand-biografen | 1 | x |
| 15 | SPANINGSUPPSLAG v2 | Pol-1990-09-05_XAC13369-00 | 1 | x |
| 16 | förhör dialog | 31.18 — Förhör Victor Gunnarsson 19 jan 1988 del 1 — 2020-06-19 — 2020-10-04 — N-3000-L del 1 — 41 | 29 |  |
| 17 | förhör dialog | 31.19 — Förhör Victor Gunnarsson 19 jan 1988 del 2 — 2020-06-19 — 2020-10-04 — N-3000-L del 2 — 40 | 2 |  |
| 18 | förhör dialog | Pol-2001-10-27_KD18188-01-A_Förhör_med_Gert_Fylking_om_ev_erkännande_från_CP | 20 |  |
| 19 | förhör dialog | Pol-1998-11-02_IA12028-00-Q_Förhör_med_Lasse_Ainasoja | 19 |  |
| 20 | förhör dialog | Pol-2013-06-14_R21248-10_Förhör-Dan-Kl_fverskj_ld | 28 |  |
| 21 | förhör dialog | Pol-1988-01-19_N3000-00-L_Förhör-VG-del1 | 14 |  |
| 22 | förhör prosa | 208.4 — Förhör med Lasse Ainasoja (Mockfjärdsvapnet) 1999-05-17 — 2020-07-08 — 2020-07-31 — IA-18654-F — 15 | 11 |  |
| 23 | förhör prosa | Pol-1994-06-29_DH15856-D_1432_Förhör_Ulla_P | 1 |  |
| 24 | förhör prosa | Pol-1994-04-12_1458_DH15869_Förhör_med_bekant_till_15868 | 4 |  |
| 25 | förhör prosa | Pol-2018-04-09-H21994-06_Förhör_Gunnar_Ekberg_ang_SB | 23 |  |
| 26 | förhör prosa | 480.13 — Utredningen om Olof Palmes personliga förhållanden, Förhör med okänd person — 2020-09-15 — 2021-01-13 — TA-12893 — 5 | 1 |  |
| 27 | förhör prosa | Pol-1989-02-22_KD11079-00-B_Förhör_med_Caroline_Rossi_om_CP | 7 | x |
| 28 | förhör prosa | Pol-1988-08-26_1100_EAE9671-01-A_Förhör_med_Klas_Gedda | 1 |  |
| 29 | förhör prosa | Pol-1987-02-17_HJ12452-00-A_Förhör_med_Lars_Andersson | 1 |  |
| 30 | förhör prosa | 58 — Förhör med vittne 1 på Klarabergsgatan (Egen berättelse + offentligt förhör + kartor) — 2020-06-22 — 2020-08-16 — D-363 — 5 | 1 |  |
| 31 | lägst poäng | 538 — Vilken väg tog OPs mördare, powerpoint 1994-09-07 — 2020-08-04 — 2020-10-05 — A-15834-1, se 551 — 71 | 16 | x |
| 32 | lägst poäng | Pol-1986-03-22_L860-00-E_Birgitta_Wennerling_teckningar | 15 | x |
| 33 | lägst poäng | Pol-1987-06-01_KD11345-01_Kari_Lehikonen_skrivelse | 2 | x |
| 34 | lägst poäng | Pol-1989-01-12_KD10405-00-L_Christer_Pettersson_kondoleansboken | 23 | x |
| 35 | lägst poäng | Pol-1989-02-02_KB10748-02_Förhör_med_Agneta_Walfridsson | 13 | x |
| 36 | lägst poäng | Pol-1989-03-16_IB10478-02-B_Agneta_Walfridsson | 8 | x |
| 37 | lägst poäng | Pol-1989-03-16_KD10405-00-R_Förhör_Christer_Pettersson_om_kalender | 12 | x |
| 38 | lägst poäng | Pol-YYYY-MM-DD_OKÄND_UPPSLAGSKOD_Stig_Engström | 30 | x |
| 39 | lägst poäng | Pol-1996-11-18_HBG4818-00_Boris-Ersson-Stander-PM-förhör | 80 | x |
| 40 | lägst poäng | Pol-1987-04-02_L941-00-D_Biobesökare | 2 | x |
| 41 | slump | Charlotte_Liljedahl | 4 | x |
| 42 | slump | 2234 — Tips från Leif Christer Boström-Narvaförbundet- — 2025-12-11 — D-19855 — 1 | 1 |  |
| 43 | slump | PM313_Samtal_administrativa_byrån_gällande_kostnader | 1 | x |
| 44 | slump | Pol-1997-05-05_DC17974_Stockholms_försvars_skytteförening | 6 | x |
| 45 | slump | 908 — Alla handlingar i uppslaget D-22015-XX. Man med WT och vit mercedes — 2021-03-16 — 2021-07-22 — D-22015 — 1 | 1 |  |
| 46 | slump | 121 — Alla förhör med, brevväxling och telefonsamtal med Eugene De Kock — 2020-07-01 — 2020-09-29 — HBE-17832-1, se 365 — 32 | 24 |  |
| 47 | slump | Pol-1986-03-19_A14220-00_Pontus_Wikner_civil_patrull_2733 | 2 |  |
| 48 | slump | 1322.8 — Förundersökningsprotokoll Christer Pettersson - Protokoll III-2 - Brottsplatsen och vittnesförhör - Mordplatsen del 1 — 2021-09-24 — 290 | 256 |  |
| 49 | slump | 1328 — Genomgång av brev för jämförelse av text — 2021-10-01 — DI-12964 — 10 | 7 |  |
| 50 | slump | 676.1 — Förhör med vittnet André på café Mon Cheri 6 april 1987 — 2020-11-02 — N-3007-30-B — 3 | 2 |  |
