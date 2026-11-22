"""Beteendetester för Utredning-sidans lägesval och sökval (headless AppTest).

Streamlit raderar widgetstate för widgets som inte ritas i en körning, så båda
lärnas sökval måste speglas för att överleva ett lägesbyte: RAG:s reranker,
top-K/N, facetter och fuzzy, och MCP:s reranker, top-K/N och webbsök — ingen av
sektionerna ritas medan det andra läget är valt. Testerna kör sidan headless med
tunga beroenden utbytta (vektor-db, embedding-modell och state-db) och
kontrollerar beteendet i stället för källkoden.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

UTREDNING = Path(__file__).resolve().parents[1] / "src" / "Utredning.py"
RAG = "Fråga arkivet (RAG)"
MCP = "Utredningsläge (MCP)"
TOP_K = "Hämta top-K kandidater"
TOP_N = "Skicka top-N till AI"
FUZZY = "OCR-tolerant fuzzy-sökning"
TRÖSKEL = "Fuzzy-likhet (tröskel)"
RERANKER = "Reranker"
WEBBSÖK = "Tillåt webbsök"


class _FakeTable:
    def __init__(self, rows: int = 1234) -> None:
        self.rows = rows

    def count_rows(self) -> int:
        return self.rows


class _FakeDB:
    def __init__(self, tables: tuple[str, ...] = ("chunks",), rows: int = 1234) -> None:
        self.tables = list(tables)
        self.rows = rows

    def list_tables(self):
        return self

    def open_table(self, name: str) -> _FakeTable:
        if name not in self.tables:
            raise ValueError(f"Table '{name}' was not found")
        return _FakeTable(self.rows)


class _FakeModel:
    def encode(self, texts, **kwargs):
        return np.zeros((len(texts), 4), dtype=np.float32)


def _run(at: AppTest, mode: str | None) -> AppTest:
    at.session_state["main_mode"] = mode
    at.run()
    assert not at.exception, at.exception
    return at


@pytest.fixture(autouse=True)
def _tysta_bare_mode_varningar() -> None:
    """AppTest kör skriptet utan webbläsarkontext. Streamlits varning om det
    säger inget om testet, så den hålls borta från testsviten."""
    logging.getLogger(
        "streamlit.runtime.scriptrunner_utils.script_run_context"
    ).setLevel(logging.ERROR)


# Egen LLM-profil i stället för generated/llm_config.json: annars avgör
# maskinens konfiguration och miljö (t.ex. en Claude-profil utan nyckel, vilket
# ger st.stop() före kropparna) om testet mäter något alls.
_TESTPROFIL = {"kind": "openai", "backend_name": "Test", "model": "test-modell",
               "base_url": "http://localhost:0/v1", "prices": {}}


@pytest.fixture
def sida(monkeypatch, tmp_path) -> AppTest:
    """Utredning-sidan utan tunga beroenden, startad i RAG-läget."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    # Sidan läser (och LLM-formuläret skriver) llm_config.json — peka allt på en
    # temp-fil, annars kan ett test ändra utvecklarens riktiga konfiguration.
    monkeypatch.setattr("config.CONFIG_FILE", tmp_path / "llm_config.json")
    monkeypatch.setattr("lancedb.connect", lambda *a, **k: _FakeDB())
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", lambda *a, **k: _FakeModel()
    )
    monkeypatch.setattr(
        "config.load_all", lambda: {"profiles": {"Test": {}}, "default": "Test"}
    )
    monkeypatch.setattr(
        "config.resolve_runtime_profile", lambda profile, backends: dict(_TESTPROFIL)
    )
    return _run(AppTest.from_file(str(UTREDNING), default_timeout=120), RAG)


def _slider(at: AppTest, label: str):
    return next(s for s in at.sidebar.slider if s.label == label)


def _toggle(at: AppTest, label: str):
    return next(t for t in at.sidebar.toggle if t.label == label)


def _selectbox(at: AppTest, label: str):
    return next(s for s in at.sidebar.selectbox if s.label == label)


def _sidebar_labels(at: AppTest) -> list[str]:
    return [w.label for w in at.sidebar.selectbox] + [w.label for w in at.sidebar.slider]


def test_rag_laget_visar_sokinställningarna_i_sidofältet(sida: AppTest) -> None:
    # Kropparna måste faktiskt ritas: sidofältet ritas före ett ev. st.stop(), så
    # utan de här raderna kunde testet bli grönt av att RAG-kroppen aldrig kördes.
    assert [f.label for f in sida.text_input] == ["Din fråga"]
    labels = _sidebar_labels(sida)
    assert RERANKER in labels
    assert TOP_K in labels
    assert TOP_N in labels
    assert _slider(sida, TOP_K).max == 100
    assert _slider(sida, TOP_N).max == 30
    assert FUZZY in [t.label for t in sida.sidebar.toggle]
    # LLM-profilen gäller båda lägena och ska alltid finnas.
    assert "LLM-profil" in labels


def test_top_k_startar_pa_50(sida: AppTest) -> None:
    """Standarden är 50 efter mätningen i docs/jev-reranker-pilot.md: topp 6
    rymde 38 av 60 belägg med 20 kandidater och 44 med 50. top_n styr vad som
    skickas till modellen, så fler kandidater kostar inga tokens."""
    assert _slider(sida, TOP_K).value == 50
    assert _slider(sida, TOP_N).value == 6


def test_mcp_laget_visar_sina_egna_sokinstallningar(sida: AppTest) -> None:
    """MCP-läget har samma tre rattar som RAG (reranker, top-K, top-N), men
    RAG-lägets sökfilter hör inte hit: MCP-verktyget har ingen facett- eller
    fuzzy-väg."""
    _run(sida, MCP)
    assert sida.chat_input, "MCP-kroppen ritades inte (tidig st.stop()?)"
    labels = _sidebar_labels(sida)
    assert "LLM-profil" in labels
    assert RERANKER in labels
    assert TOP_K in labels
    assert TOP_N in labels
    assert "Begränsa till entiteter" not in labels
    assert FUZZY not in [t.label for t in sida.sidebar.toggle]


def test_mcp_sokval_startar_pa_50_och_6(sida: AppTest) -> None:
    _run(sida, MCP)
    assert _slider(sida, TOP_K).value == 50
    assert _slider(sida, TOP_N).value == 6
    assert _selectbox(sida, RERANKER).value == "BGE – lokal"


def test_mcp_sokvalen_overlever_ett_varv_i_rag(sida: AppTest) -> None:
    """Samma spegling som RAG-läget behöver, men åt andra hållet: MCP-widgetarna
    avmonteras när RAG-läget ritas, och utan speglingen hade valen nollställts."""
    _run(sida, MCP)
    _slider(sida, TOP_K).set_value(42)
    _slider(sida, TOP_N).set_value(9)
    _selectbox(sida, RERANKER).set_value("Ingen")
    sida.run()

    _run(sida, RAG)
    _run(sida, MCP)

    assert _slider(sida, TOP_K).value == 42
    assert _slider(sida, TOP_N).value == 9
    assert _selectbox(sida, RERANKER).value == "Ingen"


