# Utredningsspår i utredningsläget — implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Utredningsläget föreslår belagda utredningsspår och ställer motfrågor efter varje svar, via ett nytt verktyg `propose_leads`, och operatören gräver i ett spår med ett klick.

**Architecture:** `propose_leads` är ett MCP-verktyg (`rag/mcp_server.py`) som bara normaliserar och kvitterar; sidan fångar argumenten i båda backendvägarna (Claude via `ToolUseBlock`, OpenAI in-process). En ny Streamlit-fri modul `src/utredningsspar.py` håller konversationens spårlista (registrering, beläggfilter, dubblettkontroll, status, sammanfattning till modellen). `Utredning.py` ritar spårkort under svaren och skickar ett **Gräv**-klick som nästa fråga via `session_state["pending_question"]`.

**Tech Stack:** Python 3.11+, Streamlit (AppTest i tester), FastMCP (`mcp<2`), Claude Agent SDK, OpenAI-klienten, pytest.

**Spec:** `docs/superpowers/specs/2026-11-22-utredningsspar-design.md`

## Global Constraints

- Ingen `git commit`/`git push` — användaren kör `/cap` själv (AGENTS.md). Varje task slutar med att testerna är gröna, inte med en commit.
- Alla kommentarer, docstrings, UI-texter och testnamn på svenska.
- Verktygsmängden i `mcp_server.mcp` och `Utredning.OPENAI_TOOLS` ska vara identisk (`test_openai_verktygsscheman_matchar_mcp_verktygen`).
- Högst **4** spår per svar och högst **en** motfråga.
- Belägg vars `source` inte setts i ett verktygssvar under konversationen tas bort; ett spår utan kvarvarande belägg visas inte.
- Tursgränsen höjs internt med **2** (`LEADS_EXTRA_TURNS = 2`) i båda vägarna; operatörens `mcp_max_turns` är oförändrad.
- Ny toppnivåmodul i `src/` → läggs i `py-modules` i `pyproject.toml` (`tests/test_packaging.py`).
- Sidan loggar via `web_log.get_logger` (`_log` i `Utredning.py`), debugnivå för antal spår och bortfiltrerade spår/belägg med orsak. Logga aldrig hela dokumenttexter.
- Länkar till belägg via `casebook_ui.find_pdf` + `citations.pdf_anchor(..., page=)`; kan PDF:en inte lösas visas belägget som text — gissa aldrig fram en länk.
- Kör tester med `.venv/bin/pytest`; slutverifiering `.venv/bin/python scripts/test.py --static`.

## Review Focus

1. **Modellen anropar `propose_leads` med fel typer** (sträng i stället för lista, `page` som sträng `"3"`, saknade nycklar) → normaliseringen ska klara det utan undantag och släppa bara de ogiltiga posterna. Test i Task 1: `test_normalize_leads_tal_skrapindata`.
2. **Modellen anropar verktyget men skriver ingen text efteråt / skriver bara text före** → svaret visas ändå med spår, ingen tom bubbla med bara knappar och inget fel. Test i Task 3: `test_spar_fangas_i_openai_vagen` (anrop följt av tomt slutsvar).
3. **Omkörning (rerun) efter Gräv-klick** → knappen ska inte dubbelsända frågan och spåret ska visas som "Grävt" i alla turer där det förekom. Test i Task 4: `test_grav_skickar_fragan_och_markerar_spaaret`.
4. **Samma spår föreslås igen med lite annan stavning/skiftläge** → räknas som samma spår (normaliserad rubrik: gemener, kollapsade blanksteg, utan avslutande skiljetecken). Test i Task 2: `test_samma_rubrik_ger_samma_spar`.
5. **Ny konversation mitt i** → spårlista, kända källor och väntande fråga töms, så gamla spår inte läcker in i nästa utredning. Test i Task 4: `test_ny_konversation_tommer_spaaren`.

---

### Task 1: Verktyget `propose_leads` och dess normalisering

