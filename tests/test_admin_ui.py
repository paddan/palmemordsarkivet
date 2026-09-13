"""Tester för rena adminhelpers i src/admin_ui.py."""

from __future__ import annotations

import json
from dataclasses import replace

import admin_ui
import prompts
from admin_ui import (
    CUSTOM_MODEL_LABEL,
    ROOT,
    UNCHANGED_CHOICE_LABEL,
    apply_llm_profile_form,
    choice_form_options,
    format_job_status,
    group_admin_operations,
    llm_form_defaults,
    llm_model_options,
    load_settings,
    missing_required_paths,
    normalize_choice_selection,
    numeric_input_bounds,
    progress_fraction,
    resolve_path_default,
)
from operations.models import OperationDefinition, ParameterDefinition
from operations.registry import OperationRegistry


def _llm_settings_app(tmp_path, monkeypatch) -> object:
    from streamlit.testing.v1 import AppTest

    # Panelen läser räknaren ur state.db — peka den på en testdatabas så testet
    # aldrig rör utvecklarens riktiga state.db.
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    config_file = tmp_path / "llm_config.json"
    config_file.write_text(
        json.dumps(
            {
                "profiles": {
                    "Standard": {
                        "backend_name": "Claude",
                        "provider": "claude",
                        "model": "claude-opus-4-8",
                        "base_url": "",
                    }
                },
                "default": "Standard",
            }
        ),
        encoding="utf-8",
    )
    app = AppTest.from_string(
        "from pathlib import Path\n"
        "import admin_ui, config\n"
        f"config.CONFIG_FILE = Path({str(config_file)!r})\n"
        "admin_ui.render_llm_settings()\n"
    )
    app.run(timeout=20)
    return app


def _definition(operation_id: str, group: str = "Pipeline", label: str | None = None, parameters=()) -> OperationDefinition:
    return OperationDefinition(
        id=operation_id,
        label=label or operation_id,
        group=group,
        description="Testoperation",
        parameters=parameters,
        admin_visible=True,
        mutating=True,
        confirmation=None,
        run=lambda context, params: None,
    )


def test_progress_fraction_handles_unknown_total() -> None:
    assert progress_fraction({"completed_units": 3, "total_units": None}) is None
    assert progress_fraction({"completed_units": 3, "total_units": 0}) is None
    assert progress_fraction({"completed_units": 3, "total_units": 10}) == 0.3


def test_only_admin_visible_operations_are_grouped() -> None:
    registry = OperationRegistry()
    registry.register(_definition("run-pipeline", group="Pipeline"))
    registry.register(_definition("ingest", group="Index"))
    hidden = replace(_definition("install", group="System"), admin_visible=False)
    registry.register(hidden)

    grouped = group_admin_operations(registry)
    ids = {definition.id for values in grouped.values() for definition in values}

    assert "run-pipeline" in ids
    assert "ingest" in ids
    assert "install" not in ids
    assert set(grouped.keys()) == {"Pipeline", "Index"}


def test_format_job_status_returns_swedish_labels() -> None:
    assert format_job_status("queued") == "Köad"
    assert format_job_status("running") == "Körs"
    assert format_job_status("succeeded") == "Lyckades"
    assert format_job_status("interrupted") == "Avbruten (omstart)"
    assert format_job_status("okänd-status") == "okänd-status"


def test_load_settings_defaults_are_relative_to_base() -> None:
    settings = load_settings()
    # Base-path och debug-växlingen lagras; underkatalogerna är hårdkodade.
    assert settings == {"base": str(ROOT), "debug_log": ""}


def test_debug_logging_setting_round_trips_and_sets_the_environment(tmp_path, monkeypatch) -> None:
    import os

    from operations.context import DEBUG_ENV

    monkeypatch.setattr(admin_ui, "SETTINGS_FILE", tmp_path / "admin_settings.json")
    monkeypatch.delenv(DEBUG_ENV, raising=False)

    assert admin_ui.apply_debug_logging(admin_ui.load_settings()) is False
    assert os.environ[DEBUG_ENV] == ""

    admin_ui.save_settings({admin_ui.BASE_KEY: str(ROOT), admin_ui.DEBUG_KEY: "1"})
    assert admin_ui.load_settings()[admin_ui.DEBUG_KEY] == "1"
    assert admin_ui.apply_debug_logging(admin_ui.load_settings()) is True
    assert os.environ[DEBUG_ENV] == "1"

    # Base-path-fältet får inte tappa debug-växlingen.
    admin_ui.save_settings({admin_ui.BASE_KEY: "/tmp/base", admin_ui.DEBUG_KEY: "1"})
    assert admin_ui.load_settings() == {"base": "/tmp/base", "debug_log": "1"}


def test_resolve_path_default_joins_base_and_relative() -> None:
    files_default = ROOT / "downloaded" / "files"
    # Underkatalogen (downloaded/files) är hårdkodad — bara base-path ändras.
    assert resolve_path_default(files_default, {"base": str(ROOT)}) == str(ROOT / "downloaded" / "files")
    assert resolve_path_default(files_default, {"base": "/tmp/base"}) == "/tmp/base/downloaded/files"
    # Icke-systemväg lämnas orörd.
    assert resolve_path_default("/annan/väg", {"base": "/tmp"}) == "/annan/väg"


def _settings_app(tmp_path):
    """Rendera Inställningar-fliken mot tillfälliga inställningsfiler."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "from pathlib import Path\n"
        "import admin_ui, prompts\n"
        f"admin_ui.SETTINGS_FILE = Path({str(tmp_path / 'admin_settings.json')!r})\n"
        f"prompts.PROMPTS_FILE = Path({str(tmp_path / 'prompts.json')!r})\n"
        "admin_ui.render_settings_tab()\n"
    )
    app.run(timeout=20)
    return app


def test_prompts_roundtrip_via_helpers(tmp_path, monkeypatch) -> None:
    """Admin sparar/läser via samma helpers som Utredning använder."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    admin_ui.save_prompts_form({"rag": "Ny RAG", "mcp": "Ny MCP"})
    assert admin_ui.load_prompts_form() == {"rag": "Ny RAG", "mcp": "Ny MCP"}


def test_prompts_form_reset_ger_tomma_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    admin_ui.save_prompts_form({"rag": "Ny RAG", "mcp": "Ny MCP"})
    admin_ui.reset_prompts_form()
    assert admin_ui.load_prompts_form() == {}


def test_prompt_hjalpen_tacker_varje_lage_och_verktygets_granser() -> None:
    """Skrivhjälpen ska finnas för varje promptkort och visa MCP-verktygets
    faktiska gränser — annars glider hjälp och prompt isär från mcp_server.py."""
    import mcp_server

    # Samma nycklar i rubrik-mappningen och hjälptexterna, annars KeyError vid rendering.
    assert set(admin_ui.PROMPT_MODE_LABELS) == set(admin_ui.PROMPT_MODE_HELP) == {"rag", "mcp"}

    intervall = (
        f"{mcp_server.TOP_K_MIN}\u2013{mcp_server.TOP_K_MAX}",
        f"{mcp_server.TOP_N_MIN}\u2013{mcp_server.TOP_N_MAX}",
    )
    for text in (admin_ui.PROMPT_MODE_HELP["mcp"], prompts.MCP_SYSTEM_PROMPT):
        for grans in intervall:
            assert grans in text
    assert f"top_k={mcp_server.TOP_K_DEFAULT}" in admin_ui.PROMPT_MODE_HELP["mcp"]