def test_avmarkerad_lageskontroll_kor_rag(sida: AppTest) -> None:
    """Segmented control kan ge None; då ska sidan ändå visa RAG-läget."""
    _run(sida, None)
    assert RERANKER in _sidebar_labels(sida)


def test_sokvalen_overlever_ett_varv_i_mcp(sida: AppTest) -> None:
    """Rundturen täcker reranker, top-K/N och fuzzy (på/tröskel). Facetterna
    lämnas utanför eftersom de kräver ett fake:at entitetsindex; de speglas på
    exakt samma sätt."""
    _slider(sida, TOP_K).set_value(33)
    _slider(sida, TOP_N).set_value(9)
    # Tröskeln är inaktiverad tills fuzzy slås på, och AppTest vägrar ändra
    # inaktiverade widgets — slå på först, som en användare gör.
    _toggle(sida, FUZZY).set_value(True)
    sida.run()
    _slider(sida, TRÖSKEL).set_value(0.85)
    _selectbox(sida, RERANKER).set_value("Ingen")
    sida.run()
    assert _slider(sida, TOP_K).value == 33

    _run(sida, MCP)
    # MCP-läget har egna rattar med samma etiketter men andra nycklar (mcp_*),
    # och de ritas med sina egna startvärden — RAG-valen ligger kvar i _sparad.
    assert _slider(sida, TOP_K).value == 50
    assert _selectbox(sida, RERANKER).value == "BGE – lokal"

    _run(sida, RAG)
    assert _slider(sida, TOP_K).value == 33
    assert _slider(sida, TOP_N).value == 9
    assert _slider(sida, TRÖSKEL).value == pytest.approx(0.85)
    assert _toggle(sida, FUZZY).value is True
    assert _selectbox(sida, RERANKER).value == "Ingen"


def test_mcp_laget_har_webbsok_avstangt_som_standard(sida: AppTest) -> None:
    """Webbsök kostar en avgift per anrop och får bara användas när arkivet inte
    räcker, så den är opt-in. RAG-läget har inga verktyg och ska inte visa valet."""
    _run(sida, MCP)
    assert _toggle(sida, WEBBSÖK).value is False

    _run(sida, RAG)
    assert WEBBSÖK not in [t.label for t in sida.sidebar.toggle]


def test_webbsok_valet_overlever_ett_varv_i_rag(sida: AppTest) -> None:
    """Samma spegling som de övriga sökvalen: widgeten avmonteras i RAG-läget och
    hade annars tyst slagits av igen."""
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()

    _run(sida, RAG)
    _run(sida, MCP)

    assert _toggle(sida, WEBBSÖK).value is True


def test_tokenpanelen_ar_kvar_i_bada_lägena(sida: AppTest) -> None:
    def usage_panel(at: AppTest) -> bool:
        return any("palme-usage" in m.value for m in at.sidebar.markdown)

    assert usage_panel(sida)
    _run(sida, MCP)
    assert usage_panel(sida)


def test_webbsok_utan_nyckel_varnar_i_sidofaltet(sida: AppTest, monkeypatch) -> None:
    """En saknad nyckel för sökmodellen gjorde webbsöket tyst dött: verktyget
    svarade bara med en feltext, inget hamnade i errors.log och gränssnittet sade
    inget. Sidofältet ska säga till, som för Jev."""
    _med_profiler(
        monkeypatch,
        {
            "Openrouter billig": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4.1-nano",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }
        },
    )
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()

    varningar = " ".join(w.value for w in sida.sidebar.warning)
    assert "OPENROUTER_API_KEY" in varningar
    assert "Openrouter billig" in varningar


def test_webbsok_tipset_beskriver_nar_sokningar_sker(sida: AppTest) -> None:
    """Tipset är operatörens enda förklaring av när sökningar sker (och därmed av
    kostnaden). Det låg kvar på den gamla, snävare regeln "sådant arkivet inte kan
    avgöra" långt efter att policyn blivit vidare."""
    _run(sida, MCP)
    help_text = _toggle(sida, WEBBSÖK).help or ""

    assert "osäker" in help_text
    assert "entydigt svar" in help_text
    assert "numera" in help_text
    assert "webbkälla" in help_text


def _tool_call(namn: str, argument: str):
    """Verktygsanrop av OpenAI-typ: loopen kräver isinstance och model_dump, och
    en handrullad klass hoppas över tyst — då mäter testet ingenting."""
    from openai.types.chat import ChatCompletionMessageFunctionToolCall
    from openai.types.chat.chat_completion_message_tool_call import Function

    return ChatCompletionMessageFunctionToolCall(
        id="call_1", type="function",
        function=Function(name=namn, arguments=argument),
    )


class _FakeMessage:
    def __init__(self, content: str | None, tool_calls: list | None) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage, finish_reason: str) -> None:
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, choice: _FakeChoice) -> None:
        self.choices = [choice]
        self.usage = None


class _FakeCompletions:
    """Svarar med verktygsanrop tills loopen ber om ett svar utan verktyg."""

    def __init__(self) -> None:
        self.utan_verktyg = 0
        self.med_verktyg = 0

    async def create(self, **kwargs):
        if kwargs.get("tool_choice") == "none":
            self.utan_verktyg += 1
            assert "tools" not in kwargs
            return _FakeResponse(
                _FakeChoice(_FakeMessage("Sammanfattning av hämtat underlag.", None), "stop")
            )
        self.med_verktyg += 1
        return _FakeResponse(
            _FakeChoice(
                _FakeMessage(None, [_tool_call("search_archive", '{"query": "ordförande"}')]),
                "tool_calls",
            )
        )


def test_tursgransen_tvingar_fram_ett_svar(sida: AppTest, monkeypatch) -> None:
    """När taket för verktygsomgångar nås ska det hämtade underlaget sammanfattas i
    stället för att bara en avklippt-notis visas — med webbsök påslaget går tio
    turer lätt åt till att söka."""
    # Verktygsanropet får inte ladda arkivindex eller cross-encoder i testet.
    monkeypatch.setattr("ask.search_hybrid", lambda *a, **k: [])
    monkeypatch.setattr("ask.rerank", lambda q, found, n: found[:n])
    fejk = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": fejk})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    _run(sida, MCP)
    sida.chat_input[0].set_value("Vem var ordförande i klubben?").run()
    assert not sida.exception, sida.exception

    svar = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "Sammanfattning av hämtat underlag." in svar
    assert "gränsen för antal verktygsomgångar" in svar
    assert fejk.utan_verktyg == 1
    # Loopen ska ha gått tills taket nåddes, inte stannat efter en eller två turer.
    assert fejk.med_verktyg > 10


