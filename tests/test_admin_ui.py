"""Tester för rena adminhelpers i src/admin_ui.py."""

from __future__ import annotations

from dataclasses import replace

import admin_ui
from admin_ui import (
    ROOT,
    UNCHANGED_CHOICE_LABEL,
    choice_form_options,
    format_job_status,
    group_admin_operations,
    load_settings,
    missing_required_paths,
    normalize_choice_selection,
    numeric_input_bounds,
    progress_fraction,
    resolve_path_default,
)
from operations.models import OperationDefinition, ParameterDefinition
from operations.registry import OperationRegistry


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
    assert numeric_input_bounds("jobs", "int") == (1.0, None)
    assert numeric_input_bounds("per_file_jobs", "int") == (1.0, None)
    assert numeric_input_bounds("chunk_chars", "int") == (1.0, None)
    assert numeric_input_bounds("batch", "int") == (1.0, None)
    assert numeric_input_bounds("dpi", "int") == (1.0, None)
    assert numeric_input_bounds("limit", "int") == (0.0, None)
    assert numeric_input_bounds("test_limit", "int") == (0.0, None)
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
