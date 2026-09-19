# tests/test_prompts.py
"""Tester för redigerbara systempromptar (src/prompts.py)."""

from __future__ import annotations

import json

import prompts


def test_default_nar_fil_saknas(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "saknas.json")
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT
    assert prompts.load_prompts() == {}


def test_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "Ny RAG-prompt", "mcp": "Ny MCP-prompt"})
    assert prompts.rag_prompt() == "Ny RAG-prompt"
    assert prompts.mcp_prompt() == "Ny MCP-prompt"


def test_partiell_fil_faller_tillbaka_per_nyckel(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text(json.dumps({"rag": "Bara RAG"}), encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.rag_prompt() == "Bara RAG"
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT


def test_trasig_fil_faller_tillbaka(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text("{inte json", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.load_prompts() == {}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT


def test_tomt_varde_raknas_inte(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "  ", "mcp": "OK"})
    lagrat = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
    assert lagrat == {"mcp": "OK"}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT


def test_inledande_och_avslutande_blanksteg_bevaras(tmp_path, monkeypatch) -> None:
    """Ett sparat fält ska bevaras ordagrant — bara helt tomma värden filtreras."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "\n  Rad ett\n\n"})
    assert prompts.rag_prompt() == "\n  Rad ett\n\n"


def test_save_lamnar_ingen_tempfil(tmp_path, monkeypatch) -> None:
    """Atomisk skrivning ska inte lämna kvar någon .tmp-fil."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"mcp": "Ny MCP"})
    assert [p.name for p in tmp_path.iterdir()] == ["prompts.json"]


def test_ask_anvander_override_vid_anropstillfallet(tmp_path, monkeypatch) -> None:
    """ask.py ska hämta prompten vid anropet, inte binda den vid import."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    import ask

    assert ask.rag_prompt() == prompts.SYSTEM_PROMPT
    assert ask.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT
    prompts.save_prompts({"rag": "Overridd RAG", "mcp": "Overridd MCP"})
    assert ask.rag_prompt() == "Overridd RAG"
    assert ask.mcp_prompt() == "Overridd MCP"


def test_mcp_prompten_namner_varje_verktyg_i_mcp_server():
    """Verktygsreferensen i prompten och verktygen i mcp_server.py ska vara samma
    mängd — annars beskriver prompten ett verktyg som inte finns (eller tvärtom)."""
    import asyncio

    import mcp_server

    namn = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    assert namn
    for verktyg in namn:
        assert verktyg in prompts.MCP_SYSTEM_PROMPT, verktyg


def test_mcp_prompten_markerar_webbkallor_utanfor_arkivet():
    """Webbmaterial får aldrig kunna läsas som arkivbelägg: prompten ska kräva en
    egen markör för webbkällor och förbjuda arkivformatet på dem."""
    assert "[webbkälla" in prompts.MCP_SYSTEM_PROMPT
    assert "web_search" in prompts.MCP_SYSTEM_PROMPT


def test_mcp_prompten_tvingar_uppslag_i_stallet_for_gissning():
    """Otydligt material (maskeringar, oläsliga namn, okända förkortningar) fick
    modellen att gissa ur minneskunskap i stället för att söka: den svarade att
    CHP betydde "Centrala högskoleförbundet" om en vapenannons från 1993. Prompten
    måste namnge de observerbara signalerna och förbjuda gissningen."""
    text = prompts.MCP_SYSTEM_PROMPT
    assert "[MASKAD]" in text
    assert "förkortning" in text
    # Betydelsen får komma ur materialet, ur ett uppslag eller inte alls.
    assert "minneskunskap" in text
    # Nätet får inte användas för att rekonstruera själva maskeringen.
    assert "rekonstruera" in text


def test_verktygsbeskrivningarna_namner_signalerna(monkeypatch) -> None:
    """Modellen läser verktygets beskrivning när den väljer — båda vägarna
    (MCP-servern och OpenAI-schemat) ska peka på samma signaler."""
    import ast
    import asyncio
    from pathlib import Path

    import mcp_server

    verktyg = {t.name: (t.description or "") for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert "[MASKAD]" in verktyg["web_search"]
    assert "förkortning" in verktyg["web_search"]

    kalla = Path(__file__).resolve().parents[1] / "src" / "Utredning.py"
    tree = ast.parse(kalla.read_text(encoding="utf-8"))
    scheman = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "OPENAI_TOOLS"
    )
    beskrivningar = {
        post["function"]["name"]: post["function"]["description"]
        for post in ast.literal_eval(scheman)
    }
    assert "[MASKAD]" in beskrivningar["web_search"]
    assert "förkortning" in beskrivningar["web_search"]


def test_mcp_prompten_kontrollerar_det_som_gar_att_belagga_utanfor_arkivet():
    """Verifieringsläget: arkivsvaret kan vila på en firma, en adress eller ett
    osäkert förhör — sådant går att belägga utanför arkivet och ska kontrolleras
    även när arkivet svarar (Sportskyttematerial på Arsenalsgatan)."""
    text = prompts.MCP_SYSTEM_PROMPT
    assert "även när arkivet svarar" in text
    for signal in ("företag", "adress", "vapenmärke", "tidningsannons"):
        assert signal in text, signal


def test_mcp_prompten_lyder_ett_uttryckligt_uppdrag_att_kontrollera():
    """Frågan får styra: ber användaren om en nätkontroll ska den göras direkt,
    utan en motfråga eller ett löfte."""
    assert "Ber användaren dig kontrollera" in prompts.MCP_SYSTEM_PROMPT


def test_mcp_prompten_kontrollerar_innan_den_säger_att_nagot_inte_framgår():
    """Rotorsaken till uteblivna sökningar: triggern var en lista av objekttyper,
    så allt utanför listan passerade tyst. Klubben (Akademiska Skytteklubben) och
    ordförandens mandattid (Ernst Althin) fanns inte i någon lista, och modellen
    avslutade med "kan inte säkert säga" utan att söka. Kriteriet ska vara
    företeelsen — inte typen."""
    text = prompts.MCP_SYSTEM_PROMPT
    # Blankettregeln: osäkerhet är alltid ett skäl att söka, inte bara vissa typer.
    assert "Osäkerhet är alltid ett skäl att söka" in text
    assert "entydigt svar" in text
    assert "klubb" in text and "uppdrag" in text
    # Ärligheten kvar: en motsägelse mellan vittnesmål avgörs inte av nätet.
    assert "vittnesmål" in text


def test_mcp_prompten_soker_runt_maskeringen_utan_att_fylla_i_den():
    """Maskerade namn fick modellen att stanna: "namnet är maskerat ... därför kan
    jag inte säkert säga". Rollen runt maskeringen får kontrolleras på nätet, men
    webbens namn får aldrig presenteras som dokumentets innehåll."""
    text = prompts.MCP_SYSTEM_PROMPT
    assert "rollen eller företeelsen runt maskeringen" in text
    assert "avse samma person" in text


def test_mcp_prompten_soker_nulaget_och_daterar_webbkällan():
    """Frågor om nuläget ("hur är det idag", "vad gör personen idag") ligger efter
    det materialet kan svara på i tid och måste alltid sökas — och webbuppgiften
    måste dateras, eftersom den åldras."""
    text = prompts.MCP_SYSTEM_PROMPT
    assert "nuläget" in text
    assert "efter det arkivet kan svara på i tid" in text
    assert "datera" in text


def test_mcp_prompten_tillater_offentligt_publicerade_privatlivsuppgifter():
    """Ägarens beslut: privatlivsuppgifter får tas med om de finns offentligt på
    nätet. Kravet är att källan och datumet står i svaret — inte att uppgiften
    utelämnas — och att arkivets uppgifter inte upprepas som nuläge."""
    # Radbrytningen i prompten ska inte avgöra testet.
    text = " ".join(prompts.MCP_SYSTEM_PROMPT.split())
    assert "finns offentligt på nätet" in text
    assert "Privatlivsuppgifter" not in text
    assert "arkivets uppgifter om personen som nuläge" in text
