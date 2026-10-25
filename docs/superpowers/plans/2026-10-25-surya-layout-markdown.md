# Surya-layout med Markdown — implementationsplan

> **För agentiska utförare:** OBLIGATORISK UNDERSKILL: använd superpowers:subagent-driven-development (rekommenderas) eller superpowers:executing-plans för att genomföra planen uppgift för uppgift. Stegen använder kryssrutor (`- [ ]`).

**Mål:** Surya-OCR läser varje sida som layoutblock (rubriker, stycken, listor, tabeller, sidhuvud/sidfot) och sparar både ren text och Markdown, så att chunkning och LLM-kontext behåller tabeller och rubriker. En ny sida **Sidgranskning** visar varje PDF-sida bredvid den text (och Markdown/layout) som OCR:en gav, och låter användaren skanna om en enskild sida med vald motor — med föregående läsning sparad så att en sämre omskanning kan ångras.

**Arkitektur:** Samma mönster som infoscry (`/Users/patrik/projects/infoscry/scripts/ocr/surya_worker.py` + `SuryaStructureParser.kt`): en fristående worker i en **egen venv** med `surya-ocr==0.22.1` (Surya 2 via `llama-server`) läser en sida per JSON-rad över stdin/stdout och skickar råa block (HTML, etikett, läsordning, bbox). All tolkning av HTML → block → Markdown/ren text sker i projektets egen process, i en ren modul utan Surya-import. `pdf_pages.text` förblir ren text (alla nuvarande konsumenter oförändrade); Markdown och blocklayout läggs i nya kolumner och används av ingest bara när sidans text inte ändrats efter OCR.

**Tech stack:** Python 3.11+, stdlib `html.parser` (inget nytt beroende), SQLite via `src/db/`, `surya-ocr==0.22.1` + Homebrew `llama.cpp` i separat Python 3.12-venv.

**Spec:** Ingen separat spec — designbesluten står i avsnittet *Designbeslut* nedan och är det utföraren argumenterar från. Referens: `/Users/patrik/projects/infoscry/docs/specs/2026-10-18-structured-page-reading-design.md`.

## Designbeslut

1. **Varför egen venv och worker:** projektets `.venv` kör Python 3.14 med `surya-ocr 0.17.1` och `transformers<5`-pinnen (Gotcha 1). Surya 2 (`0.22.1`) är verifierad på Python 3.12 av infoscry och kör modellen via `llama-server`. Två beroendeträd i samma venv går inte att hålla ihop; workern körs därför med `PALME_SURYA_PYTHON` (standard `~/.local/share/palmemordsarkivet/surya-venv/bin/python`). På den här maskinen finns redan infoscrys venv (`~/.local/share/infoscry/surya-venv`, 0.22.1) och cachade vikter (`models--datalab-to--surya-ocr-2-gguf`) — miljövariabeln får peka dit.
2. **Ny motor, inte ersättning:** `--engine surya-layout` läggs till bredvid befintliga `surya` (radbaserad, 0.17). OCR-operationen får `--surya-engine {lines,layout}` med standard `lines` tills piloten (Task 11) visat att layout är minst lika bra på arkivets sidor; först då byts standarden.
3. **Text vs Markdown:** `text` = blockens rena text i läsordning, block skilda med `\n`, tabellceller med `\t` (som infoscry). `markdown` = samma block renderade som GFM. `PAGE_FOOTER` utelämnas (oftast sidnummer). `PAGE_HEADER` skrivs som första rad `> Sidhuvud: …` (högst 200 tecken) och blir **kontextrad i varje chunk** på sidan i stället för en egen chunk — i förhören är sidhuvudet (”Förhör med X … Sid 2(5)”) det som talar om vem som hörs. Båda finns kvar i `text` och därmed i fuzzy-sökning och citat. Kvalitetspoängen räknas alltid på `text`, så Markdown-tecken (`#`, `|`) aldrig sänker poängen och triggar redo i onödan.
4. **Färskhetsregel för Markdown i ingest:** en sidas Markdown används bara om `normalize_text.normalize(pdf_pages.text) == sidan i text/<stem>.txt`. Ändrad text (LLM-korrigering, merge_wpu, manuell redigering) → ren text som i dag. Ingen hash-kolumn behövs; `normalize` är ren.
5. **Maskeringar (efter spiken, se `docs/surya-layout-pilot.md`):** Surya skriver själv `[REDACTED]` på rätt plats i meningen för större maskeringar — det byts mot `[MASKAD]`. Svarta balkar i blankettfält utelämnas helt av Surya, så pixeldetektorn körs kvar och infogar `[MASKAD]` som eget block efter y-mittpunkt, men bara där inget block redan har `[MASKAD]` inom samma y-intervall. Små maskeringar i dialog kommer som `<input type="checkbox"/>`; i ett block utan fältnummer byts en sådan mot `[MASKAD]` när blockets bbox i sidbilden innehåller en *fylld* mörk rektangel (en riktig kryssruta är en ihålig ram). Funktionen `filled_dark_boxes(image, bbox) -> int` hör till Task 2b, och `ocr_surya_layout` (Task 5) skickar in sidbilden.
6. **PDF-textlagret** skrivs per block (blockets bbox + ren text via `insert_textbox`) i stället för per rad.
7. **En worker per `ocr_pages`-process** (en PDF). Uppstartskostnaden (modell + `llama-server`) mäts i piloten; delad worker över flera filer är uttryckligen utanför planen.
8. **Vilken text sidgranskningen visar:** den *gällande* texten är sidan ur `generated/text/<stem>.txt` (split på `\f`) — det är den som indexeras och citeras. Finns en rad i `pdf_pages` visas dessutom motor, poäng, tidpunkt, motoridentitet, Markdown och layout. Skiljer sig `normalize(pdf_pages.text)` från filens sida visas en notis om att texten ändrats efter OCR (samma färskhetsregel som ingest). `pdf_pages` innehåller bara sidor som OCR:ats per sida; övriga sidor kommer från hel-fils-OCR:en och visas med motorn ”hel-fils-OCR”.
9. **Omskanning är ett vanligt bakgrundsjobb:** ny operation `rescan-page` i registret (+ `scripts/rescan_page.py` + `./rescan_page.sh`), startad från sidan via `job_service.start_job`. Den respekterar jobbinvarianten om ett skrivande jobb åt gången; sidan inaktiverar knappen och visar det aktiva jobbet när ett annat körs. Jobbet: spara nuvarande läsning i historiken → OCR:a sidan med `--force` → `merge_one` → `normalize_text.process_file` → `mark_normalized` → inkrementell ingest (valbart, standard på).
10. **Formulär (SPANINGSUPPSLAG m.fl.):** nästan varje uppslag börjar med blanketten SPANINGSUPPSLAG — numrerade fält `01`–`25` och kryssrutor. Tesseract slår sönder den helt; Surya 2 läste i spiken alla maskinskrivna och även handskrivna värden rätt. **Fälten kommer sällan som `Form`:** oftast är etikett och värde separata `Text`-block (ibland `PageHeader` eller `Table`), värdet står under etiketten och är ofta i fetstil, och ibland ligger etikett och värde i samma block åtskilda av `<br/>`. Fält tolkas därför *geometriskt* över hela sidans block, oberoende av Suryas etikett: en rad som börjar med `\d{2}\s` är en etikett, och värdet är raderna/blocken under den i samma spalt fram till nästa etikett. Kryssrutor tolkas ur `<input … [checked]>`. Tomma fält skrivs inte till Markdown, eftersom blankettens huvud upprepas tomt på varje förhörssida. Det finns minst två varianter (`03 Uppgiftsmottagare` mot `03 Upprättat(d) av`), och kända mallar i `data/formular/*.json` rättar OCR-skadade etiketter (`Poliodistrikt` → `Polisdistrikt`). Fält som inte går att tolka tappas aldrig. **Efter genomförandet (2026-10-25):** fält tolkas *bara* när en mall matchar — utan mall lämnas blocken orörda, eftersom korta numrerade rader (kronologier, tidtabeller, antal) annars blev falska formulär och kastades om. Etiketter godtas bara när hela etikettexten liknar mallens, inte efter fritextfältet (18/10) om numret inte är högre, inte i fel nummerordning, och sidfotsfält (19–25) bara som en tät, ordnad grupp om minst tre. Heuristikerna står i modulens docstring (`src/form_layout.py`).
11. **Förhör:** protokollen är mest löpande berättartext (”Avsan uppger att …”) med inledande metadata (*Protokoll fört vid förhör med …*, *Förhöret hållet …*, `Förhörsledare:`, `Förhörsvittne:`) och ibland dialog (`F:`/`S:`, `Fråga:`/`Svar:`, `FL:`, namn i versaler följt av kolon). Surya ger stycken i rätt läsordning, vilket redan löser Tesseracts dubbla radavstånd och avbrutna meningar. Ovanpå det: repliker blir egna stycken med markören i fetstil (`**F:** …`), metadataraderna överst blir en fältlista som i formulären, och chunkningen respekterar repliker (beslut 3 och Task 6). Avstavning över radbrott (`med-` + `borgare`) slås ihop bara om spiken visar att Surya lämnar kvar den.
12. **Historik i stället för överskrivning:** ny tabell `pdf_page_readings` får en kopia av den gällande läsningen (inkl. filens sidtext om ingen `pdf_pages`-rad fanns) innan omskanningen skriver. **Återställ** lägger tillbaka en historisk läsning som ny gällande läsning via samma väg (record → merge → normalize → ingest), så också återställningen hamnar i historiken.