def test_promptar_sektionen_sparar_och_aterstaller(tmp_path) -> None:
    """Sektionen visar båda promptarna med egen Spara- och Återställ-knapp."""
    app = _settings_app(tmp_path)
    prompts_file = tmp_path / "prompts.json"

    assert not app.exception
    assert {item.label for item in app.text_area} == {
        "Fråga arkivet (RAG)",
        "Utredningsläge (MCP)",
    }
    assert {item.key for item in app.button} >= {
        "prompt_rag_save",
        "prompt_mcp_save",
        "prompt_rag_reset",
        "prompt_mcp_reset",
    }
    rag = next(item for item in app.text_area if item.label == "Fråga arkivet (RAG)")
    mcp = next(item for item in app.text_area if item.label == "Utredningsläge (MCP)")
    assert rag.value == prompts.SYSTEM_PROMPT
    assert mcp.value == prompts.MCP_SYSTEM_PROMPT

    # Bara RAG-fältet sparas — MCP-prompten ska inte ärva sin standardtext som override.
    rag.set_value("Egen RAG-prompt")
    next(item for item in app.button if item.key == "prompt_rag_save").click()
    app.run(timeout=20)

    assert not app.exception
    assert json.loads(prompts_file.read_text(encoding="utf-8")) == {
        "rag": "Egen RAG-prompt"
    }

    # MCP sparas för sig, utan att röra RAG-overriden. Widgetarna hämtas om efter
    # varje körning — AppTest-referenser från före en rerun är inte pålitliga.
    mcp = next(item for item in app.text_area if item.label == "Utredningsläge (MCP)")
    mcp.set_value("Egen MCP-prompt")
    next(item for item in app.button if item.key == "prompt_mcp_save").click()
    app.run(timeout=20)

    assert json.loads(prompts_file.read_text(encoding="utf-8")) == {
        "rag": "Egen RAG-prompt",
        "mcp": "Egen MCP-prompt",
    }

    # Återställningen gäller bara den egna prompten; widget-state måste rensas.
    next(item for item in app.button if item.key == "prompt_rag_reset").click()
    app.run(timeout=20)

    assert not app.exception
    assert json.loads(prompts_file.read_text(encoding="utf-8")) == {
        "mcp": "Egen MCP-prompt"
    }
    assert next(
        item for item in app.text_area if item.label == "Fråga arkivet (RAG)"
    ).value == prompts.SYSTEM_PROMPT
    assert next(
        item for item in app.text_area if item.label == "Utredningsläge (MCP)"
    ).value == "Egen MCP-prompt"