# Sökvalen som sidan skickar in till strömfunktionerna (samma nycklar som
# _mcp_limits och _run_tool läser). Anropas direkt i testerna nedan, utan sidan.
SOKVAL = {
    "reranker_mode": "bge",
    "top_k": 50,
    "top_n": 6,
    "web_search": False,
    "max_turns": 15,
    "web_search_budget": 3,
}


class _Box:
    """Atrapp för st.status/st.empty som minns etiketter och markdown."""

    def __init__(self) -> None:
        self.etiketter: list[str] = []
        self.markdowns: list[str] = []

    def write(self, *args, **kwargs) -> None:
        return None

    def update(self, *args, **kwargs) -> None:
        label = kwargs.get("label")
        if label is not None:
            self.etiketter.append(str(label))

    def markdown(self, text, *args, **kwargs) -> None:
        self.markdowns.append(str(text))


def _importera_sidan():
    """Importera Utredning direkt, för att anropa strömfunktionerna utan sidan.

    Importen kör sidans modulkod i bare mode. Där lämnar ``with st.form("ask")``
    kvar en form-DeltaGenerator på Streamlits dg-stack (Streamlit poppar den bara
    inne i en riktig körning), och nästa AppTest-körning ser då sidofältets
    on_change-widgetar som "inuti ett formulär" och kastar
    StreamlitInvalidFormCallbackError. Formuläret byts därför mot en tom kontext
    under importen; spår-testerna ritar ändå inget på sidan."""
    import streamlit as st

    with mock.patch.object(st, "form", lambda *a, **k: contextlib.nullcontext()):
        import Utredning

    return Utredning


class _FakeSvar:
    """OpenAI-klient som svarar i tur och ordning ur en förbestämd lista."""

    def __init__(self, svar: list[tuple[str | None, list | None, str]]) -> None:
        self.svar = svar
        self.anrop = 0

    async def create(self, **kwargs):
        content, tool_calls, finish = self.svar[self.anrop]
        self.anrop += 1
        return _FakeResponse(_FakeChoice(_FakeMessage(content, tool_calls), finish))


def _fake_openai(fejk):
    class _FakeOpenAI:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": fejk})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

    return _FakeOpenAI


class _LoggAttrapp:
    """Samlar debugrader i stället för att skriva till webbloggen."""

    def __init__(self) -> None:
        self.rader: list[str] = []

    def debug(self, fmt: str, *args) -> None:
        self.rader.append(fmt % args if args else fmt)


def _spar(titel: str) -> dict:
    """Ett spår i verktygets form (belägg från a.txt sida 2)."""
    return {
        "title": titel,
        "why": "Lucka i det hämtade materialet.",
        "question": "Vad hände med bilen?",
        "sources": [{"source": "a.txt", "page": 2}],
    }


def _kor_openai(monkeypatch, fejk) -> tuple[dict, list[str], _Box, _Box]:
    """Kör OpenAI-vägen direkt mot en fejkklient.

    Ger (proposal, parts, statusruta, textplatshållare) så testerna kan se både
    det som returneras och det som ritades."""
    Utredning = _importera_sidan()
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    cfg = {"model": "m", "base_url": "http://x", "api_key": "k"}
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "Vem såg mördaren?"},
    ]
    proposal: dict = {}
    parts: list[str] = []
    status = _Box()
    text = _Box()
    asyncio.run(
        Utredning.stream_openai_mcp(
            status, text, parts, cfg, messages, SOKVAL, proposal=proposal
        )
    )
    return proposal, parts, status, text


def test_spar_fangas_i_openai_vagen(sida: AppTest, monkeypatch) -> None:
    """Ett propose_leads-anrop följt av ett tomt slutsvar ska ändå ge spåren.

    Modellen kan anropa verktyget som sista åtgärd och sedan svara tomt; spåren
    ska då finnas kvar för sidan att rita, och anropet får inte ge något fel."""
    import mcp_server

    Utredning = _importera_sidan()

    # Verktygssvaret får inte ladda arkivindex eller cross-encoder.
    monkeypatch.setattr(
        mcp_server, "search_with_settings",
        lambda *a, **k: 'Utdrag\nsource: "a.txt"\n',
    )
    leads = {
        "leads": [
            {
                "title": "Bilen som nämns i tre tips",
                "why": "Samma bil återkommer i tipsen 1987–1989.",
                "question": "Vem ägde bilen som nämns i tipsen?",
                "sources": [{"source": "a.txt", "page": 2}],
            }
        ],
        "counter_question": "Vilken hypotes prövar du?",
    }
    fejk = _FakeSvar(
        [
            (None, [_tool_call("search_archive", '{"query": "bilen"}')], "tool_calls"),
            (None, [_tool_call("propose_leads", json.dumps(leads))], "tool_calls"),
            # Tomt slutsvar: spåren kom före texten (Review Focus 2).
            ("", None, "stop"),
        ]
    )
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))

    cfg = {"model": "m", "base_url": "http://x", "api_key": "k"}
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "Vem såg mördaren?"},
    ]
    proposal: dict = {}
    parts: list[str] = []
    asyncio.run(
        Utredning.stream_openai_mcp(
            _Box(), _Box(), parts, cfg, messages, SOKVAL, proposal=proposal
        )
    )

    assert proposal["counter_question"] == "Vilken hypotes prövar du?"
    assert proposal["leads"][0]["sources"] == [{"source": "a.txt", "page": 2}]
    assert fejk.anrop == 3
    # Kvittensen (inte en tom verktygssvarstext) går tillbaka till modellen.
    assert messages[-2]["role"] == "tool"
    assert "spår" in messages[-2]["content"]