## Globala krav

- Alla kommentarer, docstrings, loggrader och felmeddelanden på svenska.
- `surya-ocr==0.22.1`, Python 3.12 i worker-venv:en; projektets `pyproject.toml`-extra `surya` lämnas orörd.
- Ingen SQL utanför `src/db/`; nya db-funktioner re-exporteras i `src/db/__init__.py` (`tests/test_sql_boundary.py`, `tests/test_db_package.py`).
- Nya toppnivåmoduler i `src/` läggs i `py-modules` i `pyproject.toml` (`tests/test_packaging.py`).
- Nya parametrar bara i `src/operations/registry.py`; Admin-hjälp i `OPERATION_HELP` måste ha en punkt per fält i formulärordning.
- Debugloggning via `ctx.log(..., level="debug")` för varje beslut (motor vald, sida föll tillbaka, Markdown förkastad och varför). Aldrig hela sidtexter eller HTML i loggar.
- `.venv/bin/python scripts/test.py --static` grönt efter varje uppgift.
- **Ingen `git commit`/`git push`** (AGENTS.md) — varje uppgift slutar med grön testkörning; användaren kör `/cap`.
- Dokumentation (`README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`) uppdateras i den uppgift som ändrar det de beskriver.

## Granskningsfokus

1. **Workern dör mitt i en fil eller hänger** → sidan loggas som fel, workerns processgrupp (inkl. `llama-server`) dödas, resten av filen hoppas och filen markeras inte som klar. Test i Task 3 (`test_worker_crash_kills_group`, `test_page_timeout`).
2. **Blank sida / bara figur** → status `empty`, sidan sparas med tom text så redo inte loopar på den, ingen `markdown`. Test i Task 5 (`test_empty_page_recorded_without_markdown`).
3. **Formulär som inte går att tolka** (fältnummer oläsligt, okänd blankettvariant, handskrivet värde utanför rutan) → blocket blir tabell eller stycke med all text kvar; ingen text försvinner ur `text` eller Markdown. Test i Task 2b (`test_unparseable_form_keeps_all_text`, `test_unknown_variant_uses_raw_labels`).
4. **Sidans text ändrad efter OCR** → ingest använder ren text, inte inaktuell Markdown. Test i Task 6 (`test_stale_markdown_ignored`).
5. **Omskanning när ett annat jobb kör, eller när PDF:en saknar sidan** → knappen är inaktiverad med förklaring respektive operationen avbryter med `OperationFailed` före all skrivning; historiken får ingen rad. Test i Task 9 (`test_rescan_rejects_page_out_of_range`) och Task 10 (`test_rescan_button_disabled_while_job_active`). (`$` i Markdown hanteras i Task 10:s visningstest.)

---

### Task 0: Spik — kör Surya 2 på riktiga förhör och blanketter

**Status:** klar 2026-10-25 — se avsnittet *Spik* i `docs/surya-layout-pilot.md`. Surya läser blanketter och förhör bra; planen är justerad för fältens placering, maskeringarnas tre beteenden, avstavning och hastigheten (~25 s/sida → layoutmotorn används riktat, inte på hela arkivet).

Syftet är att se vad Surya 2 faktiskt svarar för arkivets två vanligaste dokumenttyper innan parsern i Task 2/2b skrivs, så att tolkningen bygger på riktig HTML och inte på antaganden.

**Files:**
- Create: `tests/fixtures/surya/*.json` (rå workersvar), `docs/surya-layout-pilot.md` (avsnittet *Spik*)

- [x] **Steg 1:** Välj 10 sidor: 3 sidor SPANINGSUPPSLAG av den ena varianten och 2 av den andra (t.ex. första sidan i `1005 — Tips från polisen i Jönköping…` och `10 — Alla förhör med AA…`), 3 sidor löpande förhörsprotokoll, 1 förhörssida med `F:`/`S:`-dialog, 1 sida med `[MASKAD]`. Rendera dem till PNG i 300 dpi med `ocr_pages.render_pages` (scratch-katalog).
- [x] **Steg 2:** Kör infoscrys oförändrade worker: `~/.local/share/infoscry/surya-venv/bin/python /Users/patrik/projects/infoscry/scripts/ocr/surya_worker.py` med en begäran per sida (`{"protocol":2,"type":"page","unitId":"x","ordinal":0,"page":N,"imagePath":"<abs>"}`) och spara svaren. Förväntat: `status: "read"` och block med `label`.
- [x] **Steg 3:** Dokumentera i *Spik*: vilka etiketter blanketterna får (`Form`, `Table`, `Text`?), hur fältnummer, etikett och värde fördelas över celler, hur kryssrutor skrivs, om handskrift läses, om förhörsrepliker kommer som egna block eller rader med `<br>`, om avstavning finns kvar, och sekunder per sida. Spara 5 representativa svar som fixtures (bara offentliga arkivsidor; hela dokumenttexter loggas inte).
- [x] **Steg 4:** Stäm av mot planen. Om Surya inte delar upp blanketten i fält, eller ger tomma svar för blanketter, ska det tas upp med användaren innan Task 2b påbörjas, eftersom det ändrar 2b:s angreppssätt.

