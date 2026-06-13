# Kunskapsgraf bredvid RAG-svaret — Implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Visa ett ego-nätverk ur kunskapsgrafen i en egen kolumn bredvid varje svar i webui:t (RAG-läget och utredningsläget), centrerat kring de entiteter svaret handlar om.

**Architecture:** Färdigt svar → Haiku listar nyckelentiteter (ny modul `graph/answer_entities.py`) → namnen slås upp i Neo4j (`viz.lookup_centers`) → befintliga `fetch_ego`/`assemble_graph`/`build_pyvis_html` ritar grafen i en högerkolumn i `webui.py`. Allt degraderar tyst om Neo4j/LLM saknas.

**Tech Stack:** Python, Streamlit, Neo4j (driver), pyvis, claude-agent-sdk, openai, pytest.

**Spec:** `docs/superpowers/specs/2026-06-12-graf-bredvid-rag-svar-design.md`

> **VIKTIGT — inga git-commits:** Projektets CLAUDE.md förbjuder `git commit`/`git push` från agenter. Hoppa över alla commit-steg; användaren committar själv med `/cap`. Varje task avslutas i stället med att testsviten körs grönt.

> Alla kommandon körs från projektroten `/Users/patrik/projects/palmemordsarkivet`. Pytest: `.venv/bin/pytest`.

---

### Task 1: `parse_entity_list` — ren parser i ny modul

**Files:**
- Create: `src/graph/answer_entities.py`
- Create: `tests/test_answer_entities.py`

- [ ] **Step 1: Skriv failande tester**

Skapa `tests/test_answer_entities.py`:

```python
"""Tester för answer_entities — parsning och LLM-konfigval."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.answer_entities import parse_entity_list


def test_parse_valid_list() -> None:
    raw = '["Stig Engström", "Dekorima", "Skandia"]'
    assert parse_entity_list(raw) == ["Stig Engström", "Dekorima", "Skandia"]


def test_parse_list_in_markdown_fence() -> None:
    raw = 'Här är listan:\n```json\n["Olof Palme"]\n```\nKlart.'
    assert parse_entity_list(raw) == ["Olof Palme"]


def test_parse_garbage_returns_empty() -> None:
    assert parse_entity_list("ingen json här") == []
    assert parse_entity_list("") == []
    assert parse_entity_list('["trasig') == []
    assert parse_entity_list('{"inte": "en lista"}') == []


def test_parse_filters_and_dedups() -> None:
    raw = '["Stig Engström", "", 42, "  ", "stig engström", "Skandia"]'
    assert parse_entity_list(raw) == ["Stig Engström", "Skandia"]


def test_parse_caps_at_max() -> None:
    from graph.answer_entities import MAX_ENTITIES
    raw = "[" + ", ".join(f'"Namn {i}"' for i in range(20)) + "]"
    assert len(parse_entity_list(raw)) == MAX_ENTITIES
```

- [ ] **Step 2: Kör testerna — ska faila**

Kör: `.venv/bin/pytest tests/test_answer_entities.py -v`
Förväntat: FAIL/ERROR med `ModuleNotFoundError: No module named 'graph.answer_entities'`

- [ ] **Step 3: Skriv modulen med parsern**

Skapa `src/graph/answer_entities.py`:

