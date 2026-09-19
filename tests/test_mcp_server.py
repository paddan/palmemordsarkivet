"""Tester för mcp_server.get_page — sidhämtning och sökvägsvalidering."""

from __future__ import annotations

import json
from pathlib import Path

import ask
import mcp_server
import pytest


@pytest.fixture(autouse=True)
def _full_webbsoksbudget() -> None:
    """Budgeträknaren är modulstate (per fråga i drift, nollställs av
    Utredning-sidan). Testerna ska starta med full budget i stället för att ärva
    varandras förbrukning."""
    mcp_server.set_web_search_budget(mcp_server.WEB_SEARCH_BUDGET_MAX)
    mcp_server.set_web_search_provider(None)
    mcp_server.set_web_search_profile(None)


@pytest.mark.parametrize("rerank", [True, False])
def test_search_result_can_open_exact_source(text_dir, monkeypatch, rerank) -> None:
    """Ett långt filnamn med citattecken ska gå från sökträff till sidläsning."""
    source = 'Pol-1986-03-01_A123-4_' + 'Lång titel ' * 8 + '"Åke".txt'
    (text_dir / source).write_text("första sidan\fverifierad andra sida", encoding="utf-8")
    hits = [{"nr": "Pol-1986-03-01_A123-4", "page": 2, "titel": "Lång titel " * 8,
             "source": source, "text": "utdrag"}]
    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())
    monkeypatch.setattr(ask, "search_hybrid", lambda *args: hits)
    monkeypatch.setattr(ask, "rerank", lambda q, found, n: found[:n])
    result = mcp_server.search_archive("fråga", rerank=rerank)
    source_line = next(line for line in result.splitlines() if line.startswith("source: "))
    exact_source = json.loads(source_line.removeprefix("source: "))
    assert exact_source == source
    assert "[Nr Pol-1986-03-01_A123-4, sida 2," in result
    assert "verifierad andra sida" in mcp_server.get_page(exact_source, 2)
    assert "source: " not in ask.format_context(hits)


@pytest.fixture()
def text_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "text"
    d.mkdir()
    monkeypatch.setattr(mcp_server, "_text_dir", d)
    return d


def test_get_page_returns_requested_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("sida ett\fsida två\fsida tre",
                                      encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "sida två" in out
    assert "sida ett" not in out
    assert "[doc.txt, sida 2]" in out


def test_get_page_out_of_range(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("bara en sida", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 5)
    assert "finns inte" in out
    assert "har 1 sidor" in out


def test_get_page_missing_file(text_dir: Path) -> None:
    out = mcp_server.get_page("saknas.txt", 1)
    assert "Hittade inte" in out


def test_get_page_empty_page(text_dir: Path) -> None:
    (text_dir / "doc.txt").write_text("text\f\ftext", encoding="utf-8")
    out = mcp_server.get_page("doc.txt", 2)
    assert "är tom" in out


def test_get_page_rejects_path_traversal(text_dir: Path) -> None:
    secret = text_dir.parent / "hemlig.txt"
    secret.write_text("hemligt", encoding="utf-8")
    out = mcp_server.get_page("../hemlig.txt", 1)
    assert "hemligt" not in out


def test_clamp_result_limits_handles_minimums_and_bad_values() -> None:
    assert mcp_server.clamp_result_limits(0, -10) == (5, 1)
    assert mcp_server.clamp_result_limits("många", None) == (50, 6)


def test_search_archive_clamps_large_result_limits(monkeypatch) -> None:
    seen: dict[str, int] = {}
    hits = [
        {"nr": "1", "page": 1, "titel": "Titel", "text": f"träff {i}"}
        for i in range(60)
    ]
    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())

    def fake_search_hybrid(table, model, query: str, top_k: int) -> list[dict]:
        seen["top_k"] = top_k
        return hits

    def fake_rerank(query: str, found: list[dict], top_n: int) -> list[dict]:
        seen["top_n"] = top_n
        return found[:top_n]

    def fake_format_context(found: list[dict], *, include_source: bool = False) -> str:
        assert include_source
        seen["formatted"] = len(found)
        return "kontext"

    monkeypatch.setattr(ask, "search_hybrid", fake_search_hybrid)
    monkeypatch.setattr(ask, "rerank", fake_rerank)
    monkeypatch.setattr(ask, "format_context", fake_format_context)

    mcp_server.search_archive("fråga", top_k=9999, top_n=9999)

    assert seen == {"top_k": 50, "top_n": 15, "formatted": 15}


def test_topp_k_standard_ar_50() -> None:
    """Höjt från 20 efter mätningen i docs/jev-reranker-pilot.md: hybrid + BGE
    gav 37/60 belägg i topp 6 med 20 kandidater och 47/60 med 50."""
    assert mcp_server.TOP_K_DEFAULT == 50


def _fanga_anrop(monkeypatch, hits: list[dict]) -> dict:
    """Fejka sökning och rerankning; returnera vad de anropades med."""
    seen: dict = {}

    def fake_search(table, model, query, top_k):
        seen["vektor_top_k"] = top_k
        return hits

    def fake_hybrid(table, model, query, top_k):
        seen["top_k"] = top_k
        return hits

    def fake_rerank(query, found, top_n):
        seen["bge_top_n"] = top_n
        return found[:top_n]

    def fake_jev(query, found, top_n):
        seen["jev_top_n"] = top_n
        return found[:top_n], {"name": "Jev", "model": "jev-test",
                               "cost_usd": 0.0007, "input_tokens": 12}

    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())
    monkeypatch.setattr(ask, "search", fake_search)
    monkeypatch.setattr(ask, "search_hybrid", fake_hybrid)
    monkeypatch.setattr(ask, "rerank", fake_rerank)
    monkeypatch.setattr(ask, "rerank_jev", fake_jev)
    return seen