### Task 1: Fristående Surya-worker

**Files:**
- Create: `src/surya_worker.py` (körs med worker-venv:ens Python; importerar **inga** projektmoduler)
- Modify: `pyproject.toml` (`py-modules`)
- Test: `tests/test_surya_worker.py`

**Interfaces:**
- Produces: protokoll version 1. Begäran: `{"protocol":1,"type":"page","pdf_stem":str,"page":int,"image_path":abs-str}`. Svar: `{"protocol":1,"type":"page","pdf_stem","page","status":"read"|"empty"|"error","model":str,"blocks":[{"html":str,"label":str|None,"reading_order":int|None,"bbox":[x0,y0,x1,y1],"confidence":float|None}]}` eller vid fel `{"status":"error","code":str,"message":str}`. Felkoder: `NEEDS_SURYA`, `NEEDS_LLAMA_CPP`, `NEEDS_SURYA_MODEL`, `SURYA_START_FAILED`, `UNREADABLE_IMAGE`, `PAGE_TOO_LARGE`, `MALFORMED_REQUEST`, `INFERENCE_FAILED`, `OUTPUT_TOO_LARGE`. `--identity` skriver `{"type":"identity","status":"ok","identity":str}` utan att starta modell.
- Gränser: `MAX_REQUEST_BYTES = 64*1024`, `MAX_RESULT_BYTES = 4*1024*1024`, `MAX_PAGE_CHARS = 200_000` (HTML räknat), `MAX_BLOCKS = 2_000`, `MAX_PAGE_PIXELS = 16_777_216`.

Portera infoscrys worker (samma struktur: `ProtocolReader`, `Engine` med lat start och `stop()`, `runtime_identity`, `runtime_failure_code`, `BoundedLine`, SIGTERM→`SystemExit`). Skillnad: ingen `html_to_text` — workern skickar bara rå HTML; status `empty` när ingen blocktext finns efter att taggar strippats.

- [ ] **Steg 1: Skriv failande tester** med en falsk `Engine` vars `predictor()` returnerar objekt med `.blocks` (attribut `html`, `label`, `reading_order`, `polygon`, `confidence`):
  - `test_read_page_returns_raw_blocks` — två block, svaret har `status == "read"`, `blocks[0]["html"] == "<h2>PM</h2>"`, `bbox == [10, 20, 110, 40]` ur polygonen.
  - `test_blank_blocks_give_empty` — block med `html="<p> </p>"` → `status == "empty"`.
  - `test_relative_image_path_rejected` → `code == "UNREADABLE_IMAGE"`.
  - `test_oversize_raster_rejected` — PNG 5000×4000 → `PAGE_TOO_LARGE`.
  - `test_too_many_blocks` — 2 001 block → `OUTPUT_TOO_LARGE`.
  - `test_wrong_protocol` → `MALFORMED_REQUEST`.
  - `test_import_error_maps_to_needs_surya` — `runtime_failure_code(ImportError()) == "NEEDS_SURYA"`.
  - `test_main_exits_on_stdin_close` — kör `main()` med tom stdin, returnerar 0 och anropar `engine.stop()`.
- [ ] **Steg 2:** `.venv/bin/pytest tests/test_surya_worker.py -v` → FAIL (modul saknas).
- [ ] **Steg 3:** Implementera `src/surya_worker.py`; `Engine.predictor()` gör `get_default_manager().start()` + `RecognitionPredictor(manager)` och sidan läses med `predictor([image], full_page=True)[0]`. Lägg `surya_worker` i `py-modules`.
- [ ] **Steg 4:** `.venv/bin/pytest tests/test_surya_worker.py tests/test_packaging.py -v` → PASS.
- [ ] **Steg 5:** `.venv/bin/python scripts/test.py --static` → grönt (mypy: Surya-importerna är dynamiska och lokala, som i infoscry).

### Task 2: Ren layoutmodul — HTML-block → struktur, text och Markdown

**Files:**
- Create: `src/surya_layout.py`
- Modify: `pyproject.toml` (`py-modules`)
- Test: `tests/test_surya_layout.py`, fixtures i `tests/fixtures/surya/*.json` (riktiga workersvar från Task 0)

**Interfaces:**
- Produces:
  - `class BlockKind(StrEnum)`: `HEADING, PARAGRAPH, LIST_ITEM, TABLE, FORMULA, CODE, CAPTION, FOOTNOTE, PAGE_HEADER, PAGE_FOOTER, FIGURE, FORM`
  - `@dataclass(frozen=True) class LayoutBlock`: `kind: BlockKind`, `text: str`, `order: int`, `bbox: tuple[int,int,int,int] | None`, `level: int | None = None` (bara HEADING, 1–6), `rows: tuple[tuple[Cell, ...], ...] | None = None` (bara TABLE och FORM), `latex: str | None = None`, `fields: tuple[FormField, ...] | None = None` (bara FORM, fylls i Task 2b)
  - `@dataclass(frozen=True) class Cell`: `text: str`, `header: bool`, `row_span: int = 1`, `col_span: int = 1`
  - `parse_blocks(raw: list[dict]) -> tuple[list[LayoutBlock], bool]` — andra värdet är `degraded`
  - `page_text(blocks: list[LayoutBlock]) -> str`
  - `page_markdown(blocks: list[LayoutBlock]) -> str`
  - `blocks_to_json(blocks) -> str` / `blocks_from_json(s: str) -> list[LayoutBlock]`
  - `with_redactions(blocks, redactions: list[tuple[int,int]]) -> list[LayoutBlock]` — infogar `PARAGRAPH`-block med texten `[MASKAD]` sorterat på y-mitt, numrerar om `order`.

Etikettmappningen är infoscrys (`SuryaStructureParser.kindOf`): `SectionHeader→HEADING` (nivå ur `<hN>`, standard 2), `Text`/`Bibliography→PARAGRAPH`, `ListGroup→` ett `LIST_ITEM` per `<li>`, `Table`/`TableOfContents→TABLE`, `Form→FORM` (tolkas i Task 2b; i Task 2 behåller `FORM` bara sina rader som en tabell), `Equation→FORMULA`, `Code→CODE`, `Caption→CAPTION`, `Footnote→FOOTNOTE`, `PageHeader→PAGE_HEADER`, `PageFooter→PAGE_FOOTER`, `Picture`/`Figure`/`Diagram`/`ChemicalBlock→FIGURE` (bara med text), övrigt → `PARAGRAPH`. Sortering på `reading_order`, stabil vid lika/saknad. Bara ett `Table`-block som inte går att tolka som tabell ger `degraded=True` (blir `PARAGRAPH`). Spann klampas till 1–50.

Markdown-regler: rubrik `#`×nivå; listpunkt `- `; tabell som pipe-tabell med första raden som rubrikrad (finns ingen `th`-rad blir första raden rubrik), `|` i cell → `\|`, radbrytning i cell → mellanslag, `rowspan`/`colspan` → cellen upprepas tom i de spannade positionerna; formel `$$…$$`; kod i ```-staket; bildtext/fotnot som vanligt stycke med prefix `*Bildtext:*`/`*Fotnot:*`; block skilda med en tomrad; `PAGE_HEADER`/`PAGE_FOOTER` utelämnas. Ett stycke vars rad börjar med `#`, `- `, `* `, `+ ` eller `\d+\. ` får ett `\` framför.