```python
"""LLM-extraktion av nyckelentiteter ur ett RAG-svar — för inline-grafen i webui.

Isolerad från Streamlit och Neo4j: ren parser + ett LLM-anrop som återanvänder
anropsmönstret i ``graph.extract_entities``. Felhantering/degradering sköts av
anroparen (webui)."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, TextBlock, query,
)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
MAX_ENTITIES = 8

_SYSTEM = """\
Du får ett svar om Palmeutredningen. Lista de personer, platser och
organisationer som svaret faktiskt handlar om. Returnera ENBART en giltig
JSON-lista av namnsträngar, max 8 stycken, viktigast först, t.ex.
["Stig Engström", "Dekorima", "Skandia"].
Regler:
- Personnamn på formen "Förnamn Efternamn" när det framgår.
- Ta bara med namn som nämns i svaret — hitta inte på.
- Inga förklaringar, ingen annan text än JSON-listan."""

_LIST_RE = re.compile(r"\[.*?\]", re.DOTALL)


def parse_entity_list(raw: str) -> list[str]:
    """Plocka JSON-listan av namn ur ett LLM-svar.

    Trasig eller saknad JSON ger tom lista. Icke-strängar och tomma namn
    filtreras, dubbletter (skiftlägesokänsligt) dedupas, max MAX_ENTITIES."""
    m = _LIST_RE.search(raw or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append(name)
    return out[:MAX_ENTITIES]
```

- [ ] **Step 4: Kör testerna — ska passera**

Kör: `.venv/bin/pytest tests/test_answer_entities.py -v`
Förväntat: 5 PASS

---

### Task 2: `resolve_entity_cfg` + `extract_answer_entities`

**Files:**
- Modify: `src/graph/answer_entities.py` (lägg till i slutet)
- Modify: `tests/test_answer_entities.py` (lägg till i slutet)

- [ ] **Step 1: Skriv failande tester för konfigvalet**

Lägg till i `tests/test_answer_entities.py`:

```python
def test_resolve_entity_cfg_prefers_haiku(monkeypatch) -> None:
    from graph.answer_entities import DEFAULT_CLAUDE_MODEL, resolve_entity_cfg
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    cfg = resolve_entity_cfg({"provider": "openai", "model": "gpt-4o"})
    assert cfg == {"provider": "claude", "model": DEFAULT_CLAUDE_MODEL,
                   "base_url": "", "api_key": ""}


def test_resolve_entity_cfg_falls_back_to_openai(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    cfg = resolve_entity_cfg({"provider": "openai", "model": "gpt-4o-mini",
                              "base_url": ""})
    assert cfg == {"provider": "openai", "model": "gpt-4o-mini",
                   "base_url": "", "api_key": "sk-x"}


def test_resolve_entity_cfg_local_base_url_needs_no_key(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = resolve_entity_cfg({"provider": "openai", "model": "llama3.1:8b",
                              "base_url": "http://localhost:11434/v1"})
    assert cfg is not None and cfg["base_url"] == "http://localhost:11434/v1"


def test_resolve_entity_cfg_no_usable_llm(monkeypatch) -> None:
    from graph.answer_entities import resolve_entity_cfg
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_entity_cfg({"provider": "claude"}) is None
    assert resolve_entity_cfg({"provider": "openai", "base_url": ""}) is None
```

- [ ] **Step 2: Kör — ska faila**

Kör: `.venv/bin/pytest tests/test_answer_entities.py -v`
Förväntat: de nya testerna FAIL med `ImportError: cannot import name 'resolve_entity_cfg'`

- [ ] **Step 3: Implementera konfigval + LLM-anrop**

Lägg till i slutet av `src/graph/answer_entities.py`:

```python
def resolve_entity_cfg(saved: dict) -> dict | None:
    """Välj LLM för entitetslistningen.

    Default Claude Haiku — snabb, billig mikrouppgift, oberoende av vilken
    modell som genererade svaret. Saknas Claude-creds används openai-providern
    i llm_config om den är körbar (nyckel eller lokal base_url), annars None
    (anroparen degraderar tyst — webui får aldrig krascha på grafen)."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"):
        return {"provider": "claude", "model": DEFAULT_CLAUDE_MODEL,
                "base_url": "", "api_key": ""}
    if saved.get("provider") == "openai":
        base_url = saved.get("base_url", "")
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if base_url or api_key:
            return {"provider": "openai",
                    "model": saved.get("model") or OPENAI_DEFAULT_MODEL,
                    "base_url": base_url, "api_key": api_key}
    return None


async def _claude_call(text: str, model: str) -> str:
    """Skicka svarstexten till Claude, returnera råsvaret."""
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=model,
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
    )
    parts: list[str] = []
    async for msg in query(prompt=text, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts)


async def _openai_call(text: str, model: str, base_url: str, api_key: str) -> str:
    """Skicka svarstexten till en OpenAI-kompatibel modell, returnera råsvaret."""
    if AsyncOpenAI is None:
        raise RuntimeError("openai-paketet saknas — kör: pip install openai")
    client = AsyncOpenAI(api_key=api_key or "local", base_url=base_url or None)
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=512,
    )
    return response.choices[0].message.content or ""


async def extract_answer_entities(answer: str, cfg: dict) -> list[str]:
    """Svar → LLM → lista av entitetsnamn. Exceptions bubblar till anroparen."""
    if cfg["provider"] == "claude":
        raw = await _claude_call(answer, cfg["model"])
    else:
        raw = await _openai_call(answer, cfg["model"], cfg["base_url"],
                                 cfg["api_key"])
    return parse_entity_list(raw)
```

