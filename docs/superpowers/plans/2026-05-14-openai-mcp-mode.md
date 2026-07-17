# OpenAI MCP-läge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aktivera Utredningsläge (MCP) för alla OpenAI-kompatibla backends (GPT-4o, GPT-5, DeepSeek, custom) med en agentic tool-calling-loop och synlig verktygsstatus i chatten.

**Architecture:** `_run_tool()` importerar `search_archive`/`get_page` direkt från `mcp_server.py` och injicerar redan-laddade resurser. `stream_openai_mcp()` kör en icke-streamad agentic loop (max 10 varv) med `OPENAI_TOOLS`-schemas och visar verktygskall via `st.status()`. Claude-pathen är orörd.

**Tech Stack:** `openai` AsyncOpenAI (befintlig), `streamlit` `st.status()`, `mcp_server.py` direkt import.

---

## Filer

- Modify: `src/Utredning.py` — enda fil som ändras

---

### Task 1: Flytta `MCP_SYSTEM_PROMPT` till toppnivå-import

**Files:**
- Modify: `src/Utredning.py:26-34` (from ask import), `src/Utredning.py:273` (lokal import)

- [ ] **Steg 1: Lägg till `MCP_SYSTEM_PROMPT` i toppnivå-importen**

Raden `from ask import (` börjar på rad 26. Lägg till `MCP_SYSTEM_PROMPT` i blocket:

```python
from ask import (  # type: ignore  # noqa: E402
    CLAUDE_MODEL,
    EMBED_MODEL,
    MCP_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TABLE,
    format_context,
    rerank,
    search,
)
```

- [ ] **Steg 2: Ta bort den lokala importen i `stream_mcp` (rad 273)**

Ta bort raden:
```python
    from ask import MCP_SYSTEM_PROMPT  # type: ignore
```

- [ ] **Steg 3: Verifiera att appen startar utan fel**

```bash
cd /Users/patrik/projects/palmemordsarkivet
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); sys.path.insert(0,'src/rag'); import webui" 2>&1 | head -20
```

Förväntat: inga ImportError (Streamlit-relaterade warnings ok).

- [ ] **Steg 4: Commit**

```bash
git add src/Utredning.py
git commit -m "refactor: flytta MCP_SYSTEM_PROMPT till toppnivå-import i webui"
```

---

### Task 2: Lägg till `OPENAI_TOOLS`-konstant

**Files:**
- Modify: `src/Utredning.py` — efter `BACKENDS`-dict (rad ~172)

- [ ] **Steg 1: Lägg till konstanten direkt efter `BACKENDS`-diktens avslutande `}`**

Sätt in efter rad 172 (raden `}`  som avslutar `BACKENDS`):

```python

OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_archive",
            "description": (
                "Sök i Palmemordsarkivet och returnera relevanta textutdrag med källhänvisningar. "
                "Anropa flera gånger med olika söktermer för att täcka ett ämne från flera vinklar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Sökfrågan på svenska"},
                    "top_k": {"type": "integer", "description": "Antal kandidater att hämta (5–50)", "default": 20},
                    "top_n": {"type": "integer", "description": "Antal att behålla efter reranking (1–15)", "default": 6},
                    "hybrid": {"type": "boolean", "description": "Kombinera vektor- och BM25-sökning", "default": True},
                    "rerank": {"type": "boolean", "description": "Omranka med cross-encoder för bättre precision", "default": True},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_page",
            "description": (
                "Hämta råtexten från en specifik sida i ett arkivdokument. "
                "Använd för att läsa mer kontext kring en träff från search_archive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "description": "Filnamn från söktträff, t.ex. '281 — Titel….txt'"},
                    "page": {"type": "integer", "description": "Sidnummer (1-baserat)"},
                },
                "required": ["source", "page"],
            },
        },
    },
]
```

- [ ] **Steg 2: Commit**

```bash
git add src/Utredning.py
git commit -m "feat: lägg till OPENAI_TOOLS-schemas för agentic loop"
```

---

### Task 3: Lägg till `_run_tool()` och `stream_openai_mcp()`

**Files:**
- Modify: `src/Utredning.py` — nya funktioner efter `stream_mcp_to_string` (rad ~360)

- [ ] **Steg 1: Lägg till `_run_tool()` direkt efter `stream_mcp_to_string`**

```python

def _run_tool(name: str, arguments: dict) -> str:
    import mcp_server  # type: ignore  # noqa: PLC0415
    mcp_server._table = table
    mcp_server._model = embed_model
    if name == "search_archive":
        return mcp_server.search_archive(**arguments)
    if name == "get_page":
        return mcp_server.get_page(**arguments)
    return f"Okänt verktyg: {name}"
```

- [ ] **Steg 2: Lägg till `stream_openai_mcp()` direkt efter `_run_tool()`**