- [ ] **Steg 1: Skriv failande tester:**
  - `test_label_mapping` — parametriserad över alla etiketter ovan → förväntad `kind`.
  - `test_sorted_by_reading_order_stable` — indata `B(2), C(None), D(None), A(1)` → texter `["A", "B", "C", "D"]` (block utan ordning sorteras sist och behåller inbördes mottagen ordning).
  - `test_table_with_spans` — `<table><tr><th>Namn</th><th>Tid</th></tr><tr><td colspan="2">Okänd</td></tr></table>` → `rows[1][0].col_span == 2`; Markdown `| Namn | Tid |\n| --- | --- |\n| Okänd |  |`.
  - `test_malformed_table_degrades` — `Table` med `<p>bara text</p>` → `PARAGRAPH`, `degraded is True`; samma för `Form` → `FORM`-block med texten kvar och `degraded is False`.
  - `test_list_group_splits_items` — `<ul><li>a</li><li>b</li></ul>` → två `LIST_ITEM`.
  - `test_page_text_uses_tabs_and_keeps_headers` — `page_text` innehåller `"Namn\tTid"` och sidhuvudets text.
  - `test_markdown_omits_page_header_footer`.
  - `test_markdown_escapes_content` — stycke `"# 12 i ordningen"` → `"\\# 12 i ordningen"`; cell `"a|b"` → `"a\\|b"`.
  - `test_with_redactions_orders_by_y` — block med bbox y 100–120 och 300–320, maskering (200, 210) → `[MASKAD]` hamnar mellan, `order` 0,1,2.
  - `test_json_roundtrip`.
- [ ] **Steg 2:** `.venv/bin/pytest tests/test_surya_layout.py -v` → FAIL.
- [ ] **Steg 3:** Implementera med `html.parser.HTMLParser` och en tillåtlista (`table, thead, tbody, tr, th, td, h1–h6, p, ul, ol, li, br, math, code, pre`, inline-formatering); allt annat blir textinnehåll. Lägg modulen i `py-modules`.
- [ ] **Steg 4:** testerna + `tests/test_packaging.py` → PASS; `scripts/test.py --static` grönt.

### Task 2b: Formulär och förhör

**Files:**
- Create: `src/form_layout.py`, `data/formular/spaningsuppslag.json`
- Modify: `src/surya_layout.py` (anropar `form_layout` för `FORM`-block och förhörsstycken), `pyproject.toml` (`py-modules`)
- Test: `tests/test_form_layout.py` (fixtures från Task 0)

**Interfaces:**
- Consumes: `LayoutBlock`, `Cell`, `BlockKind` (Task 2).
- Produces:
  - `@dataclass(frozen=True) class FormField`: `number: str | None` (`"03"`), `label: str`, `value: str`, `checked: bool | None` (bara kryssrutor)
  - `@dataclass(frozen=True) class FormTemplate`: `name: str`, `match: tuple[str, ...]` (rubrikord som känner igen varianten), `labels: dict[str, str]` (fältnummer → kanonisk etikett)
  - `load_templates(path: Path = ROOT / "data" / "formular") -> list[FormTemplate]`
  - `extract_form_fields(blocks: list[LayoutBlock], templates: list[FormTemplate]) -> tuple[list[LayoutBlock], list[FormField]]` — tar ut etikett- och värdeblock ur sidans block (oavsett `kind`) och returnerar övriga block samt fälten; ett `FORM`-block med fälten placeras där det första etikettblocket stod. Inga etiketter → blocken returneras oförändrade.
  - `filled_dark_boxes(image, bbox: tuple[int,int,int,int]) -> int` — antal fyllda mörka rektanglar (höjd ≥ 60 % av radhöjden, fyllnadsgrad ≥ 0,8) inom bbox.
  - `resolve_checkbox_masks(blocks, image) -> list[LayoutBlock]` — byter `☐` mot `[MASKAD]` i block utan fältnummer enligt beslut 5.
  - `dehyphenate(text: str) -> str` — `-\n` följt av liten bokstav slås ihop.
  - `split_turns(text: str) -> list[tuple[str | None, str]]` — `(talarmarkör, repliktext)`; markören är `^[A-ZÅÄÖ]{1,3}:` (t.ex. `F`, `M`, `FL`), `Fråga:`/`Svar:`, namn i versaler (≤ 30 tecken) följt av `:`, eller `[MASKAD]` först i blocket (maskerad talare)
  - `interrogation_header_fields(text: str) -> list[FormField]` — `Förhörsledare`, `Förhörsvittne`, `Förhörsperson`, `Protokoll fört vid förhör med`, `Förhöret hållet` m.fl.
- Markdown (i `page_markdown`): `FORM` → fältlista `- **{number} {label}:** {value}`, kryssrutor `- [x] per telefon`; replik → eget stycke `**F:** …`; metadatafält → fältlista. I `page_text`: `"{number} {label}: {value}"` per rad, så fuzzy-sökning efter t.ex. ”Uppgiftslämnare” fungerar.

`data/formular/spaningsuppslag.json` innehåller de varianter som spiken visade, med etiketterna så som de är tryckta på blanketten (kontrollera mot sidbilden, inte mot Tesseract-texten).

- [ ] **Steg 1: Failande tester:**
  - `test_parse_spaningsuppslag_fields` — `tests/fixtures/surya/form-v1-a.json` → `03 Uppgiftsmottagare: Insp NEK Persson`, `05 datum: 860316`, `06 klockan: 09.10`, `08 Löpande nr: 2449`, `11 per telefon` ikryssad, `11 per brev` inte; `form-v2-b.json` (etikett och värde i samma `<br/>`-block) → `01 Polisdistrikt: Stockholm`; `form-v2-a.json` (fält som `PageHeader`) → `03 Upprättat(d) av: Krinsp Sören Morberg`.
  - `test_empty_header_fields_omitted` — `forhor-dialog-a.json` (tomt blanketthuvud på sida 2) → inga fältrader i Markdown, men etiketterna finns kvar i `page_text`.
  - `test_checkbox_mask_resolved` — `forhor-dialog-b.json` + en syntetisk bild med en fylld svart rektangel i blockets bbox → `F: Ja, [MASKAD], klockan …`; samma block med en ihålig ram → kryssrutan står kvar.
  - `test_redacted_becomes_maskad` — `[REDACTED]` → `[MASKAD]`.
  - `test_dehyphenate` — `"polis-\ninspektören"` → `"polisinspektören"`, `"Palme-\nUtredningen"` lämnas.
  - `test_template_fixes_ocr_label` — etikett `"01 Poliodistrikt (där uppslaget mottagits)"` med mallen → `label == "Polisdistrikt (där uppslaget mottagits)"`.
  - `test_unknown_variant_uses_raw_labels` — ingen mall matchar → råa etiketter, `number` kvar.
  - `test_checkboxes` — `☒ per telefon` → `checked is True`, `☐ per brev` → `False`.
  - `test_unparseable_form_keeps_all_text` — block utan fältnummer → oförändrade; varje icke-blankt tecken i indata finns i `page_text`.
  - `test_split_turns` — `"F: Var stod du?\nM: Vid Dekorima.\n[MASKAD] Ja."` → `[("F", "Var stod du?"), ("M", "Vid Dekorima."), ("[MASKAD]", "Ja.")]`; vanlig mening med kolon mitt i raden delas inte.
  - `test_interrogation_header_fields` — `"Förhörsledare: krinsp Sören Morberg."` → `FormField(None, "Förhörsledare", "krinsp Sören Morberg.", None)`.
  - `test_markdown_turns_and_fields` — `page_markdown` ger `**F:** Var stod du?` och `- **03 Uppgiftsmottagare:** …`.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera. **Steg 4:** PASS + `--static` grönt.