def test_reset_prompts_form_med_nyckel_behaller_den_andra(tmp_path, monkeypatch) -> None:
    """Återställning av en prompt får inte röra den andra."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    admin_ui.save_prompts_form({"rag": "Ny RAG", "mcp": "Ny MCP"})
    admin_ui.reset_prompts_form("rag")
    assert admin_ui.load_prompts_form() == {"mcp": "Ny MCP"}


def _path_param(name: str = "inp", *, required: bool = False, default=None) -> ParameterDefinition:
    return ParameterDefinition(name, (f"--{name}",), "path", default, "sökväg", required=required)


def test_missing_required_paths_flags_empty_fields() -> None:
    """Regression: ett tomt required-fält får inte normaliseras till cwd."""
    definition = _definition(
        "ocr-pages", parameters=(_path_param(required=True), _path_param("out_dir"))
    )

    assert missing_required_paths(definition, {"inp": ""}) == ["inp"]
    assert missing_required_paths(definition, {"inp": "  "}) == ["inp"]
    assert missing_required_paths(definition, {"inp": "."}) == ["inp"]
    assert missing_required_paths(definition, {"inp": None}) == ["inp"]
    assert missing_required_paths(definition, {}) == ["inp"]
    # Icke-obligatoriska parametrar flaggas aldrig.
    assert missing_required_paths(definition, {"inp": "/tmp/a.pdf", "out_dir": ""}) == []
    assert missing_required_paths(definition, {"inp": "/tmp/a.pdf"}) == []


def test_choice_form_options_adds_unchanged_option_when_default_none() -> None:
    """Regression: choice-parametrar med default None måste kunna lämnas orörda."""
    provider = ParameterDefinition(
        "provider", ("--provider",), "choice", None, "claude eller openai",
        choices=("claude", "openai"),
    )
    engine = ParameterDefinition(
        "engine", ("--engine",), "choice", "tesseract", "Motor",
        choices=("tesseract", "surya"),
    )

    options, default_selection = choice_form_options(provider)
    assert options == [UNCHANGED_CHOICE_LABEL, "claude", "openai"]
    assert default_selection == UNCHANGED_CHOICE_LABEL
    assert normalize_choice_selection(provider, UNCHANGED_CHOICE_LABEL) is None
    assert normalize_choice_selection(provider, "claude") == "claude"

    options, default_selection = choice_form_options(engine)
    assert options == ["tesseract", "surya"]
    assert default_selection == "tesseract"


def test_numeric_input_bounds_are_sensible() -> None:
    """Regression: antal ≥ 1, trösklar 0–100, dpi > 0, sentinel-0 tillåts."""
    for name, expected in [
        ("jobs", 1),
        ("per_file_jobs", 1),
        ("chunk_chars", 1),
        ("batch", 1),
        ("dpi", 1),
        ("limit", 0),
        ("test_limit", 0),
    ]:
        minimum, maximum = numeric_input_bounds(name, "int")
        assert minimum == expected
        assert type(minimum) is int
        assert maximum is None
    assert numeric_input_bounds("threshold", "float") == (0.0, 100.0)
    assert numeric_input_bounds("margin", "float") == (None, None)


def test_llm_profile_payload_persists_secret_env_name_but_not_secret() -> None:
    payload = admin_ui.llm_profile_payload(
        backend_name="OpenAI-kompatibel",
        provider="openai",
        model="privat-modell",
        base_url="https://llm.example/v1",
        api_key_env="PRIVATE_LLM_TOKEN",
    )

    assert payload["api_key_env"] == "PRIVATE_LLM_TOKEN"
    assert "api_key" not in payload


def test_llm_settings_shows_accumulated_usage_and_saves_prices(tmp_path, monkeypatch) -> None:
    """Räknaren visas vid profilen och priserna sparas från formuläret."""
    import db

    app = _llm_settings_app(tmp_path, monkeypatch)
    conn = db.connect()
    db.init_schema(conn)
    db.add_llm_usage(
        conn, profile="Standard", model="claude-opus-4-8",
        input_tokens=1200, output_tokens=300, cost=0.02,
    )
    conn.close()
    app.run(timeout=20)

    assert not app.exception
    assert any(
        caption.value == "Ackumulerat: 1 anrop · ↑1k ↓300 · ≈ $0.0200"
        " · senast claude-opus-4-8"
        for caption in app.caption
    )

    next(item for item in app.number_input if item.label == "Input ($/1M)").set_value(5.0)
    next(item for item in app.number_input if item.label == "Output ($/1M)").set_value(25.0)
    next(button for button in app.button if button.label == "Spara ändringar").click()
    app.run(timeout=20)

    assert not app.exception
    stored = json.loads(
        (tmp_path / "llm_config.json").read_text(encoding="utf-8")
    )["profiles"]["Standard"]
    assert stored["input_price_usd"] == 5.0
    assert stored["output_price_usd"] == 25.0
    assert "cache_hit_price_usd" not in stored


def test_llm_settings_rename_moves_the_profile_usage(tmp_path, monkeypatch) -> None:
    """Namnbytet i formuläret ska flytta räknaren, inte lämna den kvar."""
    import db

    app = _llm_settings_app(tmp_path, monkeypatch)
    conn = db.connect()
    db.init_schema(conn)
    db.add_llm_usage(
        conn, profile="Standard", model="claude-opus-4-8",
        input_tokens=10, output_tokens=2, cost=0.5,
    )
    conn.close()
    app.run(timeout=20)

    next(item for item in app.text_input if item.label == "Namn").set_value("Snabb")
    next(button for button in app.button if button.label == "Spara ändringar").click()
    app.run(timeout=20)

    assert not app.exception
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    assert "Snabb" in stored["profiles"]
    conn = db.connect()
    assert db.get_llm_usage(conn, "Snabb")["calls"] == 1
    assert db.get_llm_usage(conn, "Standard")["calls"] == 0
    conn.close()


def test_llm_settings_cannot_delete_the_last_profile(tmp_path, monkeypatch) -> None:
    """Skyddet låg bara i widgetens disabled — next(iter(())) kraschade annars."""
    app = _llm_settings_app(tmp_path, monkeypatch)

    next(button for button in app.button if button.label == "Ta bort").click()
    app.run(timeout=20)

    assert not app.exception
    assert any("sista konfigurationen" in item.value for item in app.error)
    stored = json.loads((tmp_path / "llm_config.json").read_text(encoding="utf-8"))
    assert "Standard" in stored["profiles"]


def test_llm_profile_payload_stores_prices_and_drops_unset_ones() -> None:
    payload = admin_ui.llm_profile_payload(
        backend_name="DeepSeek",
        provider="openai",
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        input_price_usd=0.14,
        output_price_usd=0.28,
        cache_hit_price_usd=0.0028,
    )
    assert (payload["input_price_usd"], payload["output_price_usd"]) == (0.14, 0.28)
    assert payload["cache_hit_price_usd"] == 0.0028

    utan_priser = admin_ui.llm_profile_payload(
        backend_name="Claude",
        provider="claude",
        model="claude-opus-4-8",
        base_url="",
        api_key_env="",
        input_price_usd=0.0,
        output_price_usd=None,
        cache_hit_price_usd=0.0,
    )
    assert not [k for k in utan_priser if k.endswith("_price_usd")]


def test_apply_llm_profile_form_renames_and_sets_default_atomically() -> None:
    profiles = {
        "Standard": {"model": "claude-opus-4-8"},
        "Snabb": {"model": "gpt-5-mini"},
    }
    payload = {"model": "deepseek-chat"}

    updated, default = apply_llm_profile_form(
        profiles,
        selected_name="Snabb",
        entered_name="DeepSeek snabb",
        payload=payload,
        default_name="Standard",
        make_default=True,
    )

    assert updated == {
        "Standard": {"model": "claude-opus-4-8"},
        "DeepSeek snabb": payload,
    }
    assert default == "DeepSeek snabb"
    assert profiles == {
        "Standard": {"model": "claude-opus-4-8"},
        "Snabb": {"model": "gpt-5-mini"},
    }


def test_apply_llm_profile_form_rejects_empty_or_duplicate_name() -> None:
    profiles = {"Standard": {}, "Snabb": {}}

    for entered_name in ("", "   ", "Standard"):
        try:
            apply_llm_profile_form(
                profiles,
                selected_name="Snabb",
                entered_name=entered_name,
                payload={},
                default_name="Standard",
                make_default=False,
            )
        except ValueError:
            pass
        else:
            raise AssertionError(f"Namnet {entered_name!r} borde ha avvisats")


def test_apply_llm_profile_form_creates_profile_without_half_saved_state() -> None:
    profiles = {"Standard": {"model": "claude-opus-4-8"}}

    updated, default = apply_llm_profile_form(
        profiles,
        selected_name=None,
        entered_name="Lokal",
        payload={"model": "gemma3:12b"},
        default_name="Standard",
        make_default=False,
    )

    assert updated["Lokal"] == {"model": "gemma3:12b"}
    assert default == "Standard"
    assert "Lokal" not in profiles


def test_llm_model_options_use_known_models_and_allow_saved_custom_model() -> None:
    backend = {"models": ["gpt-5", "gpt-5-mini"]}

    assert llm_model_options(backend, "gpt-5-mini") == (
        ["gpt-5", "gpt-5-mini", CUSTOM_MODEL_LABEL],
        "gpt-5-mini",
    )
    assert llm_model_options(backend, "intern-modell") == (
        ["gpt-5", "gpt-5-mini", CUSTOM_MODEL_LABEL],
        CUSTOM_MODEL_LABEL,
    )


def test_llm_form_defaults_reset_dependent_fields_when_service_changes() -> None:
    profile = {
        "backend_name": "OpenAI",
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }
    deepseek = {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "env": "DEEPSEEK_API_KEY",
    }

    assert llm_form_defaults(profile, "DeepSeek", deepseek) == {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
    }
    assert llm_form_defaults(profile, "OpenAI", deepseek) == {
        "model": "gpt-5",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
    }


def test_llm_settings_status_uses_current_environment_field(tmp_path, monkeypatch) -> None:
    app = _llm_settings_app(tmp_path, monkeypatch)
    next(item for item in app.selectbox if item.label == "Tjänst").set_value("OpenAI")
    app.run(timeout=20)
    env_input = next(
        item for item in app.text_input
        if item.label == "Miljövariabel för API-nyckel"
    )
    env_input.set_value("CUSTOM_TOKEN")
    app.run(timeout=20)

    assert not app.exception
    statuses = [item.value for item in app.caption if "API-nyckeln" in item.value]
    assert statuses == ["⚠ API-nyckeln `CUSTOM_TOKEN` saknas i processmiljön."]


def test_llm_settings_status_uses_backend_key_when_override_is_empty(tmp_path, monkeypatch) -> None:
    """En känd molntjänst får inte felaktigt presenteras som nyckelfri."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app = _llm_settings_app(tmp_path, monkeypatch)
    next(item for item in app.selectbox if item.label == "Tjänst").set_value("DeepSeek")
    app.run(timeout=20)
    env_input = next(
        item for item in app.text_input
        if item.label == "Miljövariabel för API-nyckel"
    )
    env_input.set_value("")
    app.run(timeout=20)

    assert not app.exception
    statuses = [item.value for item in app.caption if "API-nyckeln" in item.value]
    assert statuses == ["⚠ API-nyckeln `DEEPSEEK_API_KEY` saknas i processmiljön."]