```python

async def stream_openai_mcp(
    status_box,
    text_placeholder,
    parts: list[str],
    cfg: dict,
    messages: list[dict],
) -> None:
    """Utredningsläge för OpenAI-kompatibla backends.

    messages innehåller redan user-meddelandet. Assistentens svar och
    tool-resultat appendas direkt till messages (konversationshistorik).
    """
    import json  # noqa: PLC0415

    from openai import AsyncOpenAI  # noqa: PLC0415

    api_key = cfg.get("api_key_override") or (
        os.environ.get(cfg["env"]) if cfg.get("env") else "ollama"
    )
    client = AsyncOpenAI(api_key=api_key or "ollama", base_url=cfg["base_url"])

    tool_count = 0
    try:
        for _turn in range(10):
            response = await client.chat.completions.create(
                model=cfg["model"],
                messages=messages,
                tools=OPENAI_TOOLS,
            )
            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                })
                for tc in msg.tool_calls:
                    args = json.loads(tc.function.arguments)
                    tool_count += 1
                    if tc.function.name == "search_archive":
                        label = f'search_archive: "{args.get("query", "")}"'
                    else:
                        label = f'get_page: {args.get("source", "")}, sida {args.get("page", "")}'
                    status_box.write(label)
                    result = _run_tool(tc.function.name, args)
                    messages.append({
                        "role": "tool",
                        "content": result,
                        "tool_call_id": tc.id,
                    })
            else:
                final = msg.content or ""
                parts.append(final)
                text_placeholder.markdown(final)
                messages.append({"role": "assistant", "content": final})
                break
    except Exception as exc:
        error_msg = f"*Fel vid anrop till {cfg['model']}: {exc}*"
        parts.append(error_msg)
        text_placeholder.markdown(error_msg)

    n = tool_count
    suffix = "ar" if n != 1 else ""
    done = "a" if n != 1 else ""
    status_box.update(
        label=f"{n} sökning{suffix} gjord{done}",
        state="complete",
        expanded=False,
    )
```

- [ ] **Steg 3: Lägg till `stream_openai_mcp_to_string()` direkt efter `stream_openai_mcp()`**

```python

async def stream_openai_mcp_to_string(cfg: dict, messages: list[dict]) -> str:
    status_box = st.status("Söker i arkivet…", expanded=True)
    text_placeholder = st.empty()
    parts: list[str] = []
    await stream_openai_mcp(status_box, text_placeholder, parts, cfg, messages)
    final = linkify_citations("".join(parts))
    text_placeholder.markdown(final, unsafe_allow_html=True)
    return final
```

- [ ] **Steg 4: Commit**

```bash
git add src/Utredning.py
git commit -m "feat: lägg till _run_tool och stream_openai_mcp agentic loop"
```

---

### Task 4: Uppdatera session state, toggle och "Ny konversation"-knapp

**Files:**
- Modify: `src/Utredning.py:194-203` (toggle + knapp), `src/Utredning.py:236` (session state)

- [ ] **Steg 1: Lägg till `openai_chat_messages` i session state (efter rad 236)**

Efter `ss.setdefault("mcp_session_id", None)` lägg till:

```python
ss.setdefault("openai_chat_messages", [])
```

- [ ] **Steg 2: Uppdatera `disabled`-villkoret på togglen**

Ändra:
```python
    disabled=backend["kind"] != "claude",
```
till:
```python
    disabled=backend["kind"] not in ("claude", "openai"),
```

Uppdatera även `help`-texten — ta bort "Claude" och skriv generellt:

```python
    help="Modellen söker autonomt med egna verktyg — bättre på komplexa frågor, men långsammare. "
         "I detta läge får du en chatt där modellen minns tidigare frågor.",
```

- [ ] **Steg 3: Uppdatera "Ny konversation"-knappen**

Ändra:
```python
    if mcp_mode and st.button("Ny konversation", use_container_width=True):
        ss.chat_history = []
        ss.mcp_session_id = None
        st.rerun()
```
till:
```python
    if mcp_mode and st.button("Ny konversation", use_container_width=True):
        ss.chat_history = []
        if backend["kind"] == "claude":
            ss.mcp_session_id = None
        else:
            ss.openai_chat_messages = []
        st.rerun()
```

- [ ] **Steg 4: Commit**

```bash
git add src/Utredning.py
git commit -m "feat: aktivera MCP-toggle för openai-backends och uppdatera ny-konversation-knapp"
```

---

### Task 5: Lägg till OpenAI MCP-chattbranch i huvud-UI

**Files:**
- Modify: `src/Utredning.py:363` (if mcp_mode-blocket)

- [ ] **Steg 1: Lägg till `else`-gren för OpenAI-backend**

Ändra:
```python
if mcp_mode and backend["kind"] == "claude":
```
till:
```python
if mcp_mode:
  if backend["kind"] == "claude":
```