def test_spar_fangas_i_claude_vagen(sida: AppTest, monkeypatch) -> None:
    """Claude-vägen fångar ToolUseBlock för propose_leads och har verktyget
    tillåtet; tursgränsen höjs internt med LEADS_EXTRA_TURNS så att
    operatörens tak fortfarande gäller för sökarbetet."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock

    Utredning = _importera_sidan()

    fangade: dict = {}

    async def _fejk_query(prompt, options):
        fangade["options"] = options
        yield AssistantMessage(
            [
                ToolUseBlock(
                    id="toolu_1",
                    name="mcp__arkiv__propose_leads",
                    input={
                        "leads": [
                            {
                                "title": "Bilen som nämns i tre tips",
                                "why": "Samma bil återkommer i tipsen.",
                                "question": "Vem ägde bilen?",
                                "sources": [{"source": "a.txt", "page": 2}],
                            }
                        ],
                        "counter_question": "Vilken hypotes prövar du?",
                    },
                )
            ],
            model="m",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="ses_1",
            stop_reason="end_turn",
        )

    monkeypatch.setattr(Utredning, "query", _fejk_query)
    cfg = {"kind": "claude", "model": "m", "prices": {}}
    proposal: dict = {}
    parts: list[str] = []
    asyncio.run(
        Utredning.stream_mcp(
            "Vem såg mördaren?", _Box(), _Box(), parts, None, cfg, SOKVAL,
            proposal=proposal,
        )
    )

    assert proposal["leads"][0]["title"] == "Bilen som nämns i tre tips"
    assert proposal["counter_question"] == "Vilken hypotes prövar du?"
    assert "mcp__arkiv__propose_leads" in fangade["options"].allowed_tools
    assert fangade["options"].max_turns == 15 + Utredning.LEADS_EXTRA_TURNS


def test_text_fore_propose_leads_visas_aven_med_tomt_slutsvar(
    sida: AppTest, monkeypatch
) -> None:
    """Text som skrivits före spåren får inte försvinna när slutsvaret är tomt.

    Modellen skriver ofta hela svaret och anropar propose_leads sist; turen bär
    då både text och verktygsanrop. Utan den här vägen kastades texten."""
    fejk = _FakeSvar(
        [
            (
                "Svar före spåren.",
                [_tool_call("propose_leads", json.dumps({"leads": [_spar("Spår A")]}))],
                "tool_calls",
            ),
            ("", None, "stop"),
        ]
    )
    proposal, parts, _status, text = _kor_openai(monkeypatch, fejk)

    svar = "".join(parts)
    assert "Svar före spåren." in svar
    # Det som ritas sist ska vara samma svar som returneras.
    assert text.markdowns[-1] == svar
    assert proposal["leads"][0]["title"] == "Spår A"
    assert not any("Fel" in stycke for stycke in parts)
    assert fejk.anrop == 2


def test_sokraknaren_raknar_inte_propose_leads(sida: AppTest, monkeypatch) -> None:
    """Etiketten räknar arkivsökningar; spåren är inget sökarbete."""
    import mcp_server

    # Sökningen får inte ladda arkivindex eller cross-encoder.
    monkeypatch.setattr(
        mcp_server, "search_with_settings",
        lambda *a, **k: 'Utdrag\nsource: "a.txt"\n',
    )
    fejk = _FakeSvar(
        [
            (
                None,
                [
                    _tool_call("search_archive", '{"query": "bilen"}'),
                    _tool_call("propose_leads", json.dumps({"leads": [_spar("Spår A")]})),
                ],
                "tool_calls",
            ),
            ("", None, "stop"),
        ]
    )
    proposal, _parts, status, _text = _kor_openai(monkeypatch, fejk)

    assert proposal["leads"][0]["title"] == "Spår A"
    assert status.etiketter[-1] == "1 sökning gjord"


def test_sista_propose_leads_anropet_vinner(sida: AppTest, monkeypatch) -> None:
    """Flera anrop i samma svar: det sista gäller, utan att listor blandas."""
    fejk = _FakeSvar(
        [
            (
                None,
                [_tool_call("propose_leads", json.dumps({"leads": [_spar("Spår A")]}))],
                "tool_calls",
            ),
            (
                None,
                [_tool_call("propose_leads", json.dumps({"leads": [_spar("Spår B")]}))],
                "tool_calls",
            ),
            ("", None, "stop"),
        ]
    )
    proposal, _parts, _status, _text = _kor_openai(monkeypatch, fejk)

    assert [spår["title"] for spår in proposal["leads"]] == ["Spår B"]


def test_andra_propose_leads_anropet_loggas_aven_nar_forsta_gav_noll_spar(
    sida: AppTest, monkeypatch
) -> None:
    """Debugraden ska styras av att ett anrop skett, inte av vad det gav.

    Ett första anrop kan normaliseras till noll spår; nästa anrop ska ändå
    loggas som ett över- skrivande anrop."""
    Utredning = _importera_sidan()
    logg = _LoggAttrapp()
    monkeypatch.setattr(Utredning, "_log", logg)
    fejk = _FakeSvar(
        [
            (None, [_tool_call("propose_leads", '{"leads": []}')], "tool_calls"),
            (
                None,
                [_tool_call("propose_leads", json.dumps({"leads": [_spar("Spår B")]}))],
                "tool_calls",
            ),
            ("", None, "stop"),
        ]
    )
    proposal, _parts, _status, _text = _kor_openai(monkeypatch, fejk)

    assert proposal["leads"][0]["title"] == "Spår B"
    assert any("flera propose_leads-anrop" in rad for rad in logg.rader)


def test_propose_leads_ar_med_utan_webbsok(sida: AppTest) -> None:
    """Verktyget finns alltid i utredningsläget, oberoende av webbsök."""
    Utredning = _importera_sidan()
    navn = [verktyg["function"]["name"] for verktyg in Utredning.active_tools(False)]

    assert "propose_leads" in navn
    assert "web_search" not in navn


TURER = "Verktygsomgångar (max)"
BUDGET = "Högst antal webbsökningar per fråga"


def test_granserna_ligger_i_sokinstallningarna(sida: AppTest) -> None:
    """Tursgränsen och webbsöksbudgeten är operatörens kostnadsrattar: 15 omgångar
    och 3 betalda sökningar som standard. Budgeten är grå när webbsök är av."""
    _run(sida, MCP)

    assert _slider(sida, TURER).value == 15
    assert (_slider(sida, TURER).min, _slider(sida, TURER).max) == (3, 30)
    assert _slider(sida, BUDGET).value == 3
    assert (_slider(sida, BUDGET).min, _slider(sida, BUDGET).max) == (1, 20)
    assert _slider(sida, BUDGET).disabled is True

    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()
    assert _slider(sida, BUDGET).disabled is False


def test_granserna_overlever_ett_varv_i_rag(sida: AppTest) -> None:
    _run(sida, MCP)
    # Budgeten är inaktiverad tills webbsöket slås på.
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()
    _slider(sida, TURER).set_value(25)
    _slider(sida, BUDGET).set_value(7)
    sida.run()

    _run(sida, RAG)
    _run(sida, MCP)

    assert _slider(sida, TURER).value == 25
    assert _slider(sida, BUDGET).value == 7


def test_granserna_visas_inte_i_rag_laget(sida: AppTest) -> None:
    labels = _sidebar_labels(sida)
    assert TURER not in labels
    assert BUDGET not in labels


class _FakeWebSearchClient:
    """Modellen ber om webbsök i varje tur; testet ser när verktyget försvinner."""

    def __init__(self) -> None:
        self.verktygslistor: list[list[str]] = []

    async def create(self, **kwargs):
        self.verktygslistor.append(
            [t["function"]["name"] for t in kwargs.get("tools") or []]
        )
        if kwargs.get("tool_choice") == "none":
            return _FakeResponse(
                _FakeChoice(_FakeMessage("Sammanfattning utan fler sökningar.", None), "stop")
            )
        return _FakeResponse(
            _FakeChoice(
                _FakeMessage(None, [_tool_call("web_search", '{"query": "x"}')]),
                "tool_calls",
            )
        )


def test_webbsok_verktyget_forsvinner_nar_budgeten_ar_slut(
    sida: AppTest, monkeypatch
) -> None:
    """Taket ska inte bara stoppa anropen — modellen ska sluta se verktyget, så
    den inte fortsätter be om något den ändå inte får."""
    monkeypatch.setattr(
        "mcp_server.search_web", lambda *a, **k: ("webbsvar", None)
    )
    fejk = _FakeWebSearchClient()

    class _FakeOpenAI:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": fejk})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: _FakeOpenAI())
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()
    _slider(sida, BUDGET).set_value(2)
    sida.run()
    sida.chat_input[0].set_value("Vem var ordförande?").run()
    assert not sida.exception, sida.exception

    alla_namn = [namn for lista in fejk.verktygslistor for namn in lista]
    med_webbsok = [i for i, lista in enumerate(fejk.verktygslistor) if "web_search" in lista]
    assert med_webbsok == [0, 1], "web_search erbjuds exakt budgeten ut"
    assert len(fejk.verktygslistor) > 2, "loopen fortsätter efter att budgeten är slut"
    assert "search_archive" in alla_namn, "arkivverktygen ska finnas kvar"
    svar = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "Sammanfattning utan fler sökningar." in svar


SÖKMODELL = "Sökmodell"


def _med_profiler(
    monkeypatch,
    profiler: dict,
    *,
    nycklar: tuple[str, ...] = (),
    standard: str = "",
) -> None:
    """Operatörens profiler i testet: sökmodellslistan byggs ur dem.

    ``standard`` är förstahandsvalet från LLM-konfiguratorn (Admin).
    """
    monkeypatch.setattr("config.load_search_default", lambda: standard)
    for env in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    for nyckel in nycklar:
        monkeypatch.setenv(nyckel, "test")
    monkeypatch.setattr(
        "config.load_all", lambda: {"profiles": profiler, "default": next(iter(profiler))}
    )
    monkeypatch.setattr(
        "config.resolve_runtime_profile",
        lambda profil, katalog, **kw: {
            "kind": profil.get("provider") or "openai",
            "backend_name": profil.get("backend_name") or "Test",
            "model": profil.get("model") or "test-modell",
            "base_url": profil.get("base_url") or "",
            "api_key_env": profil.get("api_key_env") or "",
            "api_key": os.environ.get(profil.get("api_key_env") or "", ""),
            "prices": {},
        },
    )


def test_sokmodellen_visar_konfigurerade_profiler(sida: AppTest, monkeypatch) -> None:
    """Ett val i stället för två: listan är operatörens egna LLM-profiler, och
    profilens leverantör avgör sök-API:et. Profiler utan sök-API listas inte."""
    _med_profiler(
        monkeypatch,
        {
            "Openrouter billig": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4.1-nano",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            "Deepseek flash": {
                "backend_name": "DeepSeek",
                "model": "deepseek-v4-flash",
                "base_url": "https://api.deepseek.com/v1",
                "api_key_env": "DEEPSEEK_API_KEY",
            },
            "ollama": {"backend_name": "Ollama (lokal)", "base_url": "http://localhost:11434/v1"},
        },
        nycklar=("OPENROUTER_API_KEY",),
    )
    _run(sida, MCP)

    val = _selectbox(sida, SÖKMODELL)
    # Ingen "auto": valet är ett namn operatören känner igen.
    assert val.options == ["Openrouter billig — OpenRouter, openai/gpt-4.1-nano"]
    assert val.value == "Openrouter billig — OpenRouter, openai/gpt-4.1-nano"
    assert val.disabled is True

    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()
    assert _selectbox(sida, SÖKMODELL).disabled is False


def test_konfigurerad_profil_utan_nyckel_markeras(sida: AppTest, monkeypatch) -> None:
    """En profil vars nyckel saknas syns — med nyckeln namngiven — i stället för
    att se ut som ett fungerande val."""
    _med_profiler(
        monkeypatch,
        {
            "Openrouter billig": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4.1-nano",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            "Openai profil": {
                "backend_name": "OpenAI",
                "model": "gpt-4o-mini",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
            },
        },
        nycklar=("OPENROUTER_API_KEY",),
    )
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()

    assert _selectbox(sida, SÖKMODELL).options == [
        "Openrouter billig — OpenRouter, openai/gpt-4.1-nano",
        "Openai profil — OpenAI, gpt-4o-mini (OPENAI_API_KEY saknas)",
    ]


def test_standardvalet_ligger_forst_och_ar_forvalt(
    sida: AppTest, monkeypatch, tmp_path
) -> None:
    """En sparad standard ska vara förstahandsval i listan — det är hela poängen
    med att kunna sätta den."""
    _med_profiler(
        monkeypatch,
        {
            "A openrouter": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4.1-nano",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
            "B openrouter": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4o-mini",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            },
        },
        nycklar=("OPENROUTER_API_KEY",),
        standard="B openrouter",
    )
    _run(sida, MCP)

    val = _selectbox(sida, SÖKMODELL)
    assert val.options[0] == "B openrouter — OpenRouter, openai/gpt-4o-mini"
    assert val.value == val.options[0]
    assert len(val.options) == 2
    assert _toggle(sida, WEBBSÖK).value is False
    # Kryssrutan hör inte hit — förstahandsvalet ställs in i LLM-konfiguratorn.
    assert not any(c.label == "Standardval för webbsök" for c in sida.sidebar.checkbox)


def test_utan_sokbar_profil_visas_ingen_vald_sokmodell(sida: AppTest, monkeypatch) -> None:
    """Saknas en LLM vars leverantör kan söka (den lokala testprofilen kan inte)
    ska rutan säga det i stället för att erbjuda ett dött val."""
    for env in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()

    val = _selectbox(sida, SÖKMODELL)
    assert val.options == ["Ingen konfigurerad LLM kan söka"]
    assert val.disabled is True


def test_sokmodellen_overlever_ett_varv_i_rag(sida: AppTest, monkeypatch) -> None:
    """Samma spegling som de övriga sökvalen: widgeten avmonteras i RAG-läget."""
    _med_profiler(
        monkeypatch,
        {
            "Openrouter billig": {
                "backend_name": "OpenRouter",
                "model": "openai/gpt-4.1-nano",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key_env": "OPENROUTER_API_KEY",
            }
        },
        nycklar=("OPENROUTER_API_KEY",),
    )
    _run(sida, MCP)
    _toggle(sida, WEBBSÖK).set_value(True)
    sida.run()
    etikett = "Openrouter billig — OpenRouter, openai/gpt-4.1-nano"
    _selectbox(sida, SÖKMODELL).set_value(etikett)
    sida.run()

    _run(sida, RAG)
    _run(sida, MCP)

    assert _selectbox(sida, SÖKMODELL).value == etikett


def test_chat_turn_behaller_ursprunglig_profil_efter_byte(
    sida: AppTest, monkeypatch,
) -> None:
    """En sparad tur i chatthistoriken ska komma ihåg vilken profil som
    faktiskt genererade svaret, inte sidofältets nuvarande val. Regression:
    svaret genererades med profil A, operatören bytte sedan till B utan att
    ställa en ny fråga — turen ska fortfarande visa/spara A."""
    monkeypatch.setattr("ask.search_hybrid", lambda *a, **k: [])
    monkeypatch.setattr("ask.rerank", lambda q, found, n: found[:n])

    class _FakeCompletionsA:
        async def create(self, **kwargs):
            return _FakeResponse(
                _FakeChoice(_FakeMessage("Svar från profil A.", None), "stop")
            )

    class _FakeOpenAI:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": _FakeCompletionsA()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", _FakeOpenAI)
    _med_profiler(
        monkeypatch,
        {
            "A": {"model": "model-a", "backend_name": "Profil A"},
            "B": {"model": "model-b", "backend_name": "Profil B"},
        },
    )
    _run(sida, MCP)
    sida.chat_input[0].set_value("Vem var ordförande?").run()
    assert not sida.exception, sida.exception

    assert sida.session_state.chat_history[-1]["model"] == "model-a"
    assert sida.session_state.chat_history[-1]["backend_name"] == "Profil A"

    # Byt profil i sidofältet UTAN att ställa en ny fråga.
    _selectbox(sida, "LLM-profil").set_value("B").run()
    assert not sida.exception, sida.exception

    # Den redan sparade turen ska fortfarande peka på profilen som genererade
    # den, inte det nya sidofältsvalet.
    assert sida.session_state.chat_history[-1]["model"] == "model-a"
    assert sida.session_state.chat_history[-1]["backend_name"] == "Profil A"


def test_openai_chat_history_stays_bounded(sida: AppTest, monkeypatch) -> None:
    """Regression: openai_chat_messages ska inte växa obegränsat i en lång
    MCP-konversation med en OpenAI-kompatibel backend — annars växer kostnaden
    och till sist nås modellens kontextgräns."""
    monkeypatch.setattr("ask.search_hybrid", lambda *a, **k: [])
    monkeypatch.setattr("ask.rerank", lambda q, found, n: found[:n])

    class _FakeMessageObj:
        def __init__(self, content):
            self.content = content
            self.tool_calls = None

    class _FakeChoiceObj:
        def __init__(self, content):
            self.message = _FakeMessageObj(content)
            self.finish_reason = "stop"

    class _FakeResponseObj:
        def __init__(self, content):
            self.choices = [_FakeChoiceObj(content)]
            self.usage = None

    class _FakeCompletions:
        async def create(self, **kwargs):
            return _FakeResponseObj("Ett kort svar.")

    class _FakeOpenAI:
        def __init__(self, *a, **k) -> None:
            self.chat = type("Chat", (), {"completions": _FakeCompletions()})()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

    monkeypatch.setattr("openai.AsyncOpenAI", lambda *a, **k: _FakeOpenAI())
    _run(sida, MCP)

    for i in range(25):
        sida.chat_input[0].set_value(f"Fråga nummer {i}").run()
        assert not sida.exception, sida.exception

    messages = sida.session_state.openai_chat_messages
    assert messages[0]["role"] == "system"
    assert len(messages) < 25 * 2
    assert len(sida.session_state.chat_history) == 25 * 2


def test_saknat_index_forklarar_hur_det_byggs(monkeypatch, tmp_path) -> None:
    """En ny klon har inget index — sidan ska säga vad som ska köras, inte krascha."""
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr("config.CONFIG_FILE", tmp_path / "llm_config.json")
    monkeypatch.setattr("lancedb.connect", lambda *a, **k: _FakeDB(tables=()))
    laddad: list[int] = []
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer",
        lambda *a, **k: laddad.append(1) or _FakeModel(),
    )
    at = AppTest.from_file(str(UTREDNING), default_timeout=120).run()

    assert not at.exception, at.exception
    text = " ".join(i.value for i in at.info)
    assert "Sökindexet saknas" in text
    assert "./install.sh" in text
    assert not laddad, "embedding-modellen ska inte laddas utan index"


def test_tomt_index_startar_sidan_med_hjalptext(monkeypatch, tmp_path) -> None:
    """install.sh skapar en tom tabell: sidan ska fungera och säga hur den fylls."""
    import streamlit as st

    st.cache_resource.clear()  # load() är cachad över testerna i samma process
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr("config.CONFIG_FILE", tmp_path / "llm_config.json")
    monkeypatch.setattr("lancedb.connect", lambda *a, **k: _FakeDB(rows=0))
    monkeypatch.setattr(
        "sentence_transformers.SentenceTransformer", lambda *a, **k: _FakeModel()
    )
    at = _run(AppTest.from_file(str(UTREDNING), default_timeout=120), RAG)

    text = " ".join(i.value for i in at.info)
    assert "Sökindexet är tomt" in text
    assert "./run_pipeline.sh --test 5" in text
    assert _slider(at, TOP_K).value == 50, "sidan ritas i övrigt som vanligt"
    st.cache_resource.clear()  # lämna inte den tomma tabellen åt nästa test


def test_fyllt_index_visar_ingen_hjalptext(sida: AppTest) -> None:
    assert not any("Sökindexet" in i.value for i in sida.info)


# Spårlistan i utredningsläget: session_state["leads"] ägs av sidan och
# "a.txt" är en källa som aldrig setts i ett verktygssvar, så belägget ritas som
# text (ingen gissad PDF-länk).
SPÅR = {
    "id": 1,
    "turn": 1,
    "status": "föreslagen",
    "title": "Bilen i tre tips",
    "why": "Nämns 1987–1989",
    "question": "Vad vet vi om bilen?",
    "sources": [{"source": "a.txt", "page": 2}],
}


def _med_spar(sida: AppTest, *, status: str = "föreslagen") -> None:
    """Förifyll en konversation där assistenten föreslagit ett spår."""
    sida.session_state["leads"] = [{**SPÅR, "status": status}]
    sida.session_state["chat_history"] = [
        {"role": "user", "text": "Fråga", "sources": []},
        {
            "role": "assistant",
            "text": "Svar",
            "sources": [],
            "lead_ids": [1],
            "counter_question": None,
        },
    ]


class _FakeSvarLoggande(_FakeSvar):
    """Svarar ur en lista och minns meddelandena modellen fick se."""

    def __init__(self, svar: list[tuple[str | None, list | None, str]]) -> None:
        super().__init__(svar)
        self.senaste_messages: list[dict] = []

    async def create(self, **kwargs):
        self.senaste_messages = kwargs["messages"]
        return await super().create(**kwargs)


def test_spar_och_motfraga_ritas_ur_historiken(sida: AppTest) -> None:
    """Spårkorten och motfrågan ritas ur chat_history vid varje rerun — inte
    bara under strömningen, som hade tappat dem så fort sidan ritades om."""
    _run(sida, MCP)
    sida.session_state["leads"] = [dict(SPÅR)]
    sida.session_state["chat_history"] = [
        {"role": "user", "text": "Fråga", "sources": []},
        {"role": "assistant", "text": "Svar", "sources": [], "lead_ids": [1],
         "counter_question": "Vilken hypotes prövar du?"}]
    sida.run()
    assert not sida.exception, sida.exception

    text = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "Bilen i tre tips" in text and "Nämns 1987–1989" in text
    assert any("Vilken hypotes prövar du?" in i.value for i in sida.info)
    assert [b for b in sida.button if b.label == "Gräv"]


def test_grav_skickar_fragan_och_markerar_spaaret(sida: AppTest, monkeypatch) -> None:
    """Gräv skickar spårets fråga exakt en gång och visar spåret som grävt.

    Fyra identiska svar i listan så en dubbelsändning syns som två anrop i
    stället för att krascha fejkklienten."""
    fejk = _FakeSvar([("Svar om bilen.", None, "stop")] * 4)
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    _run(sida, MCP)
    _med_spar(sida)
    sida.run()
    assert [b for b in sida.button if b.label == "Gräv"]

    next(b for b in sida.button if b.label == "Gräv").click().run()
    assert not sida.exception, sida.exception

    assert sida.session_state["leads"][0]["status"] == "grävd"
    anvandare = [t["text"] for t in sida.session_state["chat_history"] if t["role"] == "user"]
    assert anvandare[-1] == "Vad vet vi om bilen?"
    assert anvandare.count("Vad vet vi om bilen?") == 1          # ingen dubbelsändning
    assert not [b for b in sida.button if b.label == "Gräv"]       # knappen nu "Grävt"
    assert fejk.anrop == 1

    sida.run()                                                     # en extra rerun
    anvandare = [t["text"] for t in sida.session_state["chat_history"] if t["role"] == "user"]
    assert anvandare.count("Vad vet vi om bilen?") == 1
    assert fejk.anrop == 1


def test_sammanfattningen_skickas_men_visas_inte(sida: AppTest, monkeypatch) -> None:
    """Spårlistan går med till modellen (så spår inte upprepas) men syns aldrig
    i chatten: chat_history bär bara operatörens egen text."""
    fejk = _FakeSvarLoggande([("Svar på nästa fråga.", None, "stop")] * 4)
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    _run(sida, MCP)
    _med_spar(sida)
    sida.run()

    sida.chat_input[0].set_value("Nästa fråga").run()
    assert not sida.exception, sida.exception

    senaste_user = next(
        m["content"]
        for m in reversed(fejk.senaste_messages)
        if m.get("role") == "user"
    )
    assert senaste_user.startswith("[Spår hittills i konversationen]")
    assert senaste_user.endswith("Nästa fråga")
    assert sida.session_state["chat_history"][-2]["text"] == "Nästa fråga"
    text = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "[Spår hittills i konversationen]" not in text


def test_ny_konversation_tommer_spaaren(sida: AppTest) -> None:
    """En ny konversation ska inte ärva den gamlas spår eller kända källor —
    annars kan gamla spår föreslås igen och sammanfattningen beskriva fel sak."""
    _run(sida, MCP)
    sida.session_state["leads"] = [dict(SPÅR)]
    sida.session_state["lead_sources"] = {"a.txt"}
    sida.run()
    next(b for b in sida.button if b.label == "Ny konversation").click().run()
    assert sida.session_state["leads"] == [] and sida.session_state["lead_sources"] == set()
    assert sida.session_state["pending_question"] is None


# Claude-profil: sidan väljer gren på backendens ``kind`` (config.resolve_runtime_profile),
# och grenen kräver en Claude-nyckel i miljön redan vid sidladdningen.
CLAUDE_PROFIL = {
    "kind": "claude",
    "backend_name": "Claude",
    "model": "test-modell",
    "base_url": "",
    "prices": {},
}


def _claude_svar(anrop: list[str]):
    """Async-generator som spelar Claudes meddelandeström.

    Verktygssvaret bär en ``source``-rad så sidan känner igen spårets belägg,
    och ``ResultMessage`` bär session_id som sidan ska spara för resume.
    Varje anrop registreras med sin prompt (frågan modellen får se)."""
    from claude_agent_sdk import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    async def _query(prompt, options):
        anrop.append(prompt)
        yield UserMessage(
            [ToolResultBlock(tool_use_id="toolu_0", content='Utdrag\nsource: "a.txt"\n')]
        )
        yield AssistantMessage([TextBlock("Svar från Claude.")], model="test-modell")
        yield AssistantMessage(
            [
                ToolUseBlock(
                    id="toolu_1",
                    name="mcp__arkiv__propose_leads",
                    input={
                        "leads": [
                            {
                                "title": "Bilen i tre tips",
                                "why": "Nämns 1987–1989",
                                "question": "Vad vet vi om bilen?",
                                "sources": [{"source": "a.txt", "page": 2}],
                            }
                        ],
                        "counter_question": None,
                    },
                )
            ],
            model="test-modell",
        )
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="ses-ny",
            stop_reason="end_turn",
        )

    return _query


def test_claude_grenen_registrerar_spar_och_skickar_sammanfattning(
    sida: AppTest, monkeypatch
) -> None:
    """Claude-grenen i den sammanslagna _render_mcp_tab.

    Grenen packar upp fem värden ur stream_mcp_to_string, sparar session_id
    för resume, registrerar spåret ur proposal och skickar spårsammanfattningen
    som prefix på nästa fråga — chatten ska bara visa operatörens text."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "test")
    monkeypatch.setattr(
        "config.resolve_runtime_profile",
        lambda profil, katalog, **kw: dict(CLAUDE_PROFIL),
    )
    anrop: list[str] = []
    # Sidan körs av AppTest i en färsk modul, så patchen måste träffa SDK:ts
    # ``query`` (som sidan importerar om vid varje körning) — inte den separat
    # importerade Utredning-modulens kopia av stream_mcp_to_string.
    monkeypatch.setattr("claude_agent_sdk.query", _claude_svar(anrop))
    _run(sida, MCP)

    sida.chat_input[0].set_value("Vem ägde bilen?").run()
    assert not sida.exception, sida.exception

    assert sida.session_state["mcp_session_id"] == "ses-ny"
    spar = sida.session_state["leads"]
    assert [s["title"] for s in spar] == ["Bilen i tre tips"]
    assert spar[0]["status"] == "föreslagen"
    assert spar[0]["sources"] == [{"source": "a.txt", "page": 2}]
    text = " ".join(m.value for m in sida.markdown if isinstance(m.value, str))
    assert "Bilen i tre tips" in text and "Nämns 1987–1989" in text
    assert [b for b in sida.button if b.label == "Gräv"]

    next(b for b in sida.button if b.label == "Gräv").click().run()
    assert not sida.exception, sida.exception

    assert sida.session_state["leads"][0]["status"] == "grävd"
    # Två anrop totalt: den skrivna frågan och Gräv-frågan — ingen dubbelsändning.
    assert len(anrop) == 2
    assert anrop[1].startswith("[Spår hittills i konversationen]")
    assert anrop[1].endswith("Vad vet vi om bilen?")
    skrivna = [
        t["text"] for t in sida.session_state["chat_history"] if t["role"] == "user"
    ]
    assert skrivna == ["Vem ägde bilen?", "Vad vet vi om bilen?"]
    assert not any("[Spår hittills" in t for t in skrivna)

    sida.run()
    assert len(anrop) == 2