**Files:**
- Modify: `src/rag/mcp_server.py` (konstanter nära `MAX_RESULTS_*`; funktioner före `# ── Verktyg`; verktyget efter `web_search`)
- Modify: `src/Utredning.py` (`OPENAI_TOOLS`, ny post sist; konstant `LEADS_TOOL = "propose_leads"` bredvid `WEB_SEARCH_TOOL`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces:
  - `mcp_server.LEADS_MAX = 4`
  - `mcp_server.normalize_leads(leads: object, counter_question: object = None) -> dict` → `{"leads": list[dict], "counter_question": str | None}` där varje spår är `{"title": str, "why": str, "sources": list[{"source": str, "page": int}], "question": str}`. Trimmar strängar, släpper spår utan `title` eller `question`, släpper belägg utan `source` eller med `page` som inte går att tolka som heltal ≥ 1 (`"3"` → `3`), klipper till `LEADS_MAX`, tom/whitespace-motfråga → `None`. Släppta poster loggas med `logging.getLogger(__name__).debug` (serverns befintliga logger om en finns) med orsak.
  - `mcp_server.leads_receipt(proposal: dict) -> str` → t.ex. `"2 spår och en motfråga visas för operatören. Svaret är klart — skriv inget mer."`; med noll spår och ingen motfråga: `"Inga spår registrerades."`
  - MCP-verktyget `propose_leads(leads: list[dict], counter_question: str | None = None) -> str` som returnerar `leads_receipt(normalize_leads(...))`.
  - `Utredning.LEADS_TOOL = "propose_leads"`.

- [ ] **Step 1: Skriv de fallerande testerna** i `tests/test_mcp_server.py`:

```python
def test_normalize_leads_klampar_och_trimmar() -> None:
    spar = [
        {"title": f" Spår {i} ", "why": " skäl ", "question": f" Fråga {i}? ",
         "sources": [{"source": "a.txt", "page": 2}]}
        for i in range(6)
    ]
    ut = mcp_server.normalize_leads(spar, "  Vilken hypotes prövar du?  ")
    assert len(ut["leads"]) == mcp_server.LEADS_MAX == 4
    assert ut["leads"][0] == {"title": "Spår 0", "why": "skäl", "question": "Fråga 0?",
                              "sources": [{"source": "a.txt", "page": 2}]}
    assert ut["counter_question"] == "Vilken hypotes prövar du?"

def test_normalize_leads_tal_skrapindata() -> None:
    ut = mcp_server.normalize_leads(
        [{"title": "Utan fråga"}, "inte en dict",
         {"title": "Ok", "question": "Q?", "sources": [
             {"source": "b.txt", "page": "3"}, {"source": "", "page": 1},
             {"source": "c.txt", "page": "x"}, {"page": 4}]}],
        "   ",
    )
    assert [s["title"] for s in ut["leads"]] == ["Ok"]
    assert ut["leads"][0]["sources"] == [{"source": "b.txt", "page": 3}]
    assert ut["counter_question"] is None
    assert mcp_server.normalize_leads("inte en lista") == {"leads": [], "counter_question": None}

def test_leads_receipt() -> None:
    assert mcp_server.leads_receipt({"leads": [], "counter_question": None}) == "Inga spår registrerades."
    text = mcp_server.leads_receipt({"leads": [{}, {}], "counter_question": "Q?"})
    assert "2 spår" in text and "motfråga" in text
```

Utöka `test_openai_verktygsscheman_matchar_mcp_verktygen` med:

```python
    spar = by_name["propose_leads"]["parameters"]
    assert set(spar["properties"]) == {"leads", "counter_question"}
    assert spar["properties"]["leads"]["maxItems"] == mcp_server.LEADS_MAX
```

- [ ] **Step 2: Kör och se dem falla**
Run: `.venv/bin/pytest tests/test_mcp_server.py -k "leads or verktygsscheman" -v`
Expected: FAIL (`AttributeError: normalize_leads`, `KeyError: 'propose_leads'`).

- [ ] **Step 3: Implementera** `LEADS_MAX`, `normalize_leads`, `leads_receipt` och verktyget `propose_leads` i `mcp_server.py`. Verktygets docstring (som modellen ser) ska säga: anropa högst en gång per svar och som sista åtgärd efter att svaret är skrivet; spår är luckor, motsägelser eller obesvarade trådar i *hämtat* material, med belägg som `source` exakt ur en sökträffs source-rad och sidnummer; upprepa inte spår som står som grävda i "Spår hittills"; motfråga bara när svaret beror på operatörens avsikt (hypotes, uteslutet, tidsperiod); aldrig spår som gissar innehåll bakom `[MASKAD]`. Lägg OPENAI-schemat i `Utredning.OPENAI_TOOLS` med samma beskrivning i kortform och JSON-schema: `leads` = `array`, `maxItems: 4`, items `object` med `title`/`why`/`question` (`string`) och `sources` (`array` av `{source: string, page: integer}`), `required: ["title", "question"]`; `counter_question` = `string`; `required: ["leads"]`.

- [ ] **Step 4: Kör testerna**
Run: `.venv/bin/pytest tests/test_mcp_server.py -v`
Expected: PASS (alla, även befintliga).

---

### Task 2: Spårlistan (`src/utredningsspar.py`)

**Files:**
- Create: `src/utredningsspar.py`
- Modify: `pyproject.toml` (`py-modules`: lägg till `"utredningsspar"` i bokstavsordning)
- Test: `tests/test_utredningsspar.py`

**Interfaces:**
- Consumes: spårformen från `mcp_server.normalize_leads` (Task 1).
- Produces (Streamlit-fri, inga sidoeffekter utöver argumenten):
  - `title_key(title: str) -> str` — gemener, kollapsade blanksteg, utan avslutande `.?!:;`.
  - `register_leads(store: list[dict], proposed: list[dict], turn: int, known_sources: set[str]) -> list[int]` — filtrerar varje spårs belägg mot `known_sources` (jämför `source` både som det står och utan/med `.txt`), släpper spår utan kvarvarande belägg, återanvänder id för spår vars `title_key` redan finns i `store` (uppdaterar inte status), lägger annars till `{"id": len(store)+1, "turn": turn, "status": "föreslagen", **spår}`. Returnerar id:n för turens spår i ordning, utan dubbletter.
  - `mark_dug(store: list[dict], lead_id: int) -> dict | None` — sätter `status = "grävd"`, returnerar spåret.
  - `get_lead(store: list[dict], lead_id: int) -> dict | None`
  - `summary(store: list[dict]) -> str` — tom sträng om `store` är tomt, annars exakt:

```
[Spår hittills i konversationen]
1. (grävd) Vittnet vid Tunnelgatan
2. (föreslagen) Bilen i tre tips
```

  - Konstanter `STATUS_FORESLAGEN = "föreslagen"`, `STATUS_GRAVD = "grävd"`.

- [ ] **Step 1: Skriv de fallerande testerna** i `tests/test_utredningsspar.py`:

```python
import utredningsspar as us

SPAR = {"title": "Vittnet vid Tunnelgatan", "why": "skäl", "question": "Q?",
        "sources": [{"source": "a.txt", "page": 2}, {"source": "påhittad.txt", "page": 1}]}

def test_belagg_som_inte_lasts_tas_bort() -> None:
    store: list[dict] = []
    ids = us.register_leads(store, [SPAR], turn=1, known_sources={"a"})
    assert ids == [1]
    assert store[0]["sources"] == [{"source": "a.txt", "page": 2}]
    assert store[0]["status"] == us.STATUS_FORESLAGEN and store[0]["turn"] == 1

def test_spar_utan_kvarvarande_belagg_visas_inte() -> None:
    store: list[dict] = []
    assert us.register_leads(store, [SPAR], turn=1, known_sources=set()) == []
    assert store == []

def test_samma_rubrik_ger_samma_spar() -> None:
    store: list[dict] = []
    us.register_leads(store, [SPAR], 1, {"a.txt"})
    us.mark_dug(store, 1)
    igen = dict(SPAR, title="  vittnet vid  TUNNELGATAN. ")
    assert us.register_leads(store, [igen], 3, {"a.txt"}) == [1]
    assert len(store) == 1 and store[0]["status"] == us.STATUS_GRAVD

def test_summary() -> None:
    store: list[dict] = []
    assert us.summary(store) == ""
    us.register_leads(store, [SPAR, dict(SPAR, title="Bilen i tre tips")], 1, {"a.txt"})
    us.mark_dug(store, 1)
    assert us.summary(store) == (
        "[Spår hittills i konversationen]\n"
        "1. (grävd) Vittnet vid Tunnelgatan\n"
        "2. (föreslagen) Bilen i tre tips"
    )
```

- [ ] **Step 2: Kör och se dem falla**
Run: `.venv/bin/pytest tests/test_utredningsspar.py -v`
Expected: FAIL (`ModuleNotFoundError: utredningsspar`).

- [ ] **Step 3: Implementera** modulen enligt Interfaces och lägg till den i `py-modules`.

- [ ] **Step 4: Kör testerna**
Run: `.venv/bin/pytest tests/test_utredningsspar.py tests/test_packaging.py -v`
Expected: PASS.

---

### Task 3: Fånga spåren i båda backendvägarna

**Files:**
- Modify: `src/Utredning.py` — `_mcp_tool_label`, `stream_mcp`, `stream_mcp_to_string`, `stream_openai_mcp`, `stream_openai_mcp_to_string`, `_run_tool`
- Test: `tests/test_utredning_ui.py`

**Interfaces:**
- Consumes: `mcp_server.normalize_leads`, `mcp_server.leads_receipt`, `LEADS_TOOL` (Task 1).
- Produces:
  - `LEADS_EXTRA_TURNS = 2` i `Utredning.py`; både `stream_mcp` (`max_turns=max_turns + LEADS_EXTRA_TURNS`) och `stream_openai_mcp` (`range(max_turns + LEADS_EXTRA_TURNS)`) använder den.
  - `stream_mcp(..., tool_sources=None, proposal: dict | None = None)` — när ett `ToolUseBlock` har namnet `mcp__arkiv__propose_leads` ersätts `proposal`s innehåll (`proposal.clear(); proposal.update(normalize_leads(**input))` med `input.get("leads")`, `input.get("counter_question")`); ett andra anrop skriver över och ger en debugrad. Lägg `"mcp__arkiv__propose_leads"` i `allowed_tools`.
  - `stream_mcp_to_string(q, resume_id, cfg, search) -> tuple[str, str | None, dict, set[str]]` → `(svar, session_id, proposal, tool_sources)`; `proposal` är `{"leads": [], "counter_question": None}` om verktyget inte anropades (även vid fel).
  - `stream_openai_mcp(..., search, proposal: dict | None = None)` — `propose_leads` hanteras i loopen före `_run_tool`: normalisera, skriv in i `proposal`, svara modellen med `leads_receipt`. `_run_tool` lämnas orörd för övriga verktyg.
  - `stream_openai_mcp_to_string(cfg, messages, search) -> tuple[str, dict, set[str]]` → `(svar, proposal, kända källor)`.
  - `_mcp_tool_label` ger `"🧭 Föreslår utredningsspår"` för `propose_leads`.
  - Debugrad per svar: `"utredningsläget: %d spår föreslagna, motfråga=%s"`.

- [ ] **Step 1: Skriv de fallerande testerna** i `tests/test_utredning_ui.py`. Bygg en falsk klient (samma mönster som `_FakeCompletions`, räknare `anrop`) som svarar i tur och ordning: (1) `search_archive`-anrop, (2) `propose_leads`-anrop med ett spår vars belägg är `{"source": "a.txt", "page": 2}` och motfrågan `"Vilken hypotes prövar du?"`, (3) slutsvar `""` med `finish_reason="stop"` (tomt, Review Focus 2). Patcha `mcp_server.search_with_settings` att returnera `'Utdrag\nsource: "a.txt"\n'`. Testet anropar strömfunktionen direkt (inte sidan), så tasken är oberoende av Task 4:

```python
def test_spar_fangas_i_openai_vagen(sida: AppTest, monkeypatch) -> None:
    import Utredning  # sidan är redan importerbar efter fixturen
    ...  # fejkklient enligt ovan, monkeypatch("openai.AsyncOpenAI", ...)
    proposal: dict = {}
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "Vem såg mördaren?"}]
    parts: list[str] = []
    asyncio.run(Utredning.stream_openai_mcp(_Box(), _Box(), parts, cfg, messages, search, proposal=proposal))
    assert proposal["counter_question"] == "Vilken hypotes prövar du?"
    assert proposal["leads"][0]["sources"] == [{"source": "a.txt", "page": 2}]
    assert fejk.anrop == 3
    assert messages[-2]["role"] == "tool" and "spår" in messages[-2]["content"]
```

`_Box` är en atrapp med `write`, `update` och `markdown` som inte gör något; `cfg = {"model": "m", "base_url": "http://x", "api_key": "k"}`; `search` = sidans standardsökval (`{"reranker_mode": "bge", "top_k": 50, "top_n": 6, "web_search": False, "mcp_max_turns": 15, "mcp_web_search_budget": 3}` — kontrollera nycklarna mot `_mcp_limits`). Om import av `Utredning` utanför AppTest inte går (sidan kör Streamlit vid import), lägg testet som AppTest-körning i stället och kontrollera `proposal` genom att patcha `utredningsspar.register_leads` först i Task 4 — men försök direktanropet först.

Lägg också till ett rent enhetstest att Claude-vägens `allowed_tools` innehåller `mcp__arkiv__propose_leads`: patcha `Utredning.query` med en async-generator som fångar `options` och yieldar ett `AssistantMessage` med ett `ToolUseBlock(name="mcp__arkiv__propose_leads", input={...})` följt av ett `ResultMessage`; assert på `proposal` och `options.max_turns == 15 + LEADS_EXTRA_TURNS`.

- [ ] **Step 2: Kör och se dem falla**
Run: `.venv/bin/pytest tests/test_utredning_ui.py -k "spar" -v`
Expected: FAIL.

- [ ] **Step 3: Implementera** enligt Interfaces. Uppdatera anropsställena i `_render_mcp_tab` till de nya returvärdena (packa upp och ignorera tills vidare — Task 4 använder dem).

- [ ] **Step 4: Kör hela UI- och servertesterna**
Run: `.venv/bin/pytest tests/test_utredning_ui.py tests/test_mcp_server.py -v`
Expected: PASS (inkl. `test_tursgransen_tvingar_fram_ett_svar`).

---

### Task 4: Spårkort, Gräv-knapp och sammanfattning till modellen

**Files:**
- Modify: `src/Utredning.py` — `ss.setdefault`-blocket, `_render_chat_turn`, `_render_mcp_tab`; ny `_render_leads(turn: dict, turn_idx: int) -> None`
- Test: `tests/test_utredning_ui.py`

**Interfaces:**
- Consumes: `utredningsspar.*` (Task 2), nya returvärden (Task 3).
- Produces:
  - Session state: `leads: list[dict]` (spårlistan), `lead_sources: set[str]` (alla källor sedda i verktygssvar under konversationen), `pending_question: str | None`.
  - Assistentturer i `chat_history` får `lead_ids: list[int]` och `counter_question: str | None`.
  - **Ny konversation** nollställer `leads`, `lead_sources`, `pending_question`.

- [ ] **Step 1: Skriv de fallerande testerna** (AppTest, OpenAI-vägen med fejkklienten från Task 3 eller förifylld `session_state`):

```python
def test_spar_och_motfraga_ritas_ur_historiken(sida: AppTest) -> None:
    _run(sida, MCP)
    sida.session_state["leads"] = [{"id": 1, "turn": 1, "status": "föreslagen",
        "title": "Bilen i tre tips", "why": "Nämns 1987–1989", "question": "Vad vet vi om bilen?",
        "sources": [{"source": "a.txt", "page": 2}]}]
    sida.session_state["chat_history"] = [
        {"role": "user", "text": "Fråga", "sources": []},
        {"role": "assistant", "text": "Svar", "sources": [], "lead_ids": [1],
         "counter_question": "Vilken hypotes prövar du?"}]
    sida.run()
    text = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "Bilen i tre tips" in text and "Nämns 1987–1989" in text
    assert any("Vilken hypotes prövar du?" in i.value for i in sida.info)
    assert [b for b in sida.button if b.label == "Gräv"]

def test_grav_skickar_fragan_och_markerar_spaaret(sida: AppTest, monkeypatch) -> None:
    # fejkklient som svarar "Svar om bilen." direkt och sparar inkommande messages
    ...
    next(b for b in sida.button if b.label == "Gräv").click().run()
    assert sida.session_state["leads"][0]["status"] == "grävd"
    anvandare = [t["text"] for t in sida.session_state["chat_history"] if t["role"] == "user"]
    assert anvandare[-1] == "Vad vet vi om bilen?"
    assert anvandare.count("Vad vet vi om bilen?") == 1          # ingen dubbelsändning
    assert not [b for b in sida.button if b.label == "Gräv"]       # knappen nu "Grävt"
    sida.run()                                                     # en extra rerun
    assert anvandare.count("Vad vet vi om bilen?") == 1

def test_sammanfattningen_skickas_men_visas_inte(sida: AppTest, monkeypatch) -> None:
    # fejkklient som sparar senaste user-content; förifylld spårlista
    ...
    sida.chat_input[0].set_value("Nästa fråga").run()
    assert fejk.senaste_user.startswith("[Spår hittills i konversationen]")
    assert fejk.senaste_user.endswith("Nästa fråga")
    assert sida.session_state["chat_history"][-2]["text"] == "Nästa fråga"

def test_ny_konversation_tommer_spaaren(sida: AppTest) -> None:
    _run(sida, MCP)
    sida.session_state["leads"] = [{"id": 1, "turn": 1, "status": "föreslagen", "title": "x",
        "why": "", "question": "q", "sources": []}]
    sida.session_state["lead_sources"] = {"a.txt"}
    sida.run()
    next(b for b in sida.button if b.label == "Ny konversation").click().run()
    assert sida.session_state["leads"] == [] and sida.session_state["lead_sources"] == set()
    assert sida.session_state["pending_question"] is None
```

(`pending_question` sätts inte i förväg, eftersom den annars skickas direkt i första körningen; testet visar att nollställningen lämnar den `None`.)

- [ ] **Step 2: Kör och se dem falla**
Run: `.venv/bin/pytest tests/test_utredning_ui.py -k "spar or grav or sammanfattning or ny_konversation" -v`
Expected: FAIL.

- [ ] **Step 3: Implementera.**
  - Slå ihop den gemensamma delen av Claude- och OpenAI-grenarna i `_render_mcp_tab` (historik, `chat_input`, appendning av turen) så att `pending_question` och spårregistreringen bara finns på ett ställe; bara själva anropet skiljer sig.
  - Frågan: `chat_q = st.chat_input(...)`; saknas den, `chat_q = ss.pop("pending_question", None)` (pop före anropet → ingen dubbelsändning vid rerun).
  - Till modellen skickas `f"{summary}\n\n{chat_q}"` när `utredningsspar.summary(ss.leads)` inte är tom (Claude: `q` till `stream_mcp_to_string`; OpenAI: user-meddelandets `content`). `chat_history` får bara `chat_q`.
  - Efter svaret: `ss.lead_sources |= tool_sources`; `lead_ids = utredningsspar.register_leads(ss.leads, proposal["leads"], turn=len(ss.chat_history), known_sources=ss.lead_sources)`; debugrad med antal föreslagna mot registrerade och rubriker på bortfiltrerade spår.
  - `_render_leads` anropas i `_render_chat_turn` för assistentturer efter svaret och före pärm/källor. Rubrik `**Spår att gräva vidare i**`; per spår `st.container(border=True)` med rubrik i fetstil, `why` som caption, belägg som `pdf_anchor`-länkar (`[a, sida 2]`) via `st.markdown(..., unsafe_allow_html=True)` (escapa etiketter med `html.escape`; olösta som text), och `st.button("Gräv", key=f"lead_{lead_id}_{turn_idx}", on_click=...)` — callbacken kör `mark_dug` och sätter `pending_question`. Grävda spår visar `st.button("Grävt", disabled=True, key=...)`. Motfrågan: `st.info(f"**Motfråga:** {q}  \nSvara i chattrutan nedan.")`.
  - Lägg till `leads`, `lead_sources`, `pending_question` i `ss.setdefault`-blocket och i **Ny konversation**.

- [ ] **Step 4: Kör testerna**
Run: `.venv/bin/pytest tests/test_utredning_ui.py -v`
Expected: PASS.

---

### Task 5: Persona, prompthjälp, CLI och dokumentation

**Files:**
- Modify: `src/prompts.py` (`MCP_SYSTEM_PROMPT`)
- Modify: `src/admin_ui.py` (`PROMPT_WRITING_HELP`, `PROMPT_MODE_HELP["mcp"]`)
- Modify: `src/rag/ask.py` (MCP-läget, `allowed_tools` och utskrift)
- Modify: `docs/teknisk-referens.md` (avsnittet om utredningsläget / `#### MCP-läge (--mcp)`), `AGENTS.md` (MCP-läge-stycket), `README.md`/`docs/kom-igang.md` bara om utredningsläget beskrivs där (`grep -n -i utredningsläge`)
- Test: `tests/test_prompts.py` eller befintligt prompttest (sök `grep -rn "MCP_SYSTEM_PROMPT" tests/`)

**Interfaces:**
- Consumes: verktygskontraktet från Task 1.

- [ ] **Step 1: Skriv det fallerande testet** — i befintlig prompttestfil (skapa `tests/test_prompts.py` om ingen finns):

```python
def test_mcp_prompten_beskriver_propose_leads() -> None:
    from prompts import MCP_SYSTEM_PROMPT
    assert "propose_leads" in MCP_SYSTEM_PROMPT
    assert "mcp__arkiv__propose_leads" in MCP_SYSTEM_PROMPT
    assert "mordutredare" in MCP_SYSTEM_PROMPT

def test_prompthjalpen_namner_propose_leads() -> None:
    import admin_ui
    assert "propose_leads" in admin_ui.PROMPT_WRITING_HELP or "propose_leads" in admin_ui.PROMPT_MODE_HELP["mcp"]
```

Utöka testet för `ask.py`:s MCP-läge om det finns (`grep -n allowed_tools tests/`); annars ett test som patchar `ask.query` och asserterar att `mcp__arkiv__propose_leads` finns i `options.allowed_tools`.

- [ ] **Step 2: Kör och se dem falla**
Run: `.venv/bin/pytest tests/ -k "propose_leads or prompten" -v`
Expected: FAIL.

- [ ] **Step 3: Implementera.**
  - Prompten: ny rollinledning — en erfaren, källkritisk mordutredare som driver utredningen framåt: efter svaret pekar den ut vad som inte är utrett, knyter an till öppna spår i "Spår hittills", och ställer en motfråga bara när svaret beror på operatörens avsikt. Alla befintliga regler står kvar ordagrant. Verktygsreferensen får en punkt för `propose_leads` (argument, högst 4 spår, en motfråga, sist efter svaret, belägg = exakt `source` + sida ur hämtat material, inga spår om maskerat innehåll) och Claude-namnet `mcp__arkiv__propose_leads` i raden om prefixade namn.
  - Prompthjälpen: nämn verktyget och att en egen prompt bör beskriva det för att spåren ska bli bra (verktygsbeskrivningen räcker för att det ska fungera).
  - `ask.py`: lägg till verktyget i `allowed_tools`, fånga `ToolUseBlock`-argumenten som i Task 3, och skriv ut spår (rubrik, varför, belägg, följdfråga) och motfrågan efter svaret.
  - Dokumentation: beskriv verktyget, spårlistan, beläggfiltret, Gräv/`pending_question`, sammanfattningen som skickas med, `LEADS_EXTRA_TURNS` och att listvyn är kommande. I AGENTS.md: kort under **MCP-läge** — särskilt att `pending_question` måste poppas före anropet, att status bara bor i `session_state["leads"]`, och att belägg filtreras mot sedda källor.

- [ ] **Step 4: Slutverifiering**
Run: `.venv/bin/python scripts/test.py --static`
Expected: alla tester gröna, ruff och mypy utan fel.

- [ ] **Step 5: Manuell kontroll i appen** — starta `./web.sh`, välj utredningsläget med en OpenAI-kompatibel profil, ställ en fråga, kontrollera att spårkort med länkade belägg och ev. motfråga visas, klicka **Gräv** och kontrollera att frågan skickas och spåret blir "Grävt". Upprepa med Claude-profilen.
