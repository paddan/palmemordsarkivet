# Design: kunskapsgraf bredvid RAG-svaret

**Datum:** 2026-06-12
**Status:** Godkänd av användaren
**Reviderad efter feedback:** kolumnlayouten ersattes med en hopfälld expander
i fullbredd mellan svaret och källorna (höjd 620 px), och pyvis-färgerna följer
nu aktivt Streamlit-tema (`_theme_colors` i webui, parametrar i
`build_pyvis_html`). Sidofältstoggeln heter "Visa kunskapsgraf".

## Mål

När webui:t visar ett svar (både RAG-läget och utredningsläget/MCP) ska ett
ego-nätverk ur kunskapsgrafen visas i en egen kolumn bredvid svaret, centrerat
kring de entiteter svaret faktiskt handlar om.

## Dataflöde

```
svar (komplett text)
  → LLM (Haiku) listar nyckelentiteter ur svaret
  → varje namn slås upp i Neo4j (search_entities → bästa träff)
  → ego-nätverk byggs (fetch_ego + assemble_graph, befintliga funktioner)
  → pyvis-HTML renderas i höger kolumn (st.iframe, samma mönster som 1_Graf.py)
```

Grafen ritas alltid efter att svaret strömmat klart — entitetsextraktionen
kräver hela texten. Under strömningen visar grafkolumnen en spinner/platshållare.

## Layout

- **RAG-läget:** `st.columns([3, 2])` runt svarsblocket. Vänster: strömmande
  svar + Källor-expander (som idag). Höger: grafkolumnen.
- **Utredningsläget (MCP, både Claude- och OpenAI-vägen):** samma
  kolumnuppdelning inuti varje `st.chat_message("assistant")`. Gäller även
  historikrendering vid rerun.

## Komponenter

### 1. `src/graph/answer_entities.py` (ny modul)

LLM-extraktion, isolerad från Streamlit och Neo4j.

- `parse_entity_list(raw: str) -> list[str]` — ren parser: plockar JSON-lista
  ur råsvar (hanterar kodstaket, skräp runt JSON; skräp → `[]`). Testbar utan LLM.
- `extract_answer_entities(answer: str, cfg: dict) -> list[str]` — anropar
  LLM:en med en egen system-prompt: "returnera de personer, platser och
  organisationer svaret faktiskt handlar om, som en JSON-lista av namn
  (Förnamn Efternamn-form), max ~8 st". Återanvänder anropsmönstret från
  `src/graph/extract_entities.py` (`_claude_call`/`_openai_call`-stil).

**Modellval:** default **Claude Haiku** (`claude-haiku-4-5-20251001`) — snabb,
billig mikrouppgift, oberoende av vilken modell som genererade svaret. Om
Claude-credentials saknas faller den tillbaka på providern i
`generated/llm_config.json`.

### 2. `src/graph/viz.py` (utökas)

- `lookup_centers(session, names: list[str]) -> list[dict]` — ren funktion:
  för varje namn körs `search_entities`; bästa träff väljs (exakt match
  skiftlägesokänsligt > kortaste CONTAINS-träff); dedupas på
  `(label, norm)`. Namn utan träff hoppas över.

Befintliga `fetch_ego`, `dedup_rels`, `assemble_graph`, `build_pyvis_html`
återanvänds oförändrade.

### 3. `src/webui.py` (utökas)

- Hjälpfunktion `_render_answer_graph(...)`:
  - Resolvar Neo4j-lösenord + cachad driver (samma `@st.cache_resource`-mönster
    som `1_Graf.py`).
  - Hämtar/cachar **center-listan** så att PDF-klick-reruns inte triggar nytt
    LLM-anrop:
    - RAG-läget: `ss.answer_centers` bredvid `ss.answer`/`ss.hits`.
    - MCP-läget: `centers`-nyckel i varje turs dict i `chat_history`.
  - Bygger ego-nätverk för alla centers, renderar pyvis via `st.iframe`
    (base64-data-URL som på grafsidan).
  - **Read-only:** inga "fäll ut"-knappar — interaktiv utforskning bor kvar på
    den dedikerade grafsidan (`src/pages/1_Graf.py`).
- Sidofälts-toggle **"Visa kunskapsgraf bredvid svaret"**, default **på**.
  Avstängd → ingen kolumnuppdelning, inget LLM-anrop, beteendet som idag.

## Graciös degradering

- Neo4j nere eller lösenord saknas → diskret caption i grafkolumnen
  ("Kunskapsgraf otillgänglig — starta Neo4j med `./neo4j.sh`").
  Svaret påverkas aldrig; inga exceptions bubblar upp.
- LLM-anropet misslyckas → samma tysta degradering + rad i `errors_log`.
- Inga entiteter hittas, eller ingen matchar grafen →
  "Inga entiteter ur svaret återfanns i grafen."

## Tester

- `tests/test_answer_entities.py` — `parse_entity_list`: välformad JSON,
  JSON i kodstaket, text runt JSON, skräp → `[]`, dedup/trim.
- Test för `viz.lookup_centers` med fejkad session (samma stil som befintliga
  viz-tester): exakt match vinner, dedup, namn utan träff hoppas över.

## Dokumentation

Per projektets CLAUDE.md uppdateras i samma commit:
`docs/teknisk-referens.md` (nytt beteende + toggle), `CLAUDE.md`
(filöversikt: `answer_entities.py`), ev. `README.md` om skärmbilder berörs.

## Avgränsningar (YAGNI)

- Ingen utfällning/interaktion i inline-grafen.
- Ingen graf för user-turer i chatten, bara assistentsvar.
- Ingen caching av entitetslistor över sessioner.
