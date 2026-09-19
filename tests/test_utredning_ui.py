"""Beteendetester för Utredning-sidans lägesval och sökval (headless AppTest).

Streamlit raderar widgetstate för widgets som inte ritas i en körning, så båda
lärnas sökval måste speglas för att överleva ett lägesbyte: RAG:s reranker,
top-K/N, facetter och fuzzy, och MCP:s reranker, top-K/N och webbsök — ingen av
sektionerna ritas medan det andra läget är valt. Testerna kör sidan headless med
tunga beroenden utbytta (vektor-db, embedding-modell och state-db) och
kontrollerar beteendet i stället för källkoden.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

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
    def count_rows(self) -> int:
        return 1234


class _FakeDB:
    def open_table(self, name: str) -> _FakeTable:
        return _FakeTable()


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
    _slider(sida, TRÖSKEL).set_value(0.85)
    _toggle(sida, FUZZY).set_value(True)
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
