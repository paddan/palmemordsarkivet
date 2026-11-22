# Utredningsläget som dialog: motfrågor och utredningsspår

Datum: 2026-11-22

## Syfte

Utredningsläget (MCP) i `src/Utredning.py` är i dag fråga → svar. Svaret stannar
vid frågan, och läget upplevs därför som en variant av Fråga arkivet (RAG) med
verktyg. Ägaren vill ha en dialog med en erfaren mordutredare som **tar initiativ**:
föreslår utredningsspår att gräva vidare i och ställer motfrågor tillbaka.

Spåren ska bygga på **hela konversationen** — allt som frågats, svarats och lästs
hittills — inte bara det senaste svaret. Utredaren ska se luckor och mönster som
bara syns när flera svar läggs ihop, och inte föreslå spår som redan grävts.

## Mål

- Efter varje svar visas 0–4 **utredningsspår**: kort rubrik, varför spåret är
  värt att gräva i, belägg (dokument + sida) som väckte det, och en färdig
  följdfråga.
- Varje spår har en knapp **Gräv** som skickar spårets följdfråga som nästa fråga
  i samma konversation och markerar spåret som grävt.
- Utredaren kan ställa **en motfråga** till operatören (vilken hypotes som prövas,
  vad som redan är uteslutet, vilken tidsperiod som avses) när frågan är öppen
  eller tvetydig. Den visas markerad under svaret; operatören svarar i den
  vanliga chattrutan.
- Sidan för en lista över alla spår i konversationen (föreslagna/grävda) och
  skickar en kort sammanfattning av den till modellen med varje ny fråga, så att
  spår inte upprepas och utredaren kan återknyta till öppna spår.
- Fungerar i båda backendvägarna: Claude (MCP-subprocess) och OpenAI-kompatibla
  (in-process).

## Icke-mål (kommer senare eller inte alls)

- En egen vy/panel över alla öppna spår i konversationen ("listan") — senare.
  Datat som byggs nu ska räcka för den.
- Att spara spåren i Utredningspärmen.
- A2UI eller annan JS-komponent. Streamlits egna widgetar används.
- Ändringar i Fråga arkivet (RAG-läget).

## Anti-mål (då har det misslyckats även om det fungerar)

- Spår utan belägg, eller spår som bygger på minneskunskap om fallet i stället
  för hämtat material. Ett spår är en lucka eller ett mönster *i arkivet*.
- Spår som gissar eller rekonstruerar innehåll bakom `[MASKAD]`.
- Motfrågor varje tur som tjat; de ska bara komma när svaret faktiskt beror på
  operatörens avsikt.
- Att utredarrollen sänker källkritiken: skillnaden mellan källuppgift och
  tolkning, beläggkraven och webbkällemärkningen står kvar oförändrade.
- Att svaret blir sämre eller kortare för att modellen lägger kraft på spåren.

## Begränsningar

- Verktygsmängden ska vara densamma i `rag/mcp_server.py` och
  `Utredning.OPENAI_TOOLS` (`test_openai_verktygsscheman_matchar_mcp_verktygen`).
- MCP-promptens verktygsreferens och `admin_ui.PROMPT_WRITING_HELP` /
  `PROMPT_MODE_HELP` ska stämma med verktygskontrakten.
- Egna promptar i `generated/prompts.json` ersätter standardprompten och nämner
  inte det nya verktyget. Verktygsbeskrivningen måste därför själv räcka för att
  modellen ska förstå när och hur det används.
- `st.chat_input` kan inte förifyllas eller skickas programmatiskt; ett klick på
  **Gräv** måste gå via `session_state` och en rerun.
- Streamlit kör om hela sidan per interaktion; spår och knappar måste ritas ur
  `chat_history`, inte bara under strömningen.
- Loggning enligt AGENTS.md: `web_log.get_logger` på sidan, debugrader om antal
  spår, ogiltiga/bortfiltrerade spår och varför.

## Ansatser