- [ ] **Step 4: Kör hela testfilen — ska passera**

Kör: `.venv/bin/pytest tests/test_answer_entities.py -v`
Förväntat: 9 PASS

---

### Task 3: `viz.lookup_centers`

**Files:**
- Modify: `src/graph/viz.py` (lägg till efter `search_entities`, rad ~85)
- Modify: `tests/test_viz.py` (lägg till i slutet)

- [ ] **Step 1: Skriv failande tester med fejkad session**

Lägg till i slutet av `tests/test_viz.py`:

```python
class _FakeSession:
    """Fejkar Neo4j-session: run() slår upp rader på söksträngen $q."""

    def __init__(self, by_query: dict[str, list[dict]]) -> None:
        self.by_query = by_query

    def run(self, cypher: str, **params):
        return list(self.by_query.get(params["q"].lower(), []))


def test_lookup_centers_exact_match_wins() -> None:
    from graph.viz import lookup_centers
    session = _FakeSession({"palme": [
        {"label": "Person", "namn": "Lisbeth Palme", "norm": "lisbeth palme"},
        {"label": "Person", "namn": "Palme", "norm": "palme"},
    ]})
    out = lookup_centers(session, ["Palme"])
    assert out == [{"label": "Person", "namn": "Palme", "norm": "palme"}]


def test_lookup_centers_falls_back_to_first_hit() -> None:
    from graph.viz import lookup_centers
    session = _FakeSession({"engström": [
        {"label": "Person", "namn": "Stig Engström", "norm": "stig engström"},
        {"label": "Person", "namn": "Margareta Engström", "norm": "margareta engström"},
    ]})
    out = lookup_centers(session, ["Engström"])
    assert out[0]["norm"] == "stig engström"


def test_lookup_centers_skips_misses_and_dedups() -> None:
    from graph.viz import lookup_centers
    hit = {"label": "Organisation", "namn": "Skandia", "norm": "skandia"}
    session = _FakeSession({"skandia": [hit], "skandiahuset": [hit]})
    out = lookup_centers(session, ["Skandia", "Okänd Person", "Skandiahuset"])
    assert out == [hit]
```

- [ ] **Step 2: Kör — ska faila**

Kör: `.venv/bin/pytest tests/test_viz.py -v -k lookup`
Förväntat: FAIL med `ImportError: cannot import name 'lookup_centers'`

- [ ] **Step 3: Implementera**

Lägg till i `src/graph/viz.py` direkt efter `search_entities` (före `fetch_ego`):

```python
def lookup_centers(session, names: list[str], per_name_limit: int = 10) -> list[dict]:
    """Slå upp svars-entiteter mot grafen → center-dicts för ``assemble_graph``.

    Bästa träff per namn: exakt namnmatch (skiftlägesokänslig) vinner, annars
    första träffen (``search_entities`` sorterar kortast namn först). Namn utan
    träff hoppas över; dubbletter (samma label+norm) dedupas."""
    centers: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for name in names:
        hits = search_entities(session, name, limit=per_name_limit)
        if not hits:
            continue
        exact = [h for h in hits if h["namn"].lower() == name.lower()]
        best = exact[0] if exact else hits[0]
        key = (best["label"], best["norm"])
        if key in seen:
            continue
        seen.add(key)
        centers.append(best)
    return centers
```

