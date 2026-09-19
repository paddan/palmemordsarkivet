"""Tester för förstahandsvalet för webbsök i LLM-konfigurationen (src/config.py)."""

from __future__ import annotations

import json

import config


def _med_fil(tmp_path, monkeypatch, innehåll: dict) -> None:
    fil = tmp_path / "llm_config.json"
    fil.write_text(json.dumps(innehåll), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", fil)


def test_forstahandsvalet_rundar_till_filen(tmp_path, monkeypatch) -> None:
    """Valet hör till LLM-konfigurationen och ska överleva att en profil sparas."""
    _med_fil(
        tmp_path,
        monkeypatch,
        {"profiles": {"Billig": {"backend_name": "OpenRouter"}}, "default": "Billig"},
    )

    assert config.load_search_default() == ""
    config.save_search_default("Billig")
    assert config.load_search_default() == "Billig"

    # Att spara profilkatalogen får inte nolla sökvalet.
    config.save_profiles({"Billig": {"backend_name": "OpenRouter"}}, "Billig")
    assert config.load_search_default() == "Billig"
    assert json.loads(config.CONFIG_FILE.read_text(encoding="utf-8"))["web_search_default"] == "Billig"

    config.save_search_default("")
    assert config.load_search_default() == ""


def test_borttagen_profil_ar_inte_ett_forstahandsval(tmp_path, monkeypatch) -> None:
    """Ett spar val som pekar på en borttagen eller omdöpt profil ska falla
    tillbaka på auto i stället för att bli ett dött val."""
    _med_fil(
        tmp_path,
        monkeypatch,
        {
            "profiles": {"Billig": {"backend_name": "OpenRouter"}},
            "default": "Billig",
            "web_search_default": "Borttagen",
        },
    )

    assert config.load_search_default() == ""