def _hits(n: int = 8) -> list[dict]:
    return [{"nr": str(i), "page": 1, "titel": "Titel", "text": f"träff {i}"}
            for i in range(n)]


def test_operatorens_val_i_miljon_vinner_over_modellens_argument(monkeypatch) -> None:
    """Utredning-sidan sätter MCP_* i subprocessens miljö. Modellen kan be om
    20/6, men operatörens 50/4 ska gälla — reranker-valet är en driftfråga."""
    seen = _fanga_anrop(monkeypatch, _hits())
    monkeypatch.setenv(mcp_server.ENV_RERANKER, "bge")
    monkeypatch.setenv(mcp_server.ENV_TOP_K, "50")
    monkeypatch.setenv(mcp_server.ENV_TOP_N, "4")

    mcp_server.search_archive("fråga", top_k=20, top_n=6)

    assert seen["top_k"] == 50
    assert seen["bge_top_n"] == 4


def test_operatorens_val_vinner_aven_om_modellen_stanger_av_rerank(monkeypatch) -> None:
    seen = _fanga_anrop(monkeypatch, _hits())
    monkeypatch.setenv(mcp_server.ENV_RERANKER, "bge")

    mcp_server.search_archive("fråga", rerank=False)

    assert "bge_top_n" in seen


def test_jev_i_miljon_anvander_jev_och_rapporterar_till_stderr(monkeypatch, capsys) -> None:
    seen = _fanga_anrop(monkeypatch, _hits())
    monkeypatch.setenv(mcp_server.ENV_RERANKER, "jev")
    monkeypatch.setenv(mcp_server.ENV_TOP_N, "3")

    result = mcp_server.search_archive("fråga")

    assert seen["jev_top_n"] == 3
    assert "bge_top_n" not in seen
    err = capsys.readouterr().err
    assert "Jev" in err and "0.0007" in err
    assert "träff 0" in result


def test_ingen_reranker_i_miljon_hoppar_over_omrankningen(monkeypatch) -> None:
    seen = _fanga_anrop(monkeypatch, _hits())
    monkeypatch.setenv(mcp_server.ENV_RERANKER, "none")
    monkeypatch.setenv(mcp_server.ENV_TOP_N, "2")

    mcp_server.search_archive("fråga")

    assert "bge_top_n" not in seen
    assert "jev_top_n" not in seen