Obs: `search_entities` itererar rader med `r["label"]`-indexering — fejksessionens dict-rader fungerar därför rakt av.

- [ ] **Step 4: Kör hela testfilen — ska passera**

Kör: `.venv/bin/pytest tests/test_viz.py -v`
Förväntat: alla PASS (befintliga + 3 nya)

---

### Task 4: webui — hjälpfunktioner, toggle och RAG-läget

**Files:**
- Modify: `src/webui.py`

Ingen pytest-täckning för Streamlit-skriptet (följer befintligt mönster — webui testas inte i sviten). Verifiering: import-röktest + manuell körning i Task 7.

- [ ] **Step 1: Importer**

I importblocket i `src/webui.py`, efter `import config as _llm_config` (rad ~22), lägg till:

```python
from errors_log import log_error  # noqa: E402
from graph import answer_entities as _answer_entities  # noqa: E402
from graph import viz as _viz  # noqa: E402
```

- [ ] **Step 2: Hjälpfunktioner**

Lägg in direkt före `_render_rag_sources` (rad ~715):

```python
GRAPH_HEIGHT = 420


@st.cache_resource(show_spinner=False)
def _graph_driver():
    """Neo4j-driver för inline-grafen, eller None om grafen är otillgänglig.

    Cachas som resurs (stabil över reruns). Vid omstart av Neo4j: rensa cachen
    via ⋮ → Clear cache eller starta om appen."""
    pw = _viz.resolve_password()
    if not pw:
        return None
    try:
        return _viz.connect(pw)
    except Exception:  # noqa: BLE001 — grafen är frivillig, svaret får aldrig falla
        return None


def _compute_answer_centers(answer: str) -> list[dict]:
    """Svar → LLM-entitetslista → center-noder i grafen. Fel → tom lista."""
    driver = _graph_driver()
    if driver is None or not answer.strip():
        return []
    cfg = _answer_entities.resolve_entity_cfg(_llm_config.load())
    if cfg is None:
        return []
    try:
        names = asyncio.run(_answer_entities.extract_answer_entities(answer, cfg))
        if not names:
            return []
        with driver.session() as s:
            return _viz.lookup_centers(s, names)
    except Exception as exc:  # noqa: BLE001
        log_error("webui.graph", answer[:60], str(exc))
        return []


def _render_answer_graph(centers: list[dict]) -> None:
    """Rita read-only ego-nätverk för centers i aktuell container.

    Interaktiv utforskning (fäll ut noder) bor på grafsidan (pages/1_Graf.py)."""
    driver = _graph_driver()
    if driver is None:
        st.caption("Kunskapsgraf otillgänglig — starta Neo4j med `./neo4j.sh`.")
        return
    if not centers:
        st.caption("Inga entiteter ur svaret återfanns i grafen.")
        return
    all_rels: list[dict] = []
    all_docs: list[dict] = []
    try:
        with driver.session() as s:
            for c in centers:
                rels, docs = _viz.fetch_ego(s, c["norm"], c["label"], limit=40)
                all_rels.extend(rels)
                for d in docs:
                    all_docs.append({**d, "center_norm": c["norm"]})
    except Exception as exc:  # noqa: BLE001
        log_error("webui.graph", centers[0]["namn"], str(exc))
        st.caption("Kunskapsgraf otillgänglig — starta Neo4j med `./neo4j.sh`.")
        return
    nodes, edges = _viz.assemble_graph(centers, _viz.dedup_rels(all_rels), all_docs)
    html = _viz.build_pyvis_html(nodes, edges, height=f"{GRAPH_HEIGHT}px")
    data_url = ("data:text/html;base64,"
                + base64.b64encode(html.encode("utf-8")).decode("ascii"))
    st.caption("Kunskapsgraf: " + ", ".join(c["namn"] for c in centers))
    st.iframe(data_url, height=GRAPH_HEIGHT)
```