class _FakeSvarSomFaller(_FakeSvarLoggande):
    """Klient vars anrop alltid faller (nätverk, 401) men som minns meddelandena."""

    async def create(self, **kwargs):
        self.senaste_messages = kwargs["messages"]
        self.anrop += 1
        raise RuntimeError("401 Unauthorized")


def test_misslyckat_grav_lamnar_sparet_foreslaget(sida: AppTest, monkeypatch) -> None:
    """Ett Gräv vars anrop faller får inte låsa spåret som grävt: knappen ska gå
    att klicka igen. Sammanfattningen till modellen visar ändå spåret som grävt
    under anropet, så modellen inte föreslår det igen i samma svar."""
    fejk = _FakeSvarSomFaller([])
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    _run(sida, MCP)
    _med_spar(sida)
    sida.run()

    next(b for b in sida.button if b.label == "Gräv").click().run()
    assert not sida.exception, sida.exception

    assert fejk.anrop == 1
    assert sida.session_state["leads"][0]["status"] == "föreslagen"
    assert sida.session_state["pending_lead_id"] is None
    assert [b for b in sida.button if b.label == "Gräv"]
    senaste_user = next(
        m["content"] for m in reversed(fejk.senaste_messages) if m.get("role") == "user"
    )
    assert "1. (grävd) Bilen i tre tips" in senaste_user