def test_okant_rerankerlage_i_miljon_ger_tydligt_fel(monkeypatch) -> None:
    _fanga_anrop(monkeypatch, _hits())
    monkeypatch.setenv(mcp_server.ENV_RERANKER, "magic")

    with pytest.raises(RuntimeError, match="MCP_RERANKER"):
        mcp_server.search_archive("fråga")


def test_utan_miljoval_galler_modellens_argument(monkeypatch) -> None:
    """Fristående server (Claude Desktop, scripts/ask.py --mcp) har ingen
    Utredning-sida som sätter MCP_*, och då äger modellen sina argument."""
    for namn in (mcp_server.ENV_RERANKER, mcp_server.ENV_TOP_K, mcp_server.ENV_TOP_N):
        monkeypatch.delenv(namn, raising=False)
    seen = _fanga_anrop(monkeypatch, _hits())

    mcp_server.search_archive("fråga", top_k=7, top_n=2)

    assert seen["top_k"] == 7
    assert seen["bge_top_n"] == 2

    seen_av = _fanga_anrop(monkeypatch, _hits())
    mcp_server.search_archive("fråga", rerank=False)
    assert "bge_top_n" not in seen_av


# ── Webbsökning ──────────────────────────────────────────────────────────────
# Verktyget frågar OpenRouters web-plugin och returnerar sökträffarna som de
# kommer (url, titel, utdrag) i stället för en modellsammanfattning: arkivets
# svar ska bygga på källutdrag, inte på en mellanhands prosa.

def test_format_web_hits_markerar_kallor_utanfor_arkivet() -> None:
    out = mcp_server.format_web_hits(
        "Christer Pettersson",
        [{"url": "https://exempel.se/a", "title": "Rubrik A", "excerpt": "Utdrag A"}],
    )

    # Träffen skrivs som den färdiga citatsträngen: domänen ska synas i svaret och
    # adressen följa med, så modellen kan kopiera formen i stället för att hitta
    # på en egen.
    assert "[webbkälla: exempel.se, Rubrik A](https://exempel.se/a)" in out
    assert "Utdrag A" in out
    assert "[Nr " not in out  # aldrig arkivets citatformat


def test_format_web_hits_utan_traffar() -> None:
    assert "inga webbträffar" in mcp_server.format_web_hits("fråga", [])


def test_format_web_hits_kortar_av_langa_utdrag() -> None:
    out = mcp_server.format_web_hits(
        "fråga",
        [{"url": "https://exempel.se/a", "title": "A", "excerpt": "x" * 5000}],
    )

    # Exakt trunkering, inte ett löst tak: ett dubbelt så långt utdrag ska fälla testet.
    assert "x" * mcp_server.WEB_EXCERPT_CHARS in out
    assert "x" * (mcp_server.WEB_EXCERPT_CHARS + 1) not in out


def test_web_search_utan_nyckel_gor_inget_anrop(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("skulle inte anropa nätet utan nyckel")

    monkeypatch.setattr(mcp_server.requests, "post", explode)
    text, cost = mcp_server.search_web("Christer Pettersson")

    assert "OPENROUTER_API_KEY" in text
    assert cost is None


def test_web_search_skickar_web_plugin_och_laser_kostnaden(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")
    payload = {
        "usage": {"cost": 0.0088304},
        "choices": [
            {
                "message": {
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": "https://exempel.se/a",
                                "title": "Rubrik A",
                                "content": "Utdrag A",
                            },
                        }
                    ]
                }
            }
        ],
    }
    anrop: dict = {}

    class _Svar:
        status_code = 200

        def json(self) -> dict:
            return payload

    def fake_post(url: str, **kwargs):
        anrop["url"] = url
        anrop.update(kwargs)
        return _Svar()

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    text, cost = mcp_server.search_web("Christer Pettersson", max_results=3)

    assert anrop["url"] == mcp_server.WEB_SEARCH_ENDPOINT
    assert anrop["headers"]["Authorization"] == "Bearer test-nyckel"
    assert anrop["json"]["plugins"] == [
        {
            "id": "web",
            "engine": mcp_server.WEB_SEARCH_ENGINE,
            "max_results": 3,
            "exclude_domains": list(mcp_server.WEB_SEARCH_EXCLUDE_DOMAINS),
        }
    ]
    assert "[webbkälla: exempel.se, Rubrik A](https://exempel.se/a)" in text
    assert "Utdrag A" in text
    assert cost == pytest.approx(0.0088304)


