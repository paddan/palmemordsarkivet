"""Tester för rena adminhelpers i src/admin_ui.py."""

from __future__ import annotations

import json
from dataclasses import replace

import admin_ui
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


def _llm_settings_app(tmp_path) -> object:
    from streamlit.testing.v1 import AppTest

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
    # Endast base-path lagras; underkatalogerna är hårdkodade.
    assert settings == {"base": str(ROOT)}


def test_resolve_path_default_joins_base_and_relative() -> None:
    files_default = ROOT / "downloaded" / "files"
    # Underkatalogen (downloaded/files) är hårdkodad — bara base-path ändras.
    assert resolve_path_default(files_default, {"base": str(ROOT)}) == str(ROOT / "downloaded" / "files")
    assert resolve_path_default(files_default, {"base": "/tmp/base"}) == "/tmp/base/downloaded/files"
    # Icke-systemväg lämnas orörd.
    assert resolve_path_default("/annan/väg", {"base": "/tmp"}) == "/annan/väg"


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


def test_llm_settings_status_uses_current_environment_field(tmp_path) -> None:
    app = _llm_settings_app(tmp_path)
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
    app = _llm_settings_app(tmp_path)
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


def test_llm_settings_cancelled_new_profile_starts_clean_next_time(tmp_path) -> None:
    app = _llm_settings_app(tmp_path)
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
