# Design: Utredningsläge (MCP) för OpenAI-kompatibla backends

**Datum:** 2026-05-14  
**Fil:** `src/Utredning.py`
**Godkänd approach:** Alt A — direkt funktionsanrop

## Bakgrund

MCP-läget i Utredning.py är idag låst till Claude (via `claude_agent_sdk`). OpenAI-kompatibla backends (GPT-4o, GPT-5, DeepSeek, custom) stöder tool calling via OpenAI chat completions API och kan därmed köra samma agentic loop — bara implementationen skiljer sig.

## Komponenter

### 1. `OPENAI_TOOLS` (ny konstant)

OpenAI JSON-schemas för `search_archive` och `get_page`, speglat från `mcp_server.py`. Definieras en gång i modulens toppnivå.

```python
OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_archive",
            "description": "Sök i Palmemordsarkivet...",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer", "default": 20},
                    "top_n": {"type": "integer", "default": 6},
                    "hybrid": {"type": "boolean", "default": True},
                    "rerank": {"type": "boolean", "default": True},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page",
            "description": "Hämta råtexten från en specifik sida...",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "page": {"type": "integer"},
                },
                "required": ["source", "page"],
            },
        },
    },
]
```

### 2. `_run_tool(name, arguments)` (ny hjälpfunktion)

Importerar `search_archive` och `get_page` från `mcp_server.py` och anropar rätt funktion. Injicerar `mcp_server._table = table` och `mcp_server._model = embed_model` (redan laddade) för att undvika dubbel inläsning av embedding-modellen.

### 3. `stream_openai_mcp(q, status_box, text_placeholder, parts, cfg, messages)` (ny async-funktion)

Agentic loop, max 10 varv:
- Skickar `messages + OPENAI_TOOLS` till OpenAI API (icke-streaming)
- `finish_reason == "tool_calls"` → kör `_run_tool`, skriv till `status_box`, lägg `tool`-meddelanden i `messages`, fortsätt
- `finish_reason == "stop"` → skriv svaret till `text_placeholder`, avsluta
- Returnerar den slutliga svarstexten

### 4. Session state: `ss.openai_chat_messages`

Lista av OpenAI-format messages (`system / user / assistant / tool`). Separat från `ss.chat_history` (Claude-historik) och `ss.mcp_session_id`. Nollställs av "Ny konversation" när OpenAI-backend är aktiv.

## UI-förändringar

### Toggle-aktivering

```python
# Innan:
disabled=backend["kind"] != "claude"
# Efter:
disabled=backend["kind"] not in ("claude", "openai")
```

Hjälptexten på togglen generaliseras (nämner inte Claude specifikt).

### Verktygsvisning

Inuti `with st.chat_message("assistant"):`:
1. `st.status("Söker i arkivet…", expanded=True)` öppnas
2. Per verktygskall skrivs en rad: `🔍 search_archive: "Stig Engström"` eller `📄 get_page: 281 — Titel…, sida 3`
3. När loopen är klar: `status.update(label="N sökningar gjorda", state="complete", expanded=False)`
4. Svarstexten renderas i `st.empty()` under statusboxen

### Chatflöde

Samma `st.chat_input` + historikrendering som Claudes MCP-läge. Existerande Claude-path (`backend["kind"] == "claude"`) är orörd; OpenAI är ett nytt `else`-ben.

### Källrendering

Återanvänder `extract_cited_sources()` och PDF-knappar — fungerar eftersom citaten har samma `[Nr X, sida Y]`-format oavsett backend.

### "Ny konversation"

Nollställer rätt historia beroende på backend:
- Claude: `ss.chat_history = []; ss.mcp_session_id = None`
- OpenAI: `ss.chat_history = []; ss.openai_chat_messages = []`

### RAG-lägets synlighetsvillkor

```python
# Innan:
if ss.hits and not (mcp_mode and backend["kind"] == "claude"):
# Efter:
if ss.hits and not mcp_mode:
```

### System prompt

`ss.openai_chat_messages` initieras med `{"role": "system", "content": MCP_SYSTEM_PROMPT}` (importeras från `ask.py`, samma prompt som Claude använder). Prompten läggs till bara en gång — vid första frågan i en ny konversation.

### Felhantering

Om OpenAI API:et returnerar ett fel (t.ex. modellen stöder inte tool calling, otillräcklig API-nyckel, nätverksfel) fångas undantaget i `stream_openai_mcp`, och ett felmeddelande visas i `text_placeholder`. Ingen krasch. `openai_chat_messages` återställs inte automatiskt — användaren kan starta ny konversation manuellt.

## Avgränsningar

- **DeepSeek Reasoner** stöder tool calling med begränsningar — om API:et returnerar ett fel hanteras det gracefully (felmeddelande i chatten, ingen krasch).
- **Custom backend** — okänt stöd; användaren väljer själv att försöka.
- Inga ändringar i `mcp_server.py` — den fortsätter vara oberoende subprocess-server för Claude.
- Inga ändringar i Claude-pathen.