def test_skriven_fraga_slapper_koad_grav(sida: AppTest, monkeypatch) -> None:
    """En skriven fråga i samma körning som en köad Gräv-fråga går före, och
    spåret markeras då inte som grävt."""
    fejk = _FakeSvar([("Svar.", None, "stop")] * 2)
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    _run(sida, MCP)
    _med_spar(sida)
    sida.session_state["pending_question"] = "Vad vet vi om bilen?"
    sida.session_state["pending_lead_id"] = 1
    sida.chat_input[0].set_value("Egen fråga").run()
    assert not sida.exception, sida.exception

    skrivna = [t["text"] for t in sida.session_state["chat_history"] if t["role"] == "user"]
    assert skrivna[-1] == "Egen fråga" and "Vad vet vi om bilen?" not in skrivna
    assert sida.session_state["leads"][0]["status"] == "föreslagen"
    assert fejk.anrop == 1


def test_textdelar_fogas_med_blankrad(sida: AppTest, monkeypatch) -> None:
    """Text före spåren och ett icke-tomt slutsvar får en blankrad emellan —
    annars blev skarven "…Nr 12.Spåren visas…"."""
    fejk = _FakeSvar(
        [
            (
                "Svar före spåren.",
                [_tool_call("propose_leads", json.dumps({"leads": [_spar("Spår A")]}))],
                "tool_calls",
            ),
            ("Klart.", None, "stop"),
        ]
    )
    _proposal, parts, _status, text = _kor_openai(monkeypatch, fejk)
    assert "".join(parts) == "Svar före spåren.\n\nKlart."
    assert text.markdowns[-1] == "Svar före spåren.\n\nKlart."