def test_web_search_http_fel_redovisas_utan_pahittad_kostnad(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")

    class _Svar:
        status_code = 500
        text = "serverfel"

    monkeypatch.setattr(mcp_server.requests, "post", lambda *a, **k: _Svar())
    text, cost = mcp_server.search_web("fråga")

    assert "500" in text
    assert cost is None


def test_web_search_clamps_max_results(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")
    sedda: list[int] = []

    class _Svar:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"annotations": []}}]}

    def fake_post(url: str, **kwargs):
        sedda.append(kwargs["json"]["plugins"][0]["max_results"])
        return _Svar()

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    for begart in (0, 99, "många"):
        mcp_server.search_web("fråga", max_results=begart)

    assert sedda == [mcp_server.MAX_RESULTS_MIN, mcp_server.MAX_RESULTS_MAX,
                     mcp_server.MAX_RESULTS_DEFAULT]


def test_openai_verktygsscheman_matchar_mcp_verktygen() -> None:
    """Utredning-sidans OpenAI-scheman är en kopia av MCP-verktygen (OpenAI-vägen
    har ingen MCP-klient). Samma mängd, annars erbjuds modellen olika verktyg
    beroende på backend."""
    import ast
    import asyncio

    kalla = Path(__file__).resolve().parents[1] / "src" / "Utredning.py"
    tree = ast.parse(kalla.read_text(encoding="utf-8"))
    scheman = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") == "OPENAI_TOOLS"
    )
    namn = {post["function"]["name"] for post in ast.literal_eval(scheman)}

    assert namn == {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}


def test_mcp_svar_med_maskering_far_en_knuff_att_sla_upp(monkeypatch) -> None:
    """Promptregler om maskeringar tappas bort mitt i en verktygsloop: modellen
    fyllde i betydelsen av en förkortning ur minneskunskap i stället för att söka.
    Signalen måste därför ligga i verktygssvaret, där masken faktiskt läses."""
    hits = [{"nr": "Liggaren_13918-14966", "page": 41, "titel": "Liggaren",
             "text": "ANONYM UPPGIFTER OM CHP VAPEN ANNONS [MASKAD] 14830 00",
             "source": "Liggaren_13918-14966.txt"}]
    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())
    monkeypatch.setattr(ask, "search_hybrid", lambda *a: hits)
    monkeypatch.setattr(ask, "rerank", lambda q, found, n: found[:n])

    svar = mcp_server.search_with_settings(
        "CHP", hybrid=True, reranker="none", top_k=5, top_n=1
    )

    assert "web_search" in svar
    assert "[MASKAD]" in svar
    # Maskerade namn fick modellen att stanna vid "kan inte säkert säga": rollen
    # runt maskeringen får kontrolleras, och negativa slutsatser ska sökas först.
    assert "rollen eller företeelsen" in svar
    assert "inte framgår" in svar


def test_mcp_svar_utan_maskering_far_ingen_knuff(monkeypatch) -> None:
    hits = [{"nr": "281", "page": 2, "titel": "Förhör", "text": "vanlig text",
             "source": "281 — Förhör.txt"}]
    monkeypatch.setattr(mcp_server, "_table", object())
    monkeypatch.setattr(mcp_server, "_model", object())
    monkeypatch.setattr(ask, "search_hybrid", lambda *a: hits)
    monkeypatch.setattr(ask, "rerank", lambda q, found, n: found[:n])

    svar = mcp_server.search_with_settings(
        "fråga", hybrid=True, reranker="none", top_k=5, top_n=1
    )

    assert "web_search" not in svar