- [ ] **Step 3: Sidofälts-toggle**

I `with st.sidebar:`-blocket, direkt efter `do_rerank = st.toggle(...)`-anropet (rad ~344–350), lägg till:

```python
    show_graph = st.toggle(
        "Visa kunskapsgraf bredvid svaret",
        value=True,
        key="show_graph",
        help="Extraherar svarets nyckelentiteter (Claude Haiku) och ritar deras "
        "nätverk ur kunskapsgrafen. Kräver att Neo4j är igång (./neo4j.sh).",
    )
```

- [ ] **Step 4: Session-state-default**

Vid de övriga `ss.setdefault`-raderna (rad ~384–390), lägg till:

```python
ss.setdefault("answer_centers", [])
```

- [ ] **Step 5: RAG-läget — kolumnlayout vid ny fråga**

Ersätt det avslutande blocket i `if submitted and q.strip():` (nuvarande rad ~868–876, från `st.subheader(f"Svar ({backend_name})")` till `_render_rag_sources(hits, "sub")`) med:

```python
        st.subheader(f"Svar ({backend_name})")
        # Skapa placeholders i rätt ordning innan asyncio.run() blockerar —
        # _src_slot ersätter omedelbart ev. gamla källexpander från föregående sökning.
        if show_graph:
            _ans_col, _graph_col = st.columns([3, 2])
        else:
            _ans_col, _graph_col = st.container(), None
        with _ans_col:
            _stream_slot = st.empty()
            _src_slot = st.empty()
        _stream_slot.markdown(_THINKING_HTML, unsafe_allow_html=True)
        ss.answer = asyncio.run(stream_to_string(hits, q, backend, _stream_slot))
        with _src_slot.container():
            _render_rag_sources(hits, "sub")
        ss.answer_centers = []
        if _graph_col is not None:
            with _graph_col:
                with st.spinner("Bygger kunskapsgraf…"):
                    ss.answer_centers = _compute_answer_centers(ss.answer)
                _render_answer_graph(ss.answer_centers)
```

- [ ] **Step 6: RAG-läget — cachad rerun (PDF-knappar)**

Ersätt det sista blocket i filen (rad ~878–883) med:

```python
# Rendera resultat från session_state vid rerun från PDF-knappar (ej ny sökning).
# Bara i RAG-läget — MCP-chatten renderar sina källor inline per tur.
if ss.hits and not mcp_mode and not (submitted and q.strip()):
    st.subheader("Svar")
    if show_graph:
        _ans_col, _graph_col = st.columns([3, 2])
        with _ans_col:
            st.markdown(ss.answer, unsafe_allow_html=True)
            _render_rag_sources(ss.hits, "cached")
        with _graph_col:
            _render_answer_graph(ss.answer_centers)
    else:
        st.markdown(ss.answer, unsafe_allow_html=True)
        _render_rag_sources(ss.hits, "cached")
```

Obs: ingen ny LLM-extraktion vid rerun — `ss.answer_centers` är cachen.

- [ ] **Step 7: Import-röktest**

Kör: `.venv/bin/python -c "import ast, pathlib; ast.parse(pathlib.Path('src/webui.py').read_text())" && echo OK`
Förväntat: `OK` (syntaxkontroll; webui kan inte importeras utanför Streamlit utan sidoeffekter)

Kör: `.venv/bin/pytest tests/ -q`
Förväntat: alla PASS

---

### Task 5: webui — chatthistorik-refaktor + graf i utredningsläget

**Files:**
- Modify: `src/webui.py`

Historikrenderingen är i dag duplicerad ordagrant i Claude- och OpenAI-grenen (rad ~748–774 resp. ~795–821) — den faktoreras till en hjälpare som samtidigt får grafkolumnen.