def test_llm_settings_cancelled_new_profile_starts_clean_next_time(tmp_path, monkeypatch) -> None:
    app = _llm_settings_app(tmp_path, monkeypatch)
    next(item for item in app.button if item.label == "Ny").click()
    app.run(timeout=20)
    next(item for item in app.text_input if item.label == "Namn").set_value("Utkast")
    next(item for item in app.button if item.label == "Avbryt").click()
    app.run(timeout=20)
    next(item for item in app.button if item.label == "Ny").click()
    app.run(timeout=20)

    assert not app.exception
    assert next(item for item in app.text_input if item.label == "Namn").value == ""


def test_llm_settings_uses_provider_model_catalog_when_available(tmp_path, monkeypatch) -> None:
    """Regression: Admin ska visa /v1/models, inte bara den statiska reservlistan."""
    from streamlit.testing.v1 import AppTest

    config_file = tmp_path / "llm_config.json"
    config_file.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    app = AppTest.from_string(
        "from pathlib import Path\n"
        "import admin_ui, backends, config, streamlit as st\n"
        "st.cache_data.clear()\n"
        "backends.fetch_models = lambda base_url, api_key: [\n"
        "    'deepseek-v4-pro', 'deepseek-v4-flash', 'framtida-modell'\n"
        "]\n"
        f"config.CONFIG_FILE = Path({str(config_file)!r})\n"
        "admin_ui.render_llm_settings()\n"
    )
    app.run(timeout=20)
    next(item for item in app.selectbox if item.label == "Tjänst").set_value("DeepSeek")
    app.run(timeout=20)

    model = next(item for item in app.selectbox if item.label == "Modell")
    assert model.options == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "framtida-modell",
        CUSTOM_MODEL_LABEL,
    ]


def test_pipeline_form_reveals_default_llm_profile_only_when_enabled(tmp_path) -> None:
    """Regression: pipelineprofilen ska endast synas och skickas vid LLM-korrigering."""
    from streamlit.testing.v1 import AppTest

    config_file = tmp_path / "llm_config.json"
    config_file.write_text(
        json.dumps(
            {
                "profiles": {"Standard": {}, "DeepSeek": {}},
                "default": "DeepSeek",
            }
        ),
        encoding="utf-8",
    )
    app = AppTest.from_string(
        "from pathlib import Path\n"
        "import admin_ui, config\n"
        "from operations.registry import get_registry\n"
        f"config.CONFIG_FILE = Path({str(config_file)!r})\n"
        "admin_ui.render_operation_form(\n"
        "    get_registry().get('run-pipeline'), settings={'base': '.'}\n"
        ")\n"
    )
    app.run(timeout=20)
    assert not [item for item in app.selectbox if item.label == "LLM-konfiguration"]

    next(item for item in app.checkbox if item.label == "Kör LLM-korrigering").set_value(True)
    app.run(timeout=20)
    profiles = [item for item in app.selectbox if item.label == "LLM-konfiguration"]

    assert not app.exception
    assert len(profiles) == 1
    assert profiles[0].value == "DeepSeek"


def test_job_option_labels_show_operation_status_and_short_id() -> None:
    labels = admin_ui.job_option_labels([
        {"operation": "run-pipeline", "status": "running", "id": "abcdef1234567890",
         "created_at": "2026-01-02T03:04:05+00:00"},
        {"operation": "ingest", "status": "okänd-status", "id": "zzz", "created_at": "igår"},
    ])
    assert labels[0].startswith("run-pipeline · Körs · abcdef12 · ")
    assert labels[1].startswith("ingest · okänd-status · zzz · ")


def test_filter_log_lines_filters_on_level_and_query() -> None:
    text = (
        "2026-01-01 [info] Startar\n"
        "2026-01-01 [warning] Saknar fil\n"
        "2026-01-01 [error] SKIP foo.pdf: trasig\n"
        "fortsättning på felet\n"
        "2026-01-01 [info] Klart\n"
    )

    assert admin_ui.filter_log_lines(text) == text.splitlines()
    assert admin_ui.filter_log_lines(text, level="error") == [
        "2026-01-01 [error] SKIP foo.pdf: trasig",
        "fortsättning på felet",
    ]
    assert admin_ui.filter_log_lines(text, level="warning") == ["2026-01-01 [warning] Saknar fil"]
    # Fortsättningsraden ärver föregående rads nivå och följer med på fritextsökning.
    assert admin_ui.filter_log_lines(text, query="fortsättning") == ["fortsättning på felet"]
    assert admin_ui.filter_log_lines(text, level="info", query="klart") == [
        "2026-01-01 [info] Klart",
    ]


def test_filter_log_lines_recognizes_debug_level() -> None:
    """Debug är en riktig nivå i filtret — annars ärver raden föregående nivå."""
    text = (
        "2026-01-01 [info] Startar\n"
        "2026-01-01 [debug] jämför text_mtime 12.5 mot 12.0\n"
        "2026-01-01 [error] misslyckades\n"
    )
    assert admin_ui.filter_log_lines(text, level="debug") == [
        "2026-01-01 [debug] jämför text_mtime 12.5 mot 12.0",
    ]
    assert "debug" in admin_ui.LOG_LEVEL_LABELS
    assert admin_ui.LOG_LEVEL_LABELS["debug"] == "Debug"


def test_filter_log_lines_treats_level_less_lines_as_errors() -> None:
    """Felloggen har inga nivåmarkörer — alla dess rader är fel."""
    text = "2026-01-01T10:00:00\tdownload\tfile.pdf\tHTTP 404\n"
    assert admin_ui.filter_log_lines(text, level="error") == text.splitlines()
    assert admin_ui.filter_log_lines(text, level="info") == []
    assert admin_ui.filter_log_lines("", level="all") == []