def test_get_page_pa_maskerad_sida_far_samma_knuff(text_dir) -> None:
    (text_dir / "maskad.txt").write_text("namn [MASKAD] datum\fren sida", encoding="utf-8")

    out = mcp_server.get_page("maskad.txt", 1)

    assert "[MASKAD]" in out
    assert "web_search" in out
    assert "web_search" not in mcp_server.get_page("maskad.txt", 2)


def test_web_search_utesluter_wpu_nu(monkeypatch) -> None:
    """wpu.nu är en spegling av arkivet självt: träffar därifrån är ingen
    självständig källa, och de tränger ut riktiga nätkällor. Domen utesluts både
    i sökningen (Exa stödjer exclude_domains) och lokalt, så garantin inte hänger
    på motorn."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")
    anrop: dict = {}

    class _Svar:
        status_code = 200

        def json(self) -> dict:
            return {"choices": [{"message": {"annotations": [
                {"url_citation": {"url": "https://wpu.nu/wiki/Sida:213.pdf",
                                  "title": "wpu.nu-kopia", "content": "arkivtext"}},
                {"url_citation": {"url": "https://www.wpu.nu/annat",
                                  "title": "wpu.nu-subdomän", "content": "arkivtext"}},
                {"url_citation": {"url": "https://skyttekretsen.se/foreningar/",
                                  "title": "Föreningar", "content": "riktig källa"}},
            ]}}]}

    def fake_post(url: str, **kwargs):
        anrop.update(kwargs)
        return _Svar()

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    text, _kostnad = mcp_server.search_web("Akademiska Skytteklubben ordförande")

    plugin = anrop["json"]["plugins"][0]
    assert "wpu.nu" in plugin["exclude_domains"]
    assert "*.wpu.nu" in plugin["exclude_domains"]
    assert "wpu.nu" not in text
    assert "skyttekretsen.se" in text
    assert "1 träffar" in text


def test_excluded_domain_kraver_domanagrans() -> None:
    """Punktgränsen: en domän som bara *slutar* på wpu.nu är en annan sajt och
    får inte uteslutas (tidigare fälldes notwpu.nu av endswith utan punkt)."""
    assert mcp_server.excluded_domain("https://wpu.nu/x") is True
    assert mcp_server.excluded_domain("https://www.wpu.nu/x") is True
    assert mcp_server.excluded_domain("https://forum.wpu.nu/x") is True
    assert mcp_server.excluded_domain("https://notwpu.nu/x") is False
    assert mcp_server.excluded_domain("https://forum.evilwpu.nu/x") is False
    assert mcp_server.excluded_domain("https://wpu.nu.evil.com/x") is False


def test_saknad_nyckel_loggas_en_gang_per_process(monkeypatch) -> None:
    """En nyckellös körning anropar verktyget i varje tur: loggen ska inte fyllas
    med samma rad, men en rad ska finnas så felet går att hitta."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server, "_nyckel_loggad", False)
    rader: list[tuple] = []
    monkeypatch.setattr(mcp_server, "log_error", lambda *a: rader.append(a))

    for _ in range(3):
        mcp_server.search_web("fråga")

    assert len(rader) == 1
    assert "OPENROUTER_API_KEY" in rader[0][2]


# ── Webbsöksbudget ───────────────────────────────────────────────────────────
# Taket är operatörens kostnadsbroms: varje sökning kostar ~$0,007 hos
# OpenRouter, och en körning utan tak gjorde 16 sökningar (~$0,12).

def _web_svar(innehåll: list, nyckel: str = "choices") -> object:
    """HTTP-svar i valt format: OpenRouters choices eller OpenAI/Anthropics listor."""
    class _Svar:
        status_code = 200

        def json(self) -> dict:
            if nyckel == "choices":
                return {"usage": {"cost": 0.0074},
                        "choices": [{"message": {"annotations": innehåll}}]}
            return {nyckel: innehåll}

    return _Svar()


