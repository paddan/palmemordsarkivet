"""Beteendetester för Utredning-sidans lägesval och RAG-sökval (headless AppTest).

Streamlit raderar widgetstate för widgets som inte ritas i en körning, så
RAG-sökvalen (reranker, top-K/top-N, facetter, fuzzy) måste speglas för att
överleva ett besök i MCP-läget — där ritas de inte alls. Testerna kör sidan
headless med tunga beroenden utbytta (vektor-db, embedding-modell och state-db)
och kontrollerar beteendet i stället för källkoden.
"""

from __future__ import annotations

import logging
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


def test_mcp_laget_visar_inte_sokinställningarna(sida: AppTest) -> None:
    _run(sida, MCP)
    assert sida.chat_input, "MCP-kroppen ritades inte (tidig st.stop()?)"
    labels = _sidebar_labels(sida)
    assert "LLM-profil" in labels
    assert RERANKER not in labels
    assert TOP_K not in labels
    assert FUZZY not in [t.label for t in sida.sidebar.toggle]


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
    assert RERANKER not in _sidebar_labels(sida)

    _run(sida, RAG)
    assert _slider(sida, TOP_K).value == 33
    assert _slider(sida, TOP_N).value == 9
    assert _slider(sida, TRÖSKEL).value == pytest.approx(0.85)
    assert _toggle(sida, FUZZY).value is True
    assert _selectbox(sida, RERANKER).value == "Ingen"


def test_tokenpanelen_ar_kvar_i_bada_lägena(sida: AppTest) -> None:
    def usage_panel(at: AppTest) -> bool:
        return any("palme-usage" in m.value for m in at.sidebar.markdown)

    assert usage_panel(sida)
    _run(sida, MCP)
    assert usage_panel(sida)