def _admin_page_app(tmp_path, monkeypatch, *, active: bool = True):
    """Rendera adminsidan med ett jobb, en jobblogg och en fellogg."""
    import os
    from datetime import UTC, datetime

    import db

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setattr(admin_ui, "error_log_path", lambda: tmp_path / "errors.log")

    job_log = tmp_path / "jobb.log"
    job_log.write_text(
        "2026-01-01T10:00:00+00:00 [info] Startar pipeline\n"
        "2026-01-01T10:00:05+00:00 [info] Hämtar kalkylbladet\n"
        "2026-01-01T10:00:10+00:00 [warning] Saknar textfil för 123.pdf\n"
        "2026-01-01T10:00:15+00:00 [error] SKIP 456.pdf: trasig PDF\n",
        encoding="utf-8",
    )
    (tmp_path / "errors.log").write_text(
        "2026-01-01T09:00:00\tdownload\t789.pdf\tHTTP 404\n", encoding="utf-8"
    )

    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    db.create_admin_job(
        conn, job_id="job-abc12345", operation="run-pipeline",
        params_json="{}", log_path=str(job_log),
    )
    db.claim_admin_job(conn, "job-abc12345", pid=os.getpid())
    if active:
        conn.execute(
            "UPDATE admin_jobs SET heartbeat_at=? WHERE id='job-abc12345'",
            (datetime.now(UTC).isoformat(),),
        )
    else:
        db.finish_admin_job(conn, "job-abc12345", status="succeeded", exit_code=0)
    conn.commit()
    conn.close()

    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file("src/pages/8_Admin.py")
    # Håll LLM-panelen borta från repots riktiga konfigurationsfil.
    monkeypatch.setattr("config.CONFIG_FILE", tmp_path / "llm_config.json")
    return at.run(timeout=60)


def _shown_log(at) -> str:
    return "\n".join(code.value for code in at.code)


def _select_log_source(at, value: str):
    return next(box for box in at.selectbox if box.key == "log_source").select(value).run()


def test_admin_log_tab_defaults_to_the_running_job(tmp_path, monkeypatch) -> None:
    """Ett enda loggfönster: kör ett jobb → dess logg, annars systemloggen."""
    at = _admin_page_app(tmp_path, monkeypatch)

    assert not list(at.exception)
    assert "Logg" in [tab.label for tab in at.tabs]
    assert [box.value for box in at.selectbox if box.key == "log_source"] == ["job-abc12345"]
    shown = _shown_log(at)
    assert "Startar pipeline" in shown
    assert "SKIP 456.pdf: trasig PDF" in shown
    assert "HTTP 404" not in shown  # felloggen ligger bakom systemloggen


def test_admin_log_tab_can_show_the_system_log(tmp_path, monkeypatch) -> None:
    at = _admin_page_app(tmp_path, monkeypatch)

    at = _select_log_source(at, "system")

    shown = _shown_log(at)
    assert "HTTP 404" in shown
    assert "Startar pipeline" not in shown
    assert any("Systemlogg" in caption.value for caption in at.caption)


def test_admin_log_tab_filters_on_level_and_search(tmp_path, monkeypatch) -> None:
    at = _select_log_source(_admin_page_app(tmp_path, monkeypatch), "system")
    at = _select_log_source(at, "job-abc12345")

    next(box for box in at.selectbox if box.key == "log_level").select("error").run()
    shown = _shown_log(at)
    assert "SKIP 456.pdf: trasig PDF" in shown
    assert "Hämtar kalkylbladet" not in shown
    assert "Saknar textfil" not in shown

    next(box for box in at.selectbox if box.key == "log_level").select("warning").run()
    shown = _shown_log(at)
    assert "Saknar textfil för 123.pdf" in shown
    assert "Startar pipeline" not in shown

    next(box for box in at.selectbox if box.key == "log_level").select("all").run()
    next(box for box in at.text_input if box.key == "log_query").input("kalkylbladet").run()
    shown = _shown_log(at)
    assert "Hämtar kalkylbladet" in shown
    assert "Startar pipeline" not in shown


def test_admin_log_tab_defaults_to_system_log_without_a_running_job(tmp_path, monkeypatch) -> None:
    at = _admin_page_app(tmp_path, monkeypatch, active=False)

    assert [box.value for box in at.selectbox if box.key == "log_source"] == ["system"]
    assert "HTTP 404" in _shown_log(at)


def test_admin_log_tab_survives_a_deleted_job_selection(tmp_path, monkeypatch) -> None:
    """Ett borttaget jobb får inte krascha vyn — valet faller tillbaka."""
    import db

    at = _select_log_source(_admin_page_app(tmp_path, monkeypatch), "job-abc12345")
    with db.connect(tmp_path / "state.db") as conn:
        db.finish_admin_job(conn, "job-abc12345", status="cancelled", exit_code=130)
        db.delete_admin_job(conn, "job-abc12345")

    at.run(timeout=60)
    assert not list(at.exception)
    assert [box.value for box in at.selectbox if box.key == "log_source"] == ["system"]