**A. Strukturerat block sist i svaret** (` ```spår ` med JSON som sidan plockar
ut). Minst kod, men rå JSON syns under strömningen och svagare modeller (DeepSeek
flash) slarvar med formatet. Sidan skulle behöva tolka prosa för att veta vilka
spår som redan föreslagits.

**B. Eget verktyg `propose_leads`** som modellen anropar sist. Verktyget gör
inget i sak; sidan fångar argumenten. Schemat ger pålitlig struktur i båda
vägarna, svarstexten förblir ren, och strukturerade spår går att hålla reda på
och skicka tillbaka. Kostar en verktygstur och modellen kan hoppa över anropet
(då blir det ett vanligt svar som i dag).

**C. A2UI via egen Streamlit-komponent.** Ingen Python/Streamlit-renderare finns;
kräver React-bygge och nytt protokoll för något Streamlits knappar klarar.

**Valt: B** (ägarens val). Avgörande: spåren är data, inte prosa, och ska spåras
över hela konversationen. C kan läggas ovanpå senare eftersom datan redan är
strukturerad.

## Design

### Verktyget `propose_leads`

Nytt verktyg i `rag/mcp_server.py` (`@mcp.tool()`) och motsvarande schema i
`Utredning.OPENAI_TOOLS`. Argument:

```
leads: list (0–4) av {
    title: str          # kort rubrik, ≤ ~80 tecken
    why: str            # varför spåret är värt att gräva, 1–2 meningar
    sources: list av {source: str, page: int}   # belägg; source = exakt source-värde ur sökträff
    question: str       # färdig följdfråga som skickas vid "Gräv"
}
counter_question: str | null   # högst en motfråga till operatören
```

Verktyget validerar och klampar (högst 4 spår, trimmar tomma fält, tar bort spår
utan `question`) och returnerar en kort kvittens till modellen, t.ex. "N spår och
motfråga visas för operatören. Avsluta svaret utan att upprepa dem." Ogiltiga
poster loggas på debugnivå. Verktygsbeskrivningen förklarar: anropa högst en gång
per svar, som sista åtgärd; spår ska vara luckor/motsägelser/obesvarade trådar i
hämtat material med belägg; upprepa inte grävda spår; motfråga bara när svaret
beror på operatörens avsikt; aldrig spår som gissar maskerat innehåll.

Valideringen ligger i en ren funktion (t.ex. `mcp_server.normalize_leads`) som
båda vägarna använder, så sidan får samma form oavsett backend.

**Verktyget finns alltid** i utredningsläget (oberoende av webbsök), utan eget
reglage. *(Byggarens val.)*

**Ordning:** modellen skriver hela svaret först och anropar `propose_leads` som
allra sista åtgärd. Kvittensen säger att svaret är klart och att inget mer ska
skrivas; eventuell text efter anropet visas ändå (kort, ofarlig). *(Byggarens val.)*

**Beläggkontroll på sidan** *(byggarens val — ändra om du vill ha det strängare
eller lösare)*: sidan håller en mängd `source`-värden som setts i verktygssvaren
under hela konversationen (samma `sources_from_tool_output` som citaten redan
använder). Belägg vars `source` inte finns där tas bort ur spåret; ett spår som
då saknar belägg visas inte alls (debugrad med rubrik och orsak). Så kan ett spår
aldrig visa påhittade belägg. Kontrollen görs på sidan efter
`normalize_leads`, som förblir ren.

### Fånga spåren

- **Claude-vägen** (`stream_mcp`): `ToolUseBlock` med namnet
  `mcp__arkiv__propose_leads` fångas — `block.input` normaliseras med samma
  funktion och sparas. Verktyget läggs i `allowed_tools`. Statusraden visar
  t.ex. "🧭 Föreslår spår" via `_mcp_tool_label`.
- **OpenAI-vägen** (`stream_openai_mcp`): `_run_tool` hanterar
  `propose_leads` in-process, returnerar kvittensen och lämnar de normaliserade
  spåren till anroparen. Loopen fortsätter som för andra verktyg tills modellen
  ger slutsvar.
- Anropar modellen verktyget flera gånger i samma svar gäller det sista anropet
  (debugrad).
- `stream_mcp_to_string`/`stream_openai_mcp_to_string` returnerar spåren
  tillsammans med svaret.

### Turbudget

`propose_leads` tar en verktygstur plus eventuellt en avslutande texttur.
Tursgränsen höjs med 2 internt så att
operatörens tak (`mcp_max_turns`) fortfarande gäller för sökarbetet. Nås taket
ändå körs `_answer_without_tools` som i dag — det svaret får inga spår, och det
är acceptabelt.

### Konversationens spårlista

`session_state["leads"]`: lista av spår med `id` (löpnummer), `turn` (index i
`chat_history`), fälten ovan och `status` (`föreslagen` / `grävd`). Nollställs av
**Ny konversation**.

Varje assistenttur i `chat_history` får `lead_ids` (bara id:n) och
`counter_question`; status bor enbart i `session_state["leads"]`, så att en
knapp aldrig visar inaktuell status. **Gräv** är aktiv på alla turers spår tills
spåret grävts. Ett spår vars normaliserade rubrik redan finns i listan räknas som
samma spår (samma id, ingen dubblett). *(Byggarens val.)*

En fråga som kommer från **Gräv** behandlas exakt som en skriven fråga; spårets
status i sammanfattningen räcker för att modellen inte ska föreslå det igen.
Med Claude-`resume` ackumuleras tidigare sammanfattningar i kontexten; det
accepteras eftersom de bara innehåller rubrik och status. *(Byggarens val.)*

Vid varje ny fråga bygger sidan en kort sammanfattning:

```
[Spår hittills i konversationen]
1. (grävd) Vittnet vid Tunnelgatan 23.20 förhördes bara en gång
2. (föreslagen) Bilen som nämns i tre tips 1987–1989
```

som skickas med till modellen men **inte** visas i chatten:
- Claude: läggs före frågetexten i `prompt` till `query(...)`.
- OpenAI: läggs i user-meddelandets `content` i `openai_chat_messages`.
`chat_history` innehåller bara operatörens egen text. Sammanfattningen hålls
kort (bara rubrik + status), och utelämnas när listan är tom.

### Rendering

Under varje assistentsvar i `_render_chat_turn`, efter svaret och före
pärm/källor:
- Rubriken "Spår att gräva vidare i" och ett kort per spår
  (`st.container(border=True)`): rubrik, varför, belägg som PDF-länkar via
  befintliga `casebook_ui.render_pdf_opener`/`citations.pdf_anchor`, och knappen
  **Gräv** (inaktiv och märkt "Grävt" när statusen är `grävd`).
- Motfrågan i en markerad ruta (t.ex. `st.info`) med en rad om att svara i
  chattrutan.

Klick på **Gräv**: markerar spåret grävt, sätter
`session_state["pending_question"] = spår.question` och gör `st.rerun()`. Sidan
läser `pending_question` som om det kommit från `st.chat_input` (samma kodväg för
båda backendvägarna), och den nya användarturen visar frågan. Källor till spår
vars `source` inte kan lösas visas som text (gissa aldrig fram en länk, samma
regel som citaten).

### Persona och prompt

`MCP_SYSTEM_PROMPT` får en ny rollbeskrivning: en erfaren, källkritisk
mordutredare som driver utredningen framåt — efter svaret pekar den ut vad som
inte är utrett, ställer motfrågor när avsikten är oklar och knyter an till öppna
spår från tidigare i konversationen. Alla befintliga regler (belägg, källuppgift
mot tolkning, motsägelser, OCR-osäkerhet, `[MASKAD]`, webbkällor) står kvar.
Verktygsreferensen får `propose_leads`. `PROMPT_WRITING_HELP`/`PROMPT_MODE_HELP`
uppdateras.

### CLI (`src/rag/ask.py`)

Det fristående utredningsläget lägger till verktyget i `allowed_tools` och
skriver ut spår och motfråga som text efter svaret.

### Tester

- `test_mcp_server.py`: normaliseringen (klampning, tomma fält, spår utan fråga),
  kvittenstexten och att verktygsmängden fortfarande matchar `OPENAI_TOOLS`.
- `test_utredning_ui.py` (AppTest): spår och motfråga ritas från
  `chat_history`; **Gräv** markerar spåret och skickar dess fråga som nästa tur;
  **Ny konversation** tömmer spårlistan; sammanfattningen skickas med men visas
  inte; belägg som inte setts i verktygssvaren filtreras bort.
- OpenAI-loopen med mockad klient: ett `propose_leads`-anrop följt av slutsvar ger
  spår + svar; flera anrop → sista gäller.

### Dokumentation

`docs/teknisk-referens.md` (utredningsläget), `AGENTS.md` (MCP-läget: verktyget,
spårlistan, `pending_question`, sammanfattningen som skickas med) och vid behov
`README.md`/`docs/kom-igang.md` om utredningsläget beskrivs där.

## Lämnat åt byggaren

- Exakt formulering av persona, verktygsbeskrivning och kvittens (inom ramarna
  ovan).
- Layout av spårkorten (kolumner, ikoner) så länge det följer sidans stil.
- Hur `pending_question` kopplas in i de två (i dag duplicerade) grenarna i
  `_render_mcp_tab` — gärna genom att slå ihop den gemensamma delen.