### Task 3: Klient som äger worker-processen

**Files:**
- Create: `src/surya_client.py`
- Modify: `pyproject.toml` (`py-modules`)
- Test: `tests/test_surya_client.py` (falsk worker = litet Python-skript i `tmp_path` som körs med `sys.executable`)

**Interfaces:**
- Consumes: protokollet från Task 1.
- Produces:
  - `DEFAULT_SURYA_PYTHON = Path.home() / ".local/share/palmemordsarkivet/surya-venv/bin/python"`; `surya_python() -> Path` läser `PALME_SURYA_PYTHON`.
  - `class SuryaUnavailable(RuntimeError)` med attribut `code: str`.
  - `class SuryaPageError(RuntimeError)` med `code: str` (sidans eget fel, filen kan fortsätta).
  - `@dataclass(frozen=True) class SuryaPage`: `status: str`, `blocks: list[dict]`, `model: str`
  - `class SuryaWorker` (kontexthanterare): `__init__(python: Path | None = None, worker: Path | None = None, page_timeout: float = 300.0)`, `read_page(pdf_stem: str, page: int, image_path: Path) -> SuryaPage`, `identity() -> str | None`, `close() -> None`.
  - `surya_layout_available() -> bool` — interpreter finns och `--identity` svarar `ok` inom 10 s.

Startar workern med `start_new_session=True`; `close()` stänger stdin, väntar 10 s, sedan `os.killpg` SIGTERM → SIGKILL efter 5 s (samma mönster som jobbinvarianterna). Svarsrader läses med gräns `MAX_RESULT_BYTES`. Felkoderna `NEEDS_*`/`SURYA_START_FAILED` → `SuryaUnavailable`; övriga → `SuryaPageError`. Timeout eller död process → `close()` och `SuryaUnavailable("WORKER_DIED")`.

- [ ] **Steg 1: Skriv failande tester:**
  - `test_read_page_roundtrip` — falsk worker ekar ett block → `SuryaPage.status == "read"`.
  - `test_runtime_error_raises_unavailable` — falsk worker svarar `NEEDS_LLAMA_CPP` → `SuryaUnavailable`, `code == "NEEDS_LLAMA_CPP"`.
  - `test_page_error_is_recoverable` — `INFERENCE_FAILED` → `SuryaPageError`, nästa `read_page` fungerar.
  - `test_page_timeout` — falsk worker sover → `SuryaUnavailable("WORKER_DIED")` inom `page_timeout=1`.
  - `test_worker_crash_kills_group` — falsk worker startar ett barn (`sleep 60`) och dör; efter `close()` lever varken worker eller barn (`os.killpg(pgid, 0)` → `ProcessLookupError`).
  - `test_env_override` — `PALME_SURYA_PYTHON` styr `surya_python()`.
- [ ] **Steg 2:** kör → FAIL. **Steg 3:** implementera. **Steg 4:** PASS + `--static` grönt.

### Task 4: Databas — Markdown och layout per sida

**Files:**
- Modify: `src/db/_schema.py` (`SCHEMA_VERSION = 11`, `_migration_011_page_layout`, `pdf_pages` i färskt schema), `src/db/pipeline.py`, `src/db/__init__.py`
- Test: `tests/test_db.py` (+ befintliga migrationsfixtures)

**Interfaces:**
- Produces:
  - Nya kolumner i `pdf_pages`: `markdown TEXT`, `layout_json TEXT`, `engine_identity TEXT`.
  - `record_page(conn, *, pdf_stem, page_num, engine, text, score, markdown: str | None = None, layout_json: str | None = None, engine_identity: str | None = None) -> None` — UPSERT skriver alltid alla tre (en omkörning med Tesseract nollar alltså Markdown).
  - `get_page_markdown_map(conn, pdf_stem: str) -> dict[int, tuple[str, str]]` — `page_num → (text, markdown)` för sidor där `markdown IS NOT NULL`.

- [ ] **Steg 1: Failande tester:** `test_migration_011_adds_layout_columns` (gammal fixture → kolumnerna finns, `user_version == 11`); `test_record_page_stores_markdown`; `test_rerecord_without_markdown_clears_it`; `test_get_page_markdown_map`.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera. **Steg 4:** `tests/test_db.py tests/test_sql_boundary.py tests/test_db_package.py` PASS, `--static` grönt.

### Task 5: Motorn `surya-layout` i `ocr_pages` och OCR-operationen

**Files:**
- Modify: `src/ocr_pages.py`, `src/operations/ocr.py`, `src/operations/registry.py`, `src/admin_ui.py` (`OPERATION_HELP`, `FORM_LAYOUT`)
- Test: `tests/test_ocr_operation.py`, ny `tests/test_ocr_pages_layout.py`, `tests/test_admin_ui.py`, `tests/test_operation_registry.py`

**Interfaces:**
- Consumes: `SuryaWorker`, `SuryaUnavailable`, `SuryaPageError` (Task 3); `parse_page(raw, image=, page_height=, redactions=, trace=)` (Task 2/2b — **inte** `parse_blocks` + `with_redactions` direkt: `parse_page` kör blankett-/förhörstolkningen och lägger in pixelmaskeringarna efteråt), `page_text`, `page_markdown`, `blocks_to_json`. `page_height` ska vara sidbildens höjd, och `trace` loggas på debugnivå (innehåller ingen dokumenttext) (Task 2); `record_page(..., markdown=, layout_json=, engine_identity=)` (Task 4).
- Produces:
  - `ocr_pages.ocr_surya_layout(worker: SuryaWorker, image, png_path: Path, pdf_stem: str, page: int, redactions: list[tuple[int,int]]) -> tuple[str, str | None, str, list[dict]]` — `(text, markdown, layout_json, pdf_lines)`; `markdown` är `None` för tom sida; `pdf_lines` är `{"text", "bbox"}` per block för `update_pdf_text_layer`.
  - `ocr_pages`-CLI: `--engine` får valet `surya-layout`.
  - Registry: OCR-operationen får `_p("surya_engine", "--surya-engine", "choice", "lines", "Surya-motor", choices=("lines", "layout"))`; `ocr-pages` får valet `surya-layout` i `engine`. `OcrOptions.surya_engine: str = "lines"`.