Och lägg till `else`-grenen direkt efter det befintliga Claude-blockets `st.rerun()` (efter rad ~401), innan `else:`-grenen för RAG-läget:

```python
  else:
      for turn_idx, turn in enumerate(ss.chat_history):
          with st.chat_message(turn["role"]):
              st.markdown(turn["text"], unsafe_allow_html=True)
              srcs = turn.get("sources") or []
              if srcs:
                  with st.expander(f"Källor ({len(srcs)})", expanded=False):
                      for i, h in enumerate(srcs):
                          pdf = find_pdf(h["source"])
                          stem = h["source"][:-4] if h["source"].endswith(".txt") else h["source"]
                          with st.container(border=True):
                              cols = st.columns([5, 2])
                              with cols[0]:
                                  st.markdown(f"**{stem}**")
                              with cols[1]:
                                  if pdf and st.button(
                                      "Öppna PDF",
                                      key=f"chat_pdf_{turn_idx}_{i}",
                                      use_container_width=True,
                                  ):
                                      subprocess.Popen(["open", str(pdf)])

      chat_q = st.chat_input("Ställ en fråga till utredningsassistenten…")
      if chat_q and chat_q.strip():
          if not ss.openai_chat_messages:
              ss.openai_chat_messages.append({"role": "system", "content": MCP_SYSTEM_PROMPT})
          ss.openai_chat_messages.append({"role": "user", "content": chat_q})
          ss.chat_history.append({"role": "user", "text": chat_q, "sources": []})
          with st.chat_message("user"):
              st.markdown(chat_q)
          with st.chat_message("assistant"):
              answer = asyncio.run(
                  stream_openai_mcp_to_string(backend, ss.openai_chat_messages)
              )
          ss.chat_history.append({
              "role": "assistant",
              "text": answer,
              "sources": extract_cited_sources(answer),
          })
          st.rerun()
```

Strukturen efter ändringen:
```
if mcp_mode:
    if backend["kind"] == "claude":
        ... (orörd, existerande kod) ...
        st.rerun()
    else:
        ... (ny OpenAI-kod ovan) ...
else:
    ... (RAG-formulär, orörd) ...
```

- [ ] **Steg 2: Commit**

```bash
git add src/Utredning.py
git commit -m "feat: lägg till OpenAI MCP-chattbranch i webui"
```

---

### Task 6: Fixa RAG-synlighetsvillkor

**Files:**
- Modify: `src/Utredning.py:428`

- [ ] **Steg 1: Uppdatera villkoret**

Ändra:
```python
if ss.hits and not (mcp_mode and backend["kind"] == "claude"):
```
till:
```python
if ss.hits and not mcp_mode:
```

- [ ] **Steg 2: Commit**

```bash
git add src/Utredning.py
git commit -m "fix: dölj RAG-källsektion i alla MCP-lägen"
```

---

### Task 7: Kör befintliga tester

**Files:** inga ändringar

- [ ] **Steg 1: Kör testerna**

```bash
cd /Users/patrik/projects/palmemordsarkivet
.venv/bin/pytest tests/ -v 2>&1 | tail -20
```

Förväntat: alla befintliga tester PASS. Inga nya fel.

---

### Task 8: Manuell integrationstest

- [ ] **Steg 1: Starta appen**

```bash
cd /Users/patrik/projects/palmemordsarkivet
./web.sh
```

- [ ] **Steg 2: Testa Claude MCP — orörd**

1. Välj "Claude Opus 4.7", aktivera "Utredningsläge (MCP)"
2. Fråga: "Vem är Stig Engström?"
3. Verifiera: svar kommer, `mcp_session_id` sätts, andra frågan i samma konversation fungerar
4. Klicka "Ny konversation" — historiken rensas

- [ ] **Steg 3: Testa OpenAI MCP**

Förutsättning: `OPENAI_API_KEY` satt i miljön.

1. Välj "OpenAI GPT-4o", aktivera "Utredningsläge (MCP)" (skall inte längre vara grå)
2. Fråga: "Vem är Stig Engström?"
3. Verifiera:
   - `st.status("Söker i arkivet…")` visas och expanderas
   - Minst en rad á `🔍 search_archive: "..."` dyker upp i statusboxen
   - Statusboxen kollapsar till "N sökningar gjorda ✓"
   - Svarstext renderas med `[Nr X, sida Y]`-citat
4. Ställ en uppföljningsfråga — modellen svarar med kontext från föregående tur
5. Klicka "Ny konversation" — historiken rensas, `openai_chat_messages` töms

- [ ] **Steg 4: Testa RAG-läget fortfarande fungerar**

1. Välj valfri backend, stäng av "Utredningsläge (MCP)"
2. Ställ en fråga — RAG-sökning, reranker, svar och källexpander fungerar som förut