def test_reserverade_turer_bara_for_sparen(sida: AppTest, monkeypatch) -> None:
    """Efter operatörens tak får modellen bara propose_leads, så taket gäller för
    sökarbetet; en sökning som ändå begärs körs inte."""
    import mcp_server

    sokningar: list[str] = []
    monkeypatch.setattr(
        mcp_server,
        "search_with_settings",
        lambda q, **k: sokningar.append(q) or 'Utdrag\nsource: "a.txt"\n',
    )

    class _AlltidSok(_FakeSvar):
        def __init__(self) -> None:
            super().__init__([])
            self.verktyg_per_anrop: list[list[str]] = []

        async def create(self, **kwargs):
            self.anrop += 1
            if kwargs.get("tool_choice") == "none":
                return _FakeResponse(_FakeChoice(_FakeMessage("Sammanfattning.", None), "stop"))
            self.verktyg_per_anrop.append(
                [t["function"]["name"] for t in kwargs.get("tools") or []]
            )
            return _FakeResponse(
                _FakeChoice(
                    _FakeMessage(None, [_tool_call("search_archive", '{"query": "x"}')]),
                    "tool_calls",
                )
            )

    fejk = _AlltidSok()
    Utredning = _importera_sidan()
    monkeypatch.setattr("openai.AsyncOpenAI", _fake_openai(fejk))
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}]
    sokval = {**SOKVAL, "max_turns": 3}
    asyncio.run(
        Utredning.stream_openai_mcp(
            _Box(), _Box(), [], {"model": "m", "base_url": "http://x", "api_key": "k"},
            messages, sokval, proposal={},
        )
    )
    assert len(sokningar) == 3
    assert len(fejk.verktyg_per_anrop) == 3 + Utredning.LEADS_EXTRA_TURNS
    assert all("search_archive" in v for v in fejk.verktyg_per_anrop[:3])
    assert all(v == ["propose_leads"] for v in fejk.verktyg_per_anrop[3:])


def test_spårbelagg_lankas_eller_visas_som_escapad_text(monkeypatch, tmp_path) -> None:
    """Ett belägg med PDF blir en länk med sida; utan PDF blir det escapad text —
    aldrig en gissad länk, och källnamnet får aldrig tolkas som HTML."""
    Utredning = _importera_sidan()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(
        Utredning._casebook_ui,
        "find_pdf",
        lambda root, source: pdf if source == "a.txt" else None,
    )

    lank = Utredning._lead_source_markdown({"source": "a.txt", "page": 2})
    assert lank.startswith("<a ") and "&page=2" in lank and "[a.txt, sida 2]" in lank

    text = Utredning._lead_source_markdown({"source": "<b>x</b>.txt", "page": 1})
    assert "<a " not in text and "<b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;.txt" in text