Beteende: workern öppnas en gång per `run_ocr_pages`-anrop (`with SuryaWorker() as worker`), sidbilden sparas som PNG (som Tesseract-grenen) och skickas som absolut sökväg. `SuryaPageError` → sidan loggas med `ctx.log(..., level="error")` + `log_error("ocr_pages", f"{pdf.name}#p{n}", code)` och hoppas. `SuryaUnavailable` → loggas, resten av filen avbryts, returkod 1 (så `run_surya_fallback` markerar `surya_failed`). `degraded` loggas på varning. Engine-värdet i `pdf_pages` blir `surya-layout`. I `operations/ocr.py`: `run_surya_fallback` och `_run_redo` skickar `--engine surya-layout` när `options.surya_engine == "layout"`; tillgänglighet kontrolleras med `surya_layout_available()` i stället för `_surya_available()` för det valet, och en saknad runtime loggas med orsak (inte tyst hoppas).

- [ ] **Steg 1: Failande tester** (falsk `SuryaWorker` injiceras via parameter `surya_worker=` till `run_ocr_pages`, standard `None` = skapa riktig):
  - `test_layout_page_records_text_and_markdown` — `pdf_pages`-raden har `engine == "surya-layout"`, `markdown` med `| Namn | Tid |`, `text` med `Namn\tTid`.
  - `test_empty_page_recorded_without_markdown` — `status "empty"` → `text == ""`, `markdown is None`.
  - `test_redaction_inserted_between_blocks`.
  - `test_page_error_skips_page_and_continues`.
  - `test_unavailable_aborts_file_with_rc1`.
  - `test_redo_uses_layout_engine_when_selected` (i `test_ocr_operation.py`, inspekterar argv till `run_process`).
  - Befintliga `test_help_explains_every_field_in_every_operation` m.fl. ska fortsatt gå igenom med den nya parametern.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera; lägg Admin-hjälppunkten: *Surya-motor — `lines` är den gamla radläsningen, `layout` läser rubriker, tabeller och sidhuvud och sparar även Markdown (kräver separat Surya 2-installation).* **Steg 4:** alla nämnda testfiler PASS, `--static` grönt.

### Task 6: Markdown i ingest — strukturmedveten chunkning

**Files:**
- Modify: `src/normalize_text.py` (`normalize_markdown`), `src/rag/ingest.py` (`_chunka_fil`, ny `chunk_markdown`)
- Test: `tests/test_chunk.py`, `tests/test_normalize_text.py`, `tests/test_reingest.py`

**Interfaces:**
- Consumes: `get_page_markdown_map` (Task 4).
- Produces:
  - `normalize_text.normalize_markdown(md: str) -> str` — bara ligaturer, NFC, CRLF, styrtecken och `\n{3,}→\n\n`; inga rader tas bort och inget indrag strippas.
  - `ingest.chunk_markdown(md: str, size: int, overlap: int) -> list[tuple[int, int, str]]` — delar vid tomrader (blockgränser); ett block större än `size` delas med `chunk_text`, utom tabeller som delas mellan rader och upprepar rubrik- och separatorraden i varje fortsättningschunk; ingen överlapp inuti tabeller. Ett `FORM`-block hålls ihop om det ryms, annars delas det mellan fält. I förhör föredras gränser före en ny replik (`**F:**`/`**S:**`), och en fråga hålls ihop med sitt svar när paret ryms. En inledande rad `> Sidhuvud: …` tas bort ur sidans flöde och läggs först i varje chunk.
  - `_chunka_fil(raw, filnamn, meta, disk_mtime, chunk_chars, chunk_overlap, page_markdown: dict[int, tuple[str, str]] | None = None)` — per sida: om `normalize(db_text) == sidan` används `chunk_markdown(normalize_markdown(md))`, annars `chunk_text` som i dag med en debugrad `"sida {n}: Markdown förkastad — texten ändrad efter OCR"`.

`is_useful` gäller fortfarande; tabellchunks räknas mot `MIN_ALNUM_RATIO` efter att `|`, `-` och mellanslag tagits bort, annars förkastas glesa tabeller. Ingen ändring av LanceDB-schemat. Om `rag/ask.py` eller någon källkortsvy visar chunktext med `st.markdown`, escapa `$` där (Granskningsfokus 5).

- [ ] **Steg 1: Failande tester:** `test_chunk_markdown_never_splits_table_row`, `test_chunk_markdown_repeats_table_header`, `test_chunk_markdown_splits_at_blank_lines`, `test_normalize_markdown_keeps_pipes_and_indent`, `test_page_header_prefixed_to_every_chunk`, `test_chunk_keeps_question_with_answer`, `test_form_block_split_between_fields`, `test_stale_markdown_ignored`, `test_fresh_markdown_used`, `test_sparse_table_chunk_is_useful`, samt `test_chunk_dollar_escaped_for_display` om en `st.markdown`-väg finns.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera. **Steg 4:** PASS + `--static` grönt. Kontrollera att `merge_pages.merge_one` skriver om `text/<stem>.txt` efter en layoutsida så att mtime-deltat i ingest plockar upp filen (befintligt beteende — lägg ett test i `tests/test_merge_pages.py` om det saknas).

### Task 7: Installation och dokumentation

**Files:**
- Modify: `scripts/install.py` (flagga `--surya-layout`), `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`
- Test: `tests/test_scripts.py` (oförändrad mängd entrypoints — `install.py` är redan undantagen; verifiera)

`--surya-layout` kör: `brew install llama.cpp uv` (om saknas), `uv venv --python 3.12 ~/.local/share/palmemordsarkivet/surya-venv`, `uv pip install --python <venv>/bin/python 'surya-ocr==0.22.1'`, och sist `<venv>/bin/python src/surya_worker.py --identity` som kontroll. Dokumentera `PALME_SURYA_PYTHON` (inkl. att infoscrys venv kan återanvändas), `LLAMA_CPP_BINARY`, viktlicensen (datalab-to/surya-ocr-2-gguf — kontrollera modellkortet), motorvalet, färskhetsregeln och att `PAGE_HEADER`/`PAGE_FOOTER` saknas i Markdown/embeddings men finns i text. AGENTS.md: katalogträdet (`surya_worker.py`, `surya_layout.py`, `surya_client.py`), en punkt under *Non-obvious Design Decisions* om varför workern har egen venv och varför `text` förblir ren text.

- [ ] **Steg 1:** implementera flaggan + dokumentation. **Steg 2:** `.venv/bin/python scripts/install.py --help`-utskriften visar flaggan; `scripts/test.py --static` grönt.

### Task 8: Läsningshistorik och sidvy-logik (Streamlit-fri)

**Files:**
- Modify: `src/db/_schema.py` (`SCHEMA_VERSION = 12`, `_migration_012_page_readings`), `src/db/pipeline.py`, `src/db/__init__.py`
- Create: `src/page_review.py`
- Modify: `pyproject.toml` (`py-modules`)
- Test: `tests/test_db.py`, `tests/test_page_review.py`