- [ ] **Step 1: Hjälpare för chatturer**

Lägg in direkt efter `_render_rag_sources` (efter rad ~742):

```python
def _render_chat_sources(srcs: list, key_prefix: str) -> None:
    with st.expander(f"Källor ({len(srcs)})", expanded=False):
        for i, h in enumerate(srcs):
            pdf = find_pdf(h["source"])
            stem = h["source"][:-4] if h["source"].endswith(".txt") else h["source"]
            with st.container(border=True):
                cols = st.columns([5, 2])
                with cols[0]:
                    st.markdown(f"**{stem}**")
                with cols[1]:
                    if pdf and st.button("Öppna PDF", key=f"{key_prefix}_{i}",
                                         use_container_width=True):
                        try:
                            subprocess.Popen(["open", str(pdf)])
                        except OSError as e:
                            st.error(f"Kan inte öppna fil: {e}")


def _render_chat_turn(turn: dict, turn_idx: int) -> None:
    """Rendera en historiktur; assistentturer med centers får grafkolumn."""
    with st.chat_message(turn["role"]):
        centers = turn.get("centers") or []
        if turn["role"] == "assistant" and show_graph and centers:
            ans_col, graph_col = st.columns([3, 2])
        else:
            ans_col, graph_col = st.container(), None
        with ans_col:
            st.markdown(turn["text"], unsafe_allow_html=True)
            srcs = turn.get("sources") or []
            if srcs:
                _render_chat_sources(srcs, f"chat_pdf_{turn_idx}")
        if graph_col is not None:
            with graph_col:
                _render_answer_graph(centers)
```

Obs: knappnycklarna blir `chat_pdf_{turn_idx}_{i}` — identiska med dagens, så inga state-krockar.

- [ ] **Step 2: Ersätt historikloopen i Claude-grenen**

I `if mcp_mode:` / `if backend["kind"] == "claude":` — ersätt hela `for turn_idx, turn in enumerate(ss.chat_history):`-loopen (rad ~748–774) med:

```python
        for turn_idx, turn in enumerate(ss.chat_history):
            _render_chat_turn(turn, turn_idx)
```

- [ ] **Step 3: Ersätt historikloopen i OpenAI-grenen**

Samma sak i `else:`-grenen — ersätt loopen (rad ~795–821) med:

```python
        for turn_idx, turn in enumerate(ss.chat_history):
            _render_chat_turn(turn, turn_idx)
```

- [ ] **Step 4: Graf vid nytt svar — Claude-grenen**

Ersätt blocket från `with st.chat_message("assistant"):` till `st.rerun()` i Claude-grenen med:

```python
            with st.chat_message("assistant"):
                if show_graph:
                    _ans_col, _graph_col = st.columns([3, 2])
                else:
                    _ans_col, _graph_col = st.container(), None
                with _ans_col:
                    answer, new_id = asyncio.run(
                        stream_mcp_to_string(chat_q, ss.mcp_session_id)
                    )
                centers: list[dict] = []
                if _graph_col is not None:
                    with _graph_col:
                        with st.spinner("Bygger kunskapsgraf…"):
                            centers = _compute_answer_centers(answer)
                        _render_answer_graph(centers)
            ss.mcp_session_id = new_id
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                    "centers": centers,
                }
            )
            st.rerun()
```

- [ ] **Step 5: Graf vid nytt svar — OpenAI-grenen**

Motsvarande i OpenAI-grenen — ersätt blocket från `with st.chat_message("assistant"):` till `st.rerun()` med:

```python
            with st.chat_message("assistant"):
                if show_graph:
                    _ans_col, _graph_col = st.columns([3, 2])
                else:
                    _ans_col, _graph_col = st.container(), None
                with _ans_col:
                    answer = asyncio.run(
                        stream_openai_mcp_to_string(backend, ss.openai_chat_messages)
                    )
                centers: list[dict] = []
                if _graph_col is not None:
                    with _graph_col:
                        with st.spinner("Bygger kunskapsgraf…"):
                            centers = _compute_answer_centers(answer)
                        _render_answer_graph(centers)
            ss.chat_history.append(
                {
                    "role": "assistant",
                    "text": answer,
                    "sources": extract_cited_sources(answer),
                    "centers": centers,
                }
            )
            st.rerun()
```