def test_operation_form_renders_every_parameter_and_the_start_button(tmp_path) -> None:
    """Rutnätet får inte tappa fält eller startknappen."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "import admin_ui\n"
        "from operations.registry import get_registry\n"
        "admin_ui.render_operation_form(get_registry().get('ocr'), settings={'base': '.'})\n"
    ).run(timeout=20)

    assert not app.exception
    keys = {
        widget.key
        for widget in [*app.selectbox, *app.text_input, *app.checkbox,
                       *app.number_input, *app.button]
    }
    for expected in (
        "ocr__mode", "ocr__threshold", "ocr__source", "ocr__skip_redo",
        "ocr__fallback_only", "ocr__jobs", "ocr__start",
    ):
        assert expected in keys
    # Hårdkodade sökvägar injiceras tyst — de ska inte bli egna fält.
    assert "ocr__root" not in keys
    assert "ocr__txt" not in keys


def test_parameter_groups_keep_related_fields_together() -> None:
    """Kryssrutor, inställningar och sökvägar ska hamna i egna grupper."""
    from operations.models import ParameterDefinition

    def param(name: str, kind: str) -> ParameterDefinition:
        return ParameterDefinition(name, (f"--{name}",), kind, "", name)

    parameters = [
        param("skip", "bool"), param("mode", "choice"), param("dry", "bool"),
        param("jobs", "int"), param("inp", "path"),
    ]
    groups = admin_ui.parameter_groups(parameters)

    assert [name for name, _ in groups] == ["Alternativ", "Inställningar", "Sökvägar"]
    by_name = dict(groups)
    assert [parameter.name for parameter in by_name["Alternativ"]] == ["skip", "dry"]
    assert [parameter.name for parameter in by_name["Inställningar"]] == ["mode", "jobs"]
    assert [parameter.name for parameter in by_name["Sökvägar"]] == ["inp"]

    # Kryssrutor en per rad; breda fält först och talfält sist i kompakta rader.
    assert [[p.name for p in row] for row in admin_ui.group_rows("Alternativ", by_name["Alternativ"])] == [
        ["skip"], ["dry"],
    ]
    assert [[p.name for p in row] for row in admin_ui.group_rows("Inställningar", by_name["Inställningar"])] == [
        ["mode"], ["jobs"],
    ]


def test_parameter_groups_skip_empty_groups() -> None:
    from operations.models import ParameterDefinition

    only_bools = [ParameterDefinition("a", ("--a",), "bool", False, "a")]
    assert [name for name, _ in admin_ui.parameter_groups(only_bools)] == ["Alternativ"]
    assert admin_ui.parameter_groups([]) == []


def test_form_labels_headers_and_buttons_come_from_the_layout() -> None:
    assert admin_ui.form_button_label("run-pipeline", "Starta Full pipeline") == "Kör pipeline"
    assert admin_ui.form_button_label("ocr", "Starta Full OCR") == "Starta Full OCR"
    # Utan överstyrning blir operationens etikett rubriken.
    assert admin_ui.form_header("ocr", "Full OCR") == "Full OCR"
    assert admin_ui.form_header("ocr") == ""

    labels = admin_ui.FORM_LAYOUT["run-pipeline"]["labels"]
    assert labels == {
        "skip_wpu": "Hoppa över wpu",
        "skip_redo": "Hoppa över Surya",
        "with_llm": "Kör LLM-korrigering",
        "jobs": "Parallella processer",
        "test_limit": "Testläge: Antal filer att ladda ner",
    }


def test_admin_log_filters_are_stacked_in_a_narrow_column() -> None:
    text = (ROOT / "src" / "admin_ui.py").read_text(encoding="utf-8")
    log_tab = text.split("def render_log_tab", 1)[1]
    assert "st.columns([1, 4]" in log_tab, "filtren ska staplas i en smal kolumn"


def test_number_fields_are_narrow(monkeypatch) -> None:
    """Talen är små (antal filer, trösklar) — fälten ska inte spänna över raden."""
    import streamlit as st

    from operations.models import ParameterDefinition

    calls: list[dict] = []
    labels: list[str] = []
    monkeypatch.setattr(
        st, "number_input",
        lambda label, **kwargs: calls.append(kwargs) or kwargs.get("value"),
    )
    monkeypatch.setattr(
        st, "markdown", lambda body, **kwargs: labels.append(body) or None,
    )

    parameter = ParameterDefinition("jobs", ("--jobs",), "int", 4, "Parallella processer")
    value = admin_ui._render_parameter(
        parameter, widget_key="k", version=0, disabled=False, label="Parallella processer",
    )

    assert value == 4
    assert calls[0]["width"] == admin_ui.FIELD_WIDTHS["int"]
    # Streamlit döljer -/+ under 7.5rem (120 px) + stegknapparnas bredd.
    assert admin_ui.FIELD_WIDTHS["int"] > 150, "stegknapparna försvinner på smala fält"
    assert admin_ui.FIELD_WIDTHS["int"] <= 190, "fältet ska vara smalt, inte fullt brett"
    # Titeln ritas separat så att ``width`` inte tvingar fram radbrytning.
    assert calls[0]["label_visibility"] == "collapsed"
    assert "Parallella processer" in labels[0]


def test_pipeline_tab_shows_only_the_pipeline_operation(tmp_path, monkeypatch) -> None:
    """Nedladdningarna körs som första steg i pipelinen eller via CLI:t."""
    from streamlit.testing.v1 import AppTest

    import db

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    with db.connect(tmp_path / "state.db") as conn:
        db.init_schema(conn)
    monkeypatch.setattr("config.CONFIG_FILE", tmp_path / "llm_config.json")

    app = AppTest.from_file("src/pages/8_Admin.py").run(timeout=60)

    assert not app.exception
    def label_of(widget) -> str:
        return widget.label

    assert [label_of(box) for box in app.checkbox if box.key.startswith("run-pipeline__")] == [
        "Hoppa över wpu", "Hoppa över Surya", "Kör LLM-korrigering",
    ]
    assert [box.label for box in app.number_input if box.key.startswith("run-pipeline__")] == [
        "Parallella processer", "Testläge: Antal filer att ladda ner",
    ]
    assert [button.label for button in app.button if button.key == "run-pipeline__start"] == [
        "Kör pipeline",
    ]

    # Nedladdningsformulären är dolda i adminsidan men finns kvar i CLI:t.
    assert [button.label for button in app.button
            if button.key == "run-pipeline__start"] == ["Kör pipeline"]
    assert not [button for button in app.button
                if button.key in ("download__start", "download-wpu__start")]
    assert not [field for field in app.text_input if field.key == "download__sheet_id"]

    from operations.registry import get_registry

    registry = get_registry()
    assert [definition.id for definition in admin_ui.group_admin_operations(registry)["Pipeline"]] == [
        "run-pipeline",
    ]
    download_flags = {flag for parameter in registry.get("download").parameters
                      for flag in parameter.flags}
    assert "--dry-run" in download_flags and "--rebuild" in download_flags


def test_numeric_rows_are_compact_and_labels_fit() -> None:
    """Två talfält ska ligga intill varandra, med plats för titeln i sin kolumn."""
    from operations.models import ParameterDefinition

    def param(name: str, kind: str, help_text: str) -> ParameterDefinition:
        return ParameterDefinition(name, (f"--{name}",), kind, 0, help_text)

    row = [
        param("jobs", "int", "Parallella processer"),
        param("test_limit", "int", "Testläge: Antal filer att ladda ner"),
    ]

    assert admin_ui.is_numeric_row(row) is True
    widths, total = admin_ui.numeric_row_spec(row, {})

    # Raden har fast bredd: fälten hamnar intill varandra i stället för att
    # spridas ut över en sida i full bredd.
    assert total < 520
    for parameter, width in zip(row, widths, strict=True):
        assert width <= admin_ui.FIELD_WIDTHS["int"] + 120
        assert admin_ui.label_width_px(parameter.help) <= width, "titeln radbryts"
    # Första fältet fyller nästan hela sin kolumn → litet avstånd till nästa.
    assert widths[0] - admin_ui.FIELD_WIDTHS["int"] <= 32

    mixed = [param("inp", "path", "PDF"), param("jobs", "int", "Parallella processer")]
    assert admin_ui.is_numeric_row(mixed) is False


def test_every_admin_section_is_a_card_with_heading_and_help(tmp_path, monkeypatch) -> None:
    """Varje operation ska vara ett eget avsnitt med rubrik och hjälptext."""
    from operations.registry import get_registry

    at = _admin_page_app(tmp_path, monkeypatch, active=False)

    assert not list(at.exception)
    grouped = admin_ui.group_admin_operations(get_registry())
    grouped.pop("LLM-inställningar", None)

    headings = [sub.value for sub in at.subheader]
    captions = [caption.value for caption in at.caption]
    for definitions in grouped.values():
        for definition in definitions:
            if definition.id in admin_ui.NEO4J_OPERATION_IDS:
                # Neo4j-operationerna delar ett kort med egna knappar.
                assert admin_ui.NEO4J_HEADING in headings
                assert any(caption.startswith(admin_ui.NEO4J_HELP.text) for caption in captions)
                continue
            assert admin_ui.form_header(definition.id, definition.label) in headings
            assert any(
                caption.startswith(admin_ui.operation_help(definition.id).text)
                for caption in captions
            )

    # Kortet (border) ger avgränsningen mellan sektionerna.
    source = (ROOT / "src" / "admin_ui.py").read_text(encoding="utf-8")
    form = source.split("def render_operation_form", 1)[1].split("def render_active_job", 1)[0]
    assert "st.container(border=True)" in form


def test_form_fields_are_grouped_in_render_order() -> None:
    """Kryssrutor får inte blandas med val och talfält i samma rutnät."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "import admin_ui\n"
        "from operations.registry import get_registry\n"
        "admin_ui.render_operation_form(get_registry().get('ocr-pages'), settings={'base': '.'})\n"
    ).run(timeout=20)

    assert not app.exception
    kinds = [type(element).__name__ for element in list(app.main)]
    headings = [getattr(element, "value", "") for element in list(app.main)]

    first_settings = headings.index("##### Inställningar")
    first_paths = headings.index("##### Sökvägar")
    assert headings.index("##### Alternativ") < first_settings < first_paths
    # Alla kryssrutor ligger i Alternativ-gruppen, före val/talfälten.
    assert "Checkbox" not in kinds[first_settings:first_paths + 1]
    checkboxes = [index for index, kind in enumerate(kinds) if kind == "Checkbox"]
    assert checkboxes and max(checkboxes) < first_settings