**Interfaces:**
- Consumes: `get_pages_for_stem`, `record_page` (Task 4), `blocks_from_json` (Task 2), `normalize_text.normalize`.
- Produces:
  - Tabell `pdf_page_readings(id INTEGER PRIMARY KEY, pdf_stem TEXT NOT NULL, page_num INTEGER NOT NULL, engine TEXT NOT NULL, text TEXT, markdown TEXT, layout_json TEXT, engine_identity TEXT, score REAL, recorded_at TEXT NOT NULL, reason TEXT NOT NULL)` med index på `(pdf_stem, page_num)`; `reason` ∈ `{"rescan", "restore"}`.
  - `archive_page_reading(conn, *, pdf_stem, page_num, engine, text, markdown, layout_json, engine_identity, score, reason) -> int`
  - `list_page_readings(conn, pdf_stem: str, page_num: int) -> list[sqlite3.Row]` (nyast först)
  - `get_page_reading(conn, reading_id: int) -> sqlite3.Row | None`
  - `get_page_row(conn, pdf_stem: str, page_num: int) -> sqlite3.Row | None`
  - `list_documents_for_review(conn) -> list[sqlite3.Row]` — `pdf_stem`, `source`, `pdf_path` ur `pdf_files`, sorterat på stem.
  - I `src/page_review.py`:
    - `@dataclass(frozen=True) class PageView`: `pdf_stem`, `page_num`, `page_count: int`, `current_text: str | None` (ur textfilen), `engine: str` (`"hel-fils-OCR"` utan `pdf_pages`-rad), `score: float | None`, `processed_at: str | None`, `engine_identity: str | None`, `markdown: str | None`, `blocks: list[LayoutBlock]`, `stale: bool`, `readings: list[dict]`
    - `load_page_view(conn, root: Path, pdf_stem: str, page_num: int) -> PageView`
    - `page_count(pdf_path: Path) -> int` (pypdfium2)
    - `render_page_image(pdf_path: Path, page_num: int, dpi: int = 150) -> Image`
    - `draw_layout_overlay(image, blocks: list[LayoutBlock], ocr_dpi: int, view_dpi: int) -> Image` — ramar per block, färg per `BlockKind`, bbox skalas `view_dpi / ocr_dpi`.
    - `escape_markdown_dollars(md: str) -> str` — `$` → `\$`.

- [ ] **Steg 1: Failande tester:** `test_migration_012_creates_readings`; `test_archive_and_list_readings_newest_first`; `test_load_page_view_without_pdf_pages_row` (motor `"hel-fils-OCR"`, text ur filen); `test_load_page_view_with_layout_row` (block och Markdown med); `test_stale_when_file_text_differs`; `test_overlay_scales_bbox` (bbox (100,100,200,200) vid OCR-dpi 300, vy 150 → ritad ram vid (50,50,100,100), kontrolleras med pixelvärde); `test_escape_markdown_dollars`.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera. **Steg 4:** PASS + `tests/test_sql_boundary.py`, `tests/test_packaging.py`, `--static` grönt.

### Task 9: Operationen `rescan-page` (omskanning och återställning)

**Files:**
- Create: `src/operations/rescan.py`, `scripts/rescan_page.py`, `rescan_page.sh`
- Modify: `src/ocr_pages.py` (`--force`), `src/operations/registry.py`, `src/operations/adapters.py`, `src/admin_ui.py` (`OPERATION_HELP`, `FORM_LAYOUT`, `FILE_PICKERS` för `pdf_stem` med ny källa `state:pdf_files`, eftersom alla dokument ska kunna väljas, inte bara de med per-sida-rader)
- Test: `tests/test_rescan_operation.py`, `tests/test_scripts.py`, `tests/test_admin_ui.py`, `tests/test_operation_registry.py`

**Interfaces:**
- Consumes: Task 8:s db-funktioner, `run_ocr_pages` (Task 5), `merge_pages.merge_one`, `normalize_text.process_file`, `state_db.mark_normalized`, `ingest.run_ingest`.
- Produces:
  - `ocr_pages`-flaggan `--force` (bool): sidor i `--pages` OCR:as även om `page_exists`.
  - Registerpost `id="rescan-page"`, `label="Skanna om sida"`, `group="OCR"`, `mutating=True`, parametrar: `pdf_stem` (`str`, required), `page` (`int`, required), `engine` (`choice`, `("surya-layout", "surya", "tesseract", "vision")`, standard `"surya-layout"`), `restore_reading` (`int`, `None` — sätts bara av sidgranskningen; döljs i Admin-formuläret och dokumenteras som CLI-flagga), `dpi` (`int`, 300), `reindex` (`bool`, `True`).
  - `run_rescan_page(*, root: Path, pdf_stem: str, page: int, engine: str, dpi: int, reindex: bool, restore_reading: int | None, context) -> int`

Flöde: hitta PDF (`downloaded/files` sedan `downloaded/wpu_files`; saknas → `OperationFailed`) → kontrollera `1 ≤ page ≤ page_count` (annars `OperationFailed`) → arkivera gällande läsning (`pdf_pages`-raden, annars filens sidtext med `engine="hel-fils-OCR"`) med `reason` → antingen `run_ocr_pages(inp=pdf, pages=str(page), engine=engine, force=True, …)` eller, vid `restore_reading`, `record_page` med den historiska radens fält → `merge_one(create_missing=True)` → `process_file` → `mark_normalized` → om `reindex`: `run_ingest(rebuild=False, …)` med registrets standardvärden. Varje steg loggas på debug (`"rescan {stem} s{page}: arkiverade läsning {id} ({engine}, poäng {score})"`). Misslyckas OCR:en lämnas den gällande läsningen orörd och jobbet slutar `failed`; historikraden står kvar och är harmlös.

- [ ] **Steg 1: Failande tester** (falsk `run_ocr_pages` och `run_ingest` monkeypatchas):
  - `test_rescan_archives_then_records` — före: `pdf_pages`-rad med motor `tesseract`; efter: historikrad `reason="rescan"` med gamla texten, ny rad med motor `surya-layout`, textfilens sida uppdaterad.
  - `test_rescan_archives_file_text_when_no_row` — historikraden har `engine == "hel-fils-OCR"`.
  - `test_rescan_rejects_page_out_of_range` — sida 99 i 3-sidig PDF → `OperationFailed`, ingen historikrad, ingen OCR.
  - `test_rescan_failure_keeps_current_reading`.
  - `test_restore_reading_becomes_current` — återställer läsning X; ny historikrad `reason="restore"` med den läsning som ersattes.
  - `test_reindex_flag_runs_incremental_ingest` / `test_no_reindex_skips_ingest`.
  - `test_ocr_pages_force_reocrs_existing_page`.
  - Befintliga `tests/test_scripts.py` (registry = entrypoints = genvägar) och Admin-hjälptesterna ska passera med den nya operationen.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera; Admin-hjälp: förklaring *”Skannar om en enda sida och sparar den tidigare läsningen så att den kan återställas från Sidgranskning.”* plus en punkt per synligt fält. **Steg 4:** PASS + `--static` grönt.

### Task 10: Streamlit-sidan Sidgranskning

**Files:**
- Create: `src/pages/9_Sidgranskning.py`
- Modify: `src/casebook_ui.py` endast om en hjälpare behövs för jobbstatus (annars inget)
- Test: `tests/test_sidgranskning_ui.py` (AppTest headless, som `tests/test_utredning_ui.py`)

