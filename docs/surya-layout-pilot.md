# Surya 2 med layout — spik och pilot

Underlag för planen `docs/superpowers/plans/2026-10-25-surya-layout-markdown.md`.
Spiken (Task 0) körs före all kod för att se vad Surya 2 faktiskt svarar för arkivets
två vanligaste dokumenttyper: blanketten SPANINGSUPPSLAG och förhörsprotokoll.
Piloten (Task 11) läggs till här när den är gjord.

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