def _column_index_of(app, predicate) -> int | None:
    """Hitta vilken kolumn (0 = vänster) ett element ligger i."""
    def walk(node, depth: int = 0, column: int | None = None) -> int | None:
        if depth > 4:
            return None
        try:
            children = list(node.children.values())
        except AttributeError:
            return None
        for index, child in enumerate(children):
            name = type(child).__name__
            current = index if name == "Column" else column
            if predicate(child):
                return current
            found = walk(child, depth + 1, current)
            if found is not None:
                return found
        return None

    return walk(app.main)


def test_legacy_llm_fields_are_hidden_for_every_operation_with_a_profile() -> None:
    """Provider/modell/URL styrs av LLM-konfigurationen och ska inte visas."""
    from operations.registry import get_registry

    registry = get_registry()
    for operation_id in ("llm-correct", "extract-entities", "extract-map-observations"):
        definition = registry.get(operation_id)
        names = [
            parameter.name
            for parameter in admin_ui._visible_parameters(definition, operation_id)
        ]
        assert "profile" in names
        assert not set(names) & set(admin_ui.LEGACY_LLM_PARAMS)
        # Flaggorna finns kvar i registret så att CLI:t fungerar som förut.
        flags = {flag for parameter in definition.parameters for flag in parameter.flags}
        assert {"--provider", "--model", "--base-url"} <= flags


def test_no_admin_form_shows_a_legacy_llm_field(tmp_path, monkeypatch) -> None:
    """Provider, modell och API-URL styrs av LLM-konfigurationen — inga fält."""
    at = _admin_page_app(tmp_path, monkeypatch, active=False)

    assert not list(at.exception)
    labels = [
        widget.label
        for widget in [*at.text_input, *at.selectbox, *at.number_input, *at.checkbox,
                       *at.toggle, *at.radio]
    ]
    removed = {"LLM-provider", "Modellnamn", "API-URL"}
    assert not set(labels) & removed, f"kvar i adminformulären: {sorted(set(labels) & removed)}"
    # Profilväljaren finns kvar i varje operation som har en profilparameter.
    from operations.registry import get_registry

    visible_profiles = [
        definition for definition in get_registry().admin_operations()
        if any(
            parameter.name == "profile"
            for parameter in admin_ui._visible_parameters(definition, definition.id)
        )
    ]
    assert labels.count("LLM-konfiguration") == len(visible_profiles)


def test_section_help_is_rendered_to_the_right_of_the_fields() -> None:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(
        "import admin_ui\n"
        "from operations.registry import get_registry\n"
        "admin_ui.render_operation_form(get_registry().get('ocr-pages'), settings={'base': '.'})\n"
    ).run(timeout=20)

    assert not app.exception
    help_info = admin_ui.operation_help("ocr-pages")
    help_captions = [
        element.value for element in app.caption
        if element.value.startswith(help_info.text)
    ]
    assert len(help_captions) == 1, "förklaring och punktlista ska vara en hjälptext"
    assert "- **Motor** —" in help_captions[0]
    for _, meaning in help_info.options:
        assert f"— {meaning}" in help_captions[0]
    assert _column_index_of(
        app, lambda element: type(element).__name__ == "Caption"
        and element.value.startswith(help_info.text),
    ) == 1, "hjälpen ska ligga i högerkolumnen"
    assert _column_index_of(
        app, lambda element: type(element).__name__ == "Subheader",
    ) == 0, "rubriken hör till fältkolumnen"


def test_every_admin_operation_has_a_help_text() -> None:
    from operations.registry import get_registry

    # LLM-inställningar renderas av panelen i Inställningar, inte som ett kort.
    missing = [
        definition.id
        for definition in get_registry().admin_operations()
        if definition.group != "LLM-inställningar"
        and not admin_ui.operation_help(definition.id).text
    ]
    assert missing == [], "lägg till en kort förklaring i OPERATION_HELP"


def test_help_explains_every_field_in_every_operation() -> None:
    """Alla fält som visas ska ha en punkt, och inga punkter ska peka fel."""
    from operations.registry import get_registry

    for definition in get_registry().admin_operations():
        if definition.group == "LLM-inställningar":
            continue
        fields = [parameter.name for parameter in admin_ui.form_fields(definition)]
        options = admin_ui.operation_help(definition.id).options
        explained = dict(options)

        missing = [name for name in fields if name not in explained]
        assert missing == [], f"{definition.id} saknar förklaring för {missing}"

        parameter_names = {parameter.name for parameter in definition.parameters}
        stale = [name for name, _ in options if name not in parameter_names]
        assert stale == [], f"{definition.id} förklarar okända fält {stale}"

        # Fält som aldrig renderas (tysta sökvägar) ska inte ha någon punkt.
        silent = admin_ui._silent_parameters(list(definition.parameters))
        assert not set(explained) & silent, f"{definition.id} förklarar dolda fält"


def test_help_bullets_follow_the_field_order() -> None:
    """Punktlistan ska ha samma ordning som fälten i formuläret."""
    from streamlit.testing.v1 import AppTest

    for operation_id in ("ocr", "ocr-pages", "merge-wpu", "ingest", "run-pipeline"):
        app = AppTest.from_string(
            "import admin_ui\n"
            "from operations.registry import get_registry\n"
            f"admin_ui.render_operation_form(get_registry().get('{operation_id}'), "
            "settings={'base': '.'})\n"
        ).run(timeout=20)
        assert not app.exception

        widget_labels = [
            element.label for element in list(app.main)
            if type(element).__name__ in ("Checkbox", "Selectbox", "NumberInput", "TextInput")
        ]
        bullet_labels = [
            line.removeprefix("- **").split("** —")[0]
            for caption in app.caption
            for line in caption.value.splitlines()
            if line.startswith("- **")
        ]
        assert bullet_labels == widget_labels, f"{operation_id}: punkterna följer inte fälten"