**Interfaces:**
- Consumes: `load_page_view`, `render_page_image`, `draw_layout_overlay`, `escape_markdown_dollars`, `list_documents_for_review`, `list_page_readings` (Task 8); `job_service.start_job("rescan-page", …)`, `reconcile_active_job()`.
- Produces: query-parametrarna `doc=<pdf_stem>&page=<n>` öppnar sidan direkt (för framtida länkar från källkort och Maskeringar).

Layout: `casebook_ui.render_page_header("Sidgranskning", …)`. Överst dokumentval (`st.selectbox` med sökbar lista över stems) och sidnummer (`st.number_input` 1–`page_count`, plus Föregående/Nästa). Två kolumner: vänster sidbilden (`st.image`, cachad med `st.cache_data` på `(pdf_path, page, dpi, mtime)`) med kryssrutan **Visa layoutblock** (overlay); höger flikar **Text** (`st.code(current_text, language=None)` så att radbrytningar och `\t` syns exakt), **Markdown** (renderad via `escape_markdown_dollars`), **Markdown-källa** (`st.code(markdown, language="markdown")`) och **Layout** (tabell: ordning, typ, nivå, bbox, textutdrag ≤ 80 tecken) och **Formulärfält** (fältnummer, etikett, värde, kryssruta — bara när sidan har `FORM`-block eller förhörsmetadata). Under flikarna: motor, poäng, tidpunkt, motoridentitet; varning när `stale` (”Texten har ändrats efter OCR — Markdown används inte i sökindexet”). Flikarna Markdown/Layout visas bara när läsningen har sådana.

Omskanning i en `st.container(border=True)`: motorval (samma choices som operationen), kryssrutan **Uppdatera sökindexet** och knappen **Skanna om sidan**. Knappen är inaktiverad med förklaring när `reconcile_active_job()` returnerar ett aktivt jobb; under ett pågående `rescan-page`-jobb för just den här sidan visas status och loggsvans (`read_log_tail`) och sidan ritas om med `st.fragment(run_every=2)` tills jobbet är klart. **Historik**: hopfällbar lista över tidigare läsningar (motor, poäng, tid, orsak) med **Visa** (texten i en `st.code`) och **Återställ** (startar `rescan-page` med `restore_reading`).

- [ ] **Steg 1: Failande AppTest-tester** (temporär state.db + en 2-sidig test-PDF skapad med pymupdf i `tmp_path`; `start_job` monkeypatchas):
  - `test_shows_page_text_from_file`.
  - `test_query_params_select_document_and_page`.
  - `test_markdown_tab_only_with_layout_reading`.
  - `test_form_fields_tab_lists_fields`.
  - `test_markdown_dollars_escaped` — Markdown `| Belopp | 5 $ |` renderas med `\$`.
  - `test_stale_warning_shown`.
  - `test_rescan_button_starts_job_with_params` — `start_job` anropad med `("rescan-page", {"pdf_stem": …, "page": 2, "engine": "surya-layout", "reindex": True, …})`.
  - `test_rescan_button_disabled_while_job_active`.
  - `test_restore_starts_job_with_reading_id`.
- [ ] **Steg 2:** FAIL. **Steg 3:** implementera. **Steg 4:** PASS + `--static` grönt.
- [ ] **Steg 5:** Kör `./web.sh`, öppna Sidgranskning i webbläsarpanelen, välj ett dokument med tabell, skanna om en sida med `surya-layout` och återställ den; kontrollera att textfilen och sökningen i Utredning speglar varje steg. Dokumentera sidan i `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md` och flikraden + katalogträdet i `AGENTS.md`.

### Task 11: Pilot mot riktiga arkivsidor (verklig Surya)

**Files:**
- Create: `tests/test_surya_real.py` (markerad `@pytest.mark.external`; registrera markören i `pyproject.toml` `[tool.pytest.ini_options]` och hoppa när `surya_layout_available()` är falsk), `scripts/`-fritt mätskript i scratch eller `docs/`-bilaga, `docs/surya-layout-pilot.md`
- Modify: `tests/fixtures/surya/` (ett par riktiga workersvar, utan känsligt innehåll — välj sidor som redan är offentliga i arkivet)

- [ ] **Steg 1:** `test_real_surya_reads_table_page` — renderar en egen tabellsida med PIL (redistribuerbar), kör `SuryaWorker`, kräver ett `TABLE`-block med rubrikceller. Kör: `PALME_SURYA_PYTHON=~/.local/share/infoscry/surya-venv/bin/python .venv/bin/pytest -m external tests/test_surya_real.py -v` → PASS.
- [ ] **Steg 2:** Mät på ett urval om 50 sidor (15 SPANINGSUPPSLAG av båda varianterna, 15 förhörssidor varav några med dialog, 10 med lägst Tesseract-poäng, 10 slumpade): kvalitetspoäng (`score_text` på `text`), teckenantal, andel sidor med `TABLE`/`HEADING`, `degraded`-andel, sekunder per sida och uppstartstid per fil — för `lines` mot `layout`. Kontrollera dessutom för hand mot sidbilden: på 5 blanketter andelen ifyllda fält där nummer och värde hamnat ihop, och om handskrivna värden lästs; på 5 förhörssidor att `Förhörsledare`/`Förhörsvittne` och varje replik finns kvar och i rätt ordning.
- [ ] **Steg 3:** Skriv `docs/surya-layout-pilot.md` (samma upplägg som `docs/jev-reranker-pilot.md`): metod, siffror, begränsningar, rekommendation. **Acceptanskriterium för att byta standard:** medianpoäng för layout ≥ lines på de 20 sämsta sidorna, ingen sida som lines läste går tom med layout, sekunder per sida ≤ dagens Surya 0.17 (spiken: ~25 s/sida för Surya 2; 0.17 anges till 30–100 s), minst 80 % av de ifyllda blankettfälten rätt parade och inga tappade repliker på förhörssidorna. Uppdatera `data/formular/spaningsuppslag.json` med de varianter piloten hittar. Lägg rapporten i dokumentationslistan i `AGENTS.md`.

### Task 12: Byt standard (endast om piloten godkänns)

**Files:**
- Modify: `src/operations/registry.py` (`surya_engine` standard `"layout"`), `src/operations/ocr.py` (`OcrOptions.surya_engine`), `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`
- Test: `tests/test_operation_registry.py` (`test_surya_engine_defaults_to_layout`)

- [ ] **Steg 1:** failande test → **Steg 2:** ändra standard → **Steg 3:** `scripts/test.py --static` grönt. Befintliga sidor uppgraderas inte automatiskt; dokumentera kommandot för att köra om dåliga sidor: `.venv/bin/python scripts/ocr.py --redo-only --surya-engine layout`.

## Utanför planen

- Att mata blankettfälten (uppgiftslämnare, datum, klockan, *uppslaget avser*) in i facetterna eller kunskapsgrafen — fälten sparas strukturerat i `layout_json`, så det är en ren uppföljning.

- Delad worker över flera PDF:er / batchning av flera sidor per anrop (beslut efter pilotens uppstartsmätning).
- Surya `detection`-radboxar för exakt PDF-placering och träffmarkering.
- Rendering av Markdown/tabeller i Utrednings källkort och Utredningspärmen.
- Att låta vision/Tesseract fylla samma blockstruktur (infoscry gör det; här ger det inget förrän en konsument behöver det).
- Automatisk omkörning av redan OCR:ade sidor.