def test_budgeten_stoppar_fler_anrop_och_nollstalls_per_fraga(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")
    anrop: list = []
    monkeypatch.setattr(
        mcp_server.requests, "post",
        lambda *a, **k: anrop.append(1) or _web_svar([]),
    )
    mcp_server.set_web_search_budget(2)

    assert mcp_server.web_search_budget_left() == 2
    assert mcp_server.search_web("ett")[1] is not None
    assert mcp_server.search_web("två")[1] is not None
    text, kostnad = mcp_server.search_web("tre")

    assert "budgeten" in text and "2 sökningar" in text
    assert kostnad is None
    assert len(anrop) == 2, "den tredje sökningen ska inte ens nå nätet"
    assert mcp_server.web_search_budget_left() == 0

    # Ny fråga → ny budget.
    mcp_server.set_web_search_budget(5)
    assert mcp_server.web_search_budget_left() == 5
    assert mcp_server.search_web("fyra")[1] is not None


def test_budgeten_lases_ur_miljon_och_klampas(monkeypatch) -> None:
    monkeypatch.setenv(mcp_server.ENV_WEB_SEARCH_BUDGET, "1")
    mcp_server.set_web_search_budget(None)
    assert mcp_server.web_search_budget() == 1

    monkeypatch.setenv(mcp_server.ENV_WEB_SEARCH_BUDGET, "99")
    mcp_server.set_web_search_budget(None)
    assert mcp_server.web_search_budget() == mcp_server.WEB_SEARCH_BUDGET_MAX

    assert mcp_server.clamp_web_search_budget(0) == mcp_server.WEB_SEARCH_BUDGET_MIN
    assert mcp_server.clamp_web_search_budget("många") == mcp_server.WEB_SEARCH_BUDGET_DEFAULT


def test_nyckel_loggas_trots_tom_budget(monkeypatch) -> None:
    """Budgeten prövas före nyckeln: en nyckellös körning ska inte heller den
    tystna — och en slot som förbrukas av ett anrop som aldrig blev av räknas
    ändå, så räknaren inte kan låsas upp av en död konfiguration."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(mcp_server, "_nyckel_loggad", False)
    rader: list = []
    monkeypatch.setattr(mcp_server, "log_error", lambda *a: rader.append(a))
    mcp_server.set_web_search_budget(1)

    mcp_server.search_web("fråga")
    mcp_server.search_web("fråga igen")

    assert len(rader) == 1
    assert mcp_server.web_search_budget_left() == 0, "förbrukad slot, inget anrop"


# ── Leverantörsval ───────────────────────────────────────────────────────────
# Verktyget får inte vara låst till OpenRouter: sökningen går via den leverantör
# operatören (eller LLM-profilen) har nyckel till. OpenAI- och Anthropic-vägarna
# är skrivna mot respektive dokumenterade svar och enhetstestade mot mockad
# klient — de är inte live-verifierade härifrån (best effort).

def _bara_nycklar(monkeypatch, *namn: str) -> None:
    for env in mcp_server.PROVIDER_KEYS.values():
        monkeypatch.delenv(env, raising=False)
    for provider in namn:
        monkeypatch.setenv(mcp_server.PROVIDER_KEYS[provider], "test-nyckel")


def test_provider_from_endpoint_kanner_igen_profilerna(monkeypatch) -> None:
    assert mcp_server.provider_from_endpoint("https://openrouter.ai/api/v1") == "openrouter"
    assert mcp_server.provider_from_endpoint("https://api.openai.com/v1") == "openai"
    assert mcp_server.provider_from_endpoint("https://api.anthropic.com/v1") == "anthropic"
    assert mcp_server.provider_from_endpoint(None, kind="claude") == "anthropic"
    assert mcp_server.provider_from_endpoint("http://localhost:11434/v1") is None


def test_auto_foljer_llm_profilens_leverantor(monkeypatch) -> None:
    _bara_nycklar(monkeypatch, "openrouter", "openai")

    assert mcp_server.resolve_web_search_provider("auto", preferred="openai") == "openai"
    assert mcp_server.resolve_web_search_provider("auto", preferred="openrouter") == "openrouter"
    # Okänd preferens (lokal modell) → första leverantör med nyckel.
    assert mcp_server.resolve_web_search_provider("auto", preferred=None) == "openrouter"


def test_uttryckligt_val_utan_nyckel_faller_tillbaka(monkeypatch) -> None:
    _bara_nycklar(monkeypatch, "openrouter")

    # OpenAI valt men nyckeln saknas: hellre en fungerande sökning än ett tyst nej.
    assert mcp_server.resolve_web_search_provider("openai", preferred="openrouter") == "openrouter"
    # Ingen nyckel alls: namnge standardleverantörens nyckel i felet.
    _bara_nycklar(monkeypatch)
    assert mcp_server.resolve_web_search_provider("auto", preferred="openai") == "openrouter"


def test_openai_vagen_anropar_responses_med_web_search(monkeypatch) -> None:
    _bara_nycklar(monkeypatch, "openai")
    mcp_server.set_web_search_provider("openai")
    anrop: dict = {}

    def fake_post(url: str, **kwargs):
        anrop["url"] = url
        anrop.update(kwargs)
        return _web_svar(
            [
                {
                    "type": "message",
                    "content": [
                        {
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url_citation": {
                                        "url": "https://svd.se/a/palme",
                                        "title": "SvD om Palmemordet",
                                    },
                                }
                            ]
                        }
                    ],
                }
            ],
            nyckel="output",
        )

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    text, kostnad = mcp_server.search_web("Christer Andersson revolver", max_results=4)

    assert anrop["url"] == mcp_server.WEB_SEARCH_OPENAI_ENDPOINT
    assert anrop["headers"]["Authorization"] == "Bearer test-nyckel"
    assert anrop["json"]["tools"] == [{"type": "web_search"}]
    assert "[webbkälla: svd.se, SvD om Palmemordet](https://svd.se/a/palme)" in text
    # OpenAI fakturerar sökningen separat och rapporterar den inte i svaret.
    assert kostnad is None


def test_anthropic_vagen_laser_web_search_tool_result(monkeypatch) -> None:
    _bara_nycklar(monkeypatch, "anthropic")
    mcp_server.set_web_search_provider("anthropic")
    anrop: dict = {}

    def fake_post(url: str, **kwargs):
        anrop["url"] = url
        anrop.update(kwargs)
        return _web_svar(
            [
                {
                    "type": "web_search_tool_result",
                    "content": [
                        {"type": "web_search_result", "url": "https://wpu.nu/x",
                         "title": "spegling"},
                        {"type": "web_search_result", "url": "https://skyttekretsen.se/f",
                         "title": "Föreningar"},
                    ],
                }
            ],
            nyckel="content",
        )

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    text, kostnad = mcp_server.search_web("Akademiska skytteklubben")

    assert anrop["url"] == mcp_server.WEB_SEARCH_ANTHROPIC_ENDPOINT
    assert anrop["headers"]["x-api-key"] == "test-nyckel"
    assert anrop["json"]["tools"][0]["type"] == "web_search_20250305"
    assert anrop["json"]["tool_choice"] == {"type": "tool", "name": "web_search"}
    # wpu.nu-undantaget gäller alla leverantörer.
    assert "wpu.nu" not in text
    assert "skyttekretsen.se" in text
    assert kostnad is None


def test_nyckellos_vald_leverantor_namnges_i_feltexten(monkeypatch) -> None:
    _bara_nycklar(monkeypatch)
    mcp_server.set_web_search_provider("openai")
    monkeypatch.setattr(mcp_server.requests, "post",
                        lambda *a, **k: pytest.fail("ska inte anropa nätet"))

    text, kostnad = mcp_server.search_web("fråga")

    assert "OPENAI_API_KEY" in text and "openai" in text
    assert kostnad is None


def _med_profil(monkeypatch, namn: str = "Billig", standard: str = "", **profil) -> None:
    """En konfigurerad LLM-profil i llm_config, som sökningen kan välja."""
    import config

    monkeypatch.setattr(config, "load_search_default", lambda: standard)
    vald = {
        "backend_name": "OpenRouter",
        "model": "openai/gpt-4.1-nano",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        **profil,
    }
    monkeypatch.setattr(
        config, "load_all", lambda: {"profiles": {namn: vald}, "default": namn}
    )


def test_sokmodellen_ar_en_konfigurerad_llm_profil(monkeypatch, tmp_path) -> None:
    """Sökmodellen väljs bland operatörens egna LLM-profiler: profilens leverantör
    avgör sök-API:et och profilens modell gör anropet."""
    _bara_nycklar(monkeypatch, "openrouter")
    _med_profil(monkeypatch)
    mcp_server.set_web_search_profile(None)
    monkeypatch.delenv(mcp_server.ENV_WEB_SEARCH_MODEL, raising=False)

    # Utan vald profil: auto → LLM-profilens leverantör och dess standardmodell.
    assert mcp_server.web_search_profile() == ""
    assert mcp_server.web_search_provider() == "openrouter"
    assert mcp_server.web_search_model() == mcp_server.WEB_SEARCH_DEFAULT_MODELS["openrouter"]

    mcp_server.set_web_search_profile("Billig")
    assert mcp_server.web_search_profile() == "Billig"
    assert mcp_server.web_search_provider() == "openrouter"
    assert mcp_server.web_search_model() == "openai/gpt-4.1-nano"

    # Förstahandsvalet ligger i LLM-konfigurationen (llm_config.json) och används
    # när inget annat är satt.
    _med_profil(monkeypatch, standard="Billig")
    mcp_server.set_web_search_profile(None)
    assert mcp_server.web_search_profile() == "Billig"
    assert mcp_server.web_search_model() == "openai/gpt-4.1-nano"

    _med_profil(monkeypatch, standard="")
    mcp_server.set_web_search_profile(None)
    assert mcp_server.web_search_profile() == ""


def test_vald_sokmodell_skickas_med_i_anropet(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-nyckel")
    _med_profil(monkeypatch)
    mcp_server.set_web_search_profile("Billig")
    anrop: dict = {}

    def fake_post(url: str, **kwargs):
        anrop.update(kwargs)
        return _web_svar([])

    monkeypatch.setattr(mcp_server.requests, "post", fake_post)
    mcp_server.search_web("fråga")

    assert anrop["json"]["model"] == "openai/gpt-4.1-nano"
    assert anrop["headers"]["Authorization"] == "Bearer test-nyckel"


def test_search_profiles_speglar_konfigurationen(monkeypatch) -> None:
    """Listan i gränssnittet byggs ur operatörens profiler — inte en fast lista
    över API:er som erbjuder val utan nyckel. Profiler utan sök-API faller bort."""
    import config

    profiler = {
        "Deepseek flash": {"backend_name": "DeepSeek", "base_url": "https://api.deepseek.com/v1"},
        "Openrouter billig": {
            "backend_name": "OpenRouter",
            "model": "openai/gpt-4.1-nano",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_API_KEY",
        },
        "ollama": {"backend_name": "Ollama (lokal)", "base_url": "http://localhost:11434/v1"},
        "Openai profil": {
            "backend_name": "OpenAI",
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "OPENAI_API_KEY",
        },
    }
    monkeypatch.setattr(
        config, "load_all", lambda: {"profiles": profiler, "default": "Deepseek flash"}
    )
    import backends

    valbara = mcp_server.search_profiles(profiler, backends.BACKENDS)

    assert [v["name"] for v in valbara] == ["Openrouter billig", "Openai profil"]
    assert valbara[0] == {
        "name": "Openrouter billig",
        "provider": "openrouter",
        "model": "openai/gpt-4.1-nano",
        "key_env": "OPENROUTER_API_KEY",
    }
    assert valbara[1]["provider"] == "openai"
    assert mcp_server.search_profiles(None) == []