def test_neo4j_operations_share_one_card_with_three_buttons(tmp_path, monkeypatch) -> None:
    """Starta/Stopp/Status ska vara en sektion, inte tre separata kort."""
    at = _admin_page_app(tmp_path, monkeypatch, active=False)

    assert not list(at.exception)
    assert [sub.value for sub in at.subheader].count(admin_ui.NEO4J_HEADING) == 1
    buttons = {
        button.key: button.label for button in at.button
        if button.key in {f"{operation_id}__start" for operation_id in admin_ui.NEO4J_OPERATION_IDS}
    }
    assert buttons == {
        "neo4j-start__start": "Starta",
        "neo4j-stop__start": "Stopp",
        "neo4j-status__start": "Status",
    }
    # Den gemensamma hjälpen förklarar alla tre knapparna.
    expected = admin_ui.section_help_markdown(admin_ui.NEO4J_HELP.text, [
        f"- **{label}** — {meaning}" for label, meaning in admin_ui.NEO4J_HELP.options
    ])
    assert [c.value for c in at.caption if c.value == expected] == [expected]

    # Sektionen ligger först i graf-fliken: grafen kräver en startad databas.
    def position(predicate) -> int:
        for index, element in enumerate(list(at.main)):
            if predicate(element):
                return index
        raise AssertionError("elementet saknas")

    graph_tab = position(
        lambda element: type(element).__name__ == "Tab"
        and element.label == "Extraktion och graf"
    )
    neo4j = position(
        lambda element: type(element).__name__ == "Subheader"
        and element.value == admin_ui.NEO4J_HEADING
    )
    first_operation = position(
        lambda element: type(element).__name__ == "Subheader"
        and element.value == "Entitetsextraktion"
    )
    assert graph_tab < neo4j < first_operation
    assert neo4j > position(lambda element: type(element).__name__ == "Tab")


def test_neo4j_buttons_start_their_operation(tmp_path, monkeypatch) -> None:
    from operations import job_service

    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        job_service, "start_job",
        lambda operation_id, params, **kwargs: calls.append((operation_id, params)) or {"id": "jobb"},
    )
    at = _admin_page_app(tmp_path, monkeypatch, active=False)
    assert not list(at.exception)
    next(button for button in at.button if button.key == "neo4j-status__start").click().run()

    assert calls == [("neo4j-status", {})]


def _archive(tmp_path):
    """Minimalt arkiv: PDF:er, textfiler och en sida i state.db."""
    import db

    (tmp_path / "downloaded" / "files").mkdir(parents=True)
    (tmp_path / "downloaded" / "wpu_files").mkdir(parents=True)
    (tmp_path / "generated" / "text").mkdir(parents=True)
    (tmp_path / "downloaded" / "files" / "b — dokument — 2021.pdf").write_bytes(b"%PDF")
    (tmp_path / "downloaded" / "files" / "a — dokument — 2020.pdf").write_bytes(b"%PDF")
    (tmp_path / "downloaded" / "wpu_files" / "wpu — dokument.pdf").write_bytes(b"%PDF")
    (tmp_path / "generated" / "text" / "b — dokument.txt").write_text("text")

    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    db.record_page(
        conn, pdf_stem="b — dokument", page_num=1, engine="surya", text="x", score=80.0,
    )
    conn.close()
    return tmp_path


def test_file_choices_read_the_directories_the_function_uses(tmp_path) -> None:
    _archive(tmp_path)
    settings = {"base": str(tmp_path)}

    pdfs = admin_ui.file_choices(
        admin_ui.FILE_PICKERS[("ocr-pages", "in")]["sources"], "*.pdf", settings=settings,
    )
    assert [label for _, label in pdfs] == [
        "a — dokument — 2020", "b — dokument — 2021", "wpu — dokument",
    ]
    assert str(tmp_path / "downloaded" / "files" / "a — dokument — 2020.pdf") == pdfs[0][0]

    texts = admin_ui.file_choices(("generated/text",), "*.txt", settings=settings)
    assert [label for _, label in texts] == ["b — dokument"]
    # Okänd katalog ger inga val i stället för att krascha.
    assert admin_ui.file_choices(("generated/saknas",), "*.txt", settings=settings) == []


def _picker_form(tmp_path, monkeypatch, operation_id):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    return AppTest.from_string(
        "from pathlib import Path\n"
        "import admin_ui\n"
        "from operations.registry import get_registry\n"
        f"admin_ui.render_operation_form(\n"
        f"    get_registry().get('{operation_id}'), settings={{'base': {str(tmp_path)!r}}}\n"
        ")\n"
    ).run(timeout=20)


def test_file_pickers_replace_the_text_fields(tmp_path, monkeypatch) -> None:
    _archive(tmp_path)

    pdf_form = _picker_form(tmp_path, monkeypatch, "ocr-pages")
    pickers = {box.key: list(box.options) for box in pdf_form.selectbox}
    assert "ocr-pages__in" in pickers, "PDF-filen ska väljas, inte skrivas"
    assert not [field for field in pdf_form.text_input if field.key.startswith("ocr-pages__in")]
    assert pickers["ocr-pages__in"] == [
        "— välj PDF —",
        "a — dokument — 2020",
        "b — dokument — 2021",
        "wpu — dokument",
    ]

    stem_form = _picker_form(tmp_path, monkeypatch, "merge-pages")
    stems = next(box for box in stem_form.selectbox if box.key == "merge-pages__stem")
    assert [label for label in stems.options] == ["— välj dokument —", "b — dokument"]
    assert stems.label == "Dokument"

    text_form = _picker_form(tmp_path, monkeypatch, "llm-correct")
    texts = next(box for box in text_form.selectbox if box.key == "llm-correct__test")
    assert list(texts.options) == ["— välj textfil —", "b — dokument"]
    assert texts.label == "Textfil att rätta"


def test_picked_file_is_passed_as_a_path(tmp_path, monkeypatch) -> None:
    """Valet i väljaren ska bli rätt värde till operationen."""
    from streamlit.testing.v1 import AppTest

    _archive(tmp_path)
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    app = AppTest.from_string(
        "import json\n"
        "import streamlit as st\n"
        "import admin_ui\n"
        "from operations.registry import get_registry\n"
        f"params = admin_ui.render_operation_form(\n"
        f"    get_registry().get('ocr-pages'), settings={{'base': {str(tmp_path)!r}}}\n"
        ")\n"
        "if params:\n"
        "    st.code(json.dumps(params, default=str, ensure_ascii=False), language='json')\n"
    ).run(timeout=20)
    pdf = tmp_path / "downloaded" / "files" / "a — dokument — 2020.pdf"

    next(box for box in app.selectbox if box.key == "ocr-pages__in").select(str(pdf)).run()
    assert not list(app.error)  # valet fyller det obligatoriska fältet
    next(button for button in app.button if button.key == "ocr-pages__start").click().run()

    assert any(str(pdf) in code.value for code in app.code)


def test_missing_required_field_names_the_label(tmp_path, monkeypatch) -> None:
    """Felmeddelandet ska säga vilket fält som saknas, inte parameternamnet."""
    _archive(tmp_path)

    form = _picker_form(tmp_path, monkeypatch, "ocr-pages")

    assert [error.value for error in form.error] == ["Välj ett värde för: PDF-fil"]