- [ ] **Step 6: Syntax + testsvit**

Kör: `.venv/bin/python -c "import ast, pathlib; ast.parse(pathlib.Path('src/webui.py').read_text())" && echo OK`
Förväntat: `OK`

Kör: `.venv/bin/pytest tests/ -q`
Förväntat: alla PASS

---

### Task 6: Dokumentation

**Files:**
- Modify: `docs/teknisk-referens.md`
- Modify: `CLAUDE.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: teknisk-referens.md**

I avsnittet om webui (nära raden om citatlänkar, rad ~310) lägg till ett stycke:

```markdown
### Kunskapsgraf bredvid svaret

När Neo4j är igång (`./neo4j.sh` + `./load_graph.sh`) visas ett ego-nätverk ur
kunskapsgrafen i en kolumn bredvid varje svar — i både RAG-läget och
utredningsläget. Efter att svaret är klart listar Claude Haiku svarets
nyckelentiteter (`src/graph/answer_entities.py`); namnen slås upp i grafen
(`viz.lookup_centers`) och deras nätverk ritas read-only med pyvis. Grafen är
frivillig: är Neo4j nere eller LLM-konfig saknas visas en kort notis i stället,
och sidofältets toggle "Visa kunskapsgraf bredvid svaret" (på som standard)
stänger av hela steget. Interaktiv utforskning (fäll ut noder) finns kvar på
grafsidan. Entitetslistorna cachas per svar i session state, så klick på
PDF-knappar triggar inga nya LLM-anrop.
```

- [ ] **Step 2: CLAUDE.md och AGENTS.md**

I Directory Structure-trädet i båda filerna, under `graph/`-posten, lägg till raden:

```
    answer_entities.py  # LLM (Haiku) listar nyckelentiteter ur ett RAG-svar → inline-grafen i webui
```

och i `tests/`-uppräkningen lägg till `test_answer_entities` i listan.

- [ ] **Step 3: Kontrollera att inget annat doc-löfte bryts**

Kör: `grep -n "1_Graf\|kunskapsgraf" docs/teknisk-referens.md | head`
Läs träffarna och säkerställ att den nya texten inte motsäger befintlig beskrivning av grafsidan.

---

### Task 7: Slutverifiering

- [ ] **Step 1: Full testsvit**

Kör: `.venv/bin/pytest tests/ -q`
Förväntat: alla PASS, inga nya varningar relaterade till ändringarna

- [ ] **Step 2: Manuell verifiering i webui**

1. Starta Neo4j: `./neo4j.sh` (om den inte redan kör) — kontrollera att `load_graph.sh` körts någon gång.
2. Starta webui: `./web.sh`
3. RAG-läget: ställ "Vem är Stig Engström?" → svaret strömmar i vänsterkolumnen; högerkolumnen visar spinner och sedan en graf med Stig Engström m.fl. som centernoder.
4. Klicka en PDF-knapp under Källor → sidan rerunnar, grafen ritas om från cachen (ingen ny spinner för LLM-extraktion).
5. Slå på Utredningsläge (MCP), ställ en fråga → svaret får grafkolumn; ställ en följdfråga → båda assistentturerna i historiken har kvar sina grafer.
6. Stäng av toggeln "Visa kunskapsgraf bredvid svaret" → enkolumnslayout som tidigare.
7. Stoppa Neo4j och rensa cachen (⋮ → Clear cache) → grafkolumnen visar "Kunskapsgraf otillgänglig…", svaret fungerar som vanligt.

- [ ] **Step 3: Lämna över till användaren**

Rapportera resultatet. Användaren committar själv med `/cap` (inga agent-commits i detta projekt).
