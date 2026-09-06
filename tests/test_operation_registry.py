"""Tester för operationsdefinitioner, registry och förgrunds-CLI."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from operations.cli import parse_operation_args, run_operation_cli
from operations.models import OperationDefinition, ParameterDefinition
from operations.registry import OperationRegistry, get_registry


def _definition(*, operation_id: str = "sample", admin_visible: bool = True) -> OperationDefinition:
    return OperationDefinition(
        id=operation_id,
        label="Prov",
        group="Pipeline",
        description="Testoperation",
        parameters=(
            ParameterDefinition("jobs", ("--jobs",), "int", 4, "Antal jobb"),
            ParameterDefinition(
                "mode",
                ("--mode",),
                "choice",
                "pages",
                "Läge",
                choices=("pages", "files"),
            ),
        ),
        admin_visible=admin_visible,
        mutating=True,
        confirmation=None,
        run=lambda context, params: None,
    )


def test_same_definition_drives_cli_and_admin_metadata() -> None:
    definition = _definition()

    assert parse_operation_args(definition, ["--jobs", "8", "--mode", "files"]) == {
        "jobs": 8,
        "mode": "files",
    }
    assert definition.parameters[0].default == 4


def test_full_pipeline_declares_optional_llm_profile() -> None:
    definition = get_registry().get("run-pipeline")

    assert next(parameter for parameter in definition.parameters if parameter.name == "profile").default == ""


def test_secret_parameter_is_rejected_for_background_serialization() -> None:
    parameter = ParameterDefinition(
        "api_key", ("--api-key",), "str", "", "API-nyckel", secret=True
    )

    with pytest.raises(ValueError, match="miljövariabel"):
        parameter.validate_background_value("hemlig")


def test_path_parameter_normalizes_values_to_path() -> None:
    definition = OperationDefinition(
        id="path-test",
        label="Sökväg",
        group="Pipeline",
        description="Testar sökvägar",
        parameters=(
            ParameterDefinition("output", ("--output",), "path", "generated/text", "Utkatalog"),
        ),
        admin_visible=True,
        mutating=False,
        confirmation=None,
        run=lambda context, params: None,
    )

    assert parse_operation_args(definition, ["--output", "generated/nytt"]) == {
        "output": Path("generated/nytt"),
    }


def test_choice_parameter_rejects_values_outside_definition() -> None:
    definition = _definition()

    with pytest.raises(SystemExit) as exc_info:
        parse_operation_args(definition, ["--mode", "okänt"])

    assert exc_info.value.code == 2


def test_registry_rejects_duplicate_ids_and_sorts_visible_operations() -> None:
    registry = OperationRegistry()
    hidden = _definition(operation_id="hidden", admin_visible=False)
    first = replace(_definition(operation_id="first"), label="Alfa", group="Z")
    second = replace(_definition(operation_id="second"), label="Beta", group="A")
    registry.register(first)
    registry.register(hidden)
    registry.register(second)

    with pytest.raises(ValueError, match="first"):
        registry.register(first)
    with pytest.raises(KeyError, match="Okänd operation"):
        registry.get("saknas")

    assert [operation.id for operation in registry.admin_operations()] == ["second", "first"]


def test_cli_runs_registered_definition_without_mutating_sys_argv(monkeypatch) -> None:
    registry = OperationRegistry()
    received: dict[str, object] = {}

    def run(context, params) -> None:
        received["context"] = context
        received["params"] = params

    definition = replace(_definition(), run=run)
    registry.register(definition)
    monkeypatch.setattr("operations.cli.get_registry", lambda: registry)

    context_module = ModuleType("operations.context")

    class TerminalSink:
        pass

    class OperationContext:
        def __init__(self, *, sink, cancel_requested) -> None:
            self.sink = sink
            self.cancel_requested = cancel_requested

    context_module.OperationContext = OperationContext
    context_module.TerminalSink = TerminalSink
    monkeypatch.setitem(sys.modules, "operations.context", context_module)
    original_argv = sys.argv.copy()

    assert run_operation_cli("sample", ["--jobs", "9"]) == 0
    assert received["params"] == {"jobs": 9, "mode": "pages"}
    assert isinstance(received["context"], OperationContext)
    assert sys.argv == original_argv


def test_cli_returns_2_for_invalid_operation_arguments(monkeypatch) -> None:
    registry = OperationRegistry()
    registry.register(_definition())
    monkeypatch.setattr("operations.cli.get_registry", lambda: registry)

    assert run_operation_cli("sample", ["--mode", "okänt"]) == 2


def test_cli_returns_130_when_operation_is_interrupted(monkeypatch) -> None:
    registry = OperationRegistry()

    def interrupt(context, params) -> None:
        raise KeyboardInterrupt

    registry.register(replace(_definition(), run=interrupt))
    monkeypatch.setattr("operations.cli.get_registry", lambda: registry)

    context_module = ModuleType("operations.context")
    context_module.TerminalSink = lambda: object()
    context_module.OperationContext = lambda **kwargs: object()
    monkeypatch.setitem(sys.modules, "operations.context", context_module)

    assert run_operation_cli("sample", []) == 130


def test_bootstrap_prioritizes_source_tree_and_forwards_terminal_arguments(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    import scripts._bootstrap as bootstrap

    received: dict[str, object] = {}
    cli_module = ModuleType("operations.cli")

    def fake_run(operation_id, argv) -> int:
        received["operation_id"] = operation_id
        received["argv"] = argv
        return 0

    cli_module.run_operation_cli = fake_run
    monkeypatch.setitem(sys.modules, "operations.cli", cli_module)
    monkeypatch.setattr(sys, "argv", ["scripts/prov.py", "--jobs", "3"])

    assert bootstrap.run("sample") == 0
    assert sys.path[0] == str(bootstrap.SRC)
    assert received == {"operation_id": "sample", "argv": ["--jobs", "3"]}


# ---------------------------------------------------------------------------
# Inbyggda registryn: defaults och exponerade flaggor
# ---------------------------------------------------------------------------

def _flags(operation_id: str) -> set[str]:
    from operations.registry import get_registry

    definition = get_registry().get(operation_id)
    return {flag for parameter in definition.parameters for flag in parameter.flags}


def _defaults(operation_id: str) -> dict[str, object]:
    from operations.registry import get_registry

    definition = get_registry().get(operation_id)
    return {parameter.name: parameter.default for parameter in definition.parameters}


def test_builtin_ingest_defaults_match_ingest_constants() -> None:
    from rag import ingest

    defaults = _defaults("ingest")

    assert defaults["model"] == ingest.MODEL_NAME
    assert defaults["chunk_chars"] == ingest.CHUNK_CHARS == 800
    assert defaults["chunk_overlap"] == ingest.CHUNK_OVERLAP == 150


def test_builtin_quality_per_page_default_is_opt_in() -> None:
    # Gamla CLI:n var opt-in (--per-page) — default ska vara False.
    assert _defaults("quality")["per_page"] is False


def test_ocr_operation_exposes_legacy_flags() -> None:
    flags = _flags("ocr")
    for flag in (
        "--redo", "--mode", "--source", "--no-update-pdf", "--in", "--ocr",
        "--txt", "--pages-out", "--from-list", "--retry-failed", "--root",
    ):
        assert flag in flags, f"ocr-operationen saknar {flag}"


def test_ocr_tesseract_operation_exposes_legacy_flags() -> None:
    flags = _flags("ocr-tesseract")
    for flag in (
        "--ocr", "--txt", "--tessdata", "--user-words", "--user-words-auto",
        "--tess-config", "--psm", "--langs", "--min-text-chars", "--image-dpi",
        "--errors-log",
    ):
        assert flag in flags, f"ocr-tesseract-operationen saknar {flag}"


def test_ingest_operation_exposes_reindex_since() -> None:
    assert "--reindex-since" in _flags("ingest")


def test_graph_review_llm_uses_named_profile_and_bounded_page_count() -> None:
    assert _flags("graph-review-llm") == {"--profile", "--limit"}
    defaults = _defaults("graph-review-llm")
    assert defaults == {"profile": "", "limit": 0}


@pytest.mark.parametrize(
    "operation_id, expected_flags",
    (
        (
            "llm-correct",
            {
                "--provider", "--model", "--base-url", "--api-key", "--test",
                "--threshold", "--txt", "--root", "--jobs", "--dry-run",
            },
        ),
        (
            "extract-entities",
            {
                "--provider", "--model", "--base-url", "--api-key",
                "--text-dir", "--limit", "--dry-run", "--jobs", "--timeout",
            },
        ),
        (
            "extract-map-observations",
            {
                "--provider", "--model", "--base-url", "--api-key",
                "--text-dir", "--limit", "--dry-run", "--jobs", "--timeout",
            },
        ),
    ),
)
def test_llm_operations_expose_all_legacy_flags(
    operation_id: str, expected_flags: set[str]
) -> None:
    assert expected_flags <= _flags(operation_id)


def test_legacy_llm_flag_defaults_and_types_are_preserved() -> None:
    from operations.registry import get_registry

    definition = get_registry().get("llm-correct")
    params = parse_operation_args(
        definition,
        [
            "--provider", "openai", "--model", "gpt-test", "--base-url",
            "https://example.invalid/v1", "--api-key", "secret", "--test", "doc.txt",
        ],
    )

    assert params["provider"] == "openai"
    assert params["model"] == "gpt-test"
    assert params["base_url"] == "https://example.invalid/v1"
    assert params["api_key"] == "secret"
    assert params["test"] == "doc.txt"


@pytest.mark.parametrize(
    "operation_id",
    (
        "build-user-words", "detect-redactions", "download", "download-wpu",
        "ingest", "merge-wpu", "ocr-pages", "ocr-tesseract", "quality",
    ),
)
def test_operations_wrapped_with_legacy_root_expose_root(operation_id: str) -> None:
    assert "--root" in _flags(operation_id)


def test_merge_wpu_preserves_legacy_defaults() -> None:
    import merge_wpu

    defaults = _defaults("merge-wpu")

    assert defaults["margin"] == merge_wpu.DEFAULT_MARGIN == 5
    assert defaults["jobs"] == max(1, os.cpu_count() or 4)


def test_merge_pages_requires_exactly_one_target() -> None:
    from operations.registry import get_registry

    definition = get_registry().get("merge-pages")
    with pytest.raises(ValueError, match="--stem.*--all"):
        parse_operation_args(definition, [])
    with pytest.raises(ValueError, match="--stem.*--all"):
        parse_operation_args(definition, ["--stem", "doc", "--all"])


def test_parallelism_defaults_preserve_legacy_environment_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    code = """
from operations.registry import get_registry
for operation_id in ('run-pipeline', 'ocr', 'ocr-tesseract', 'detect-redactions',
                     'llm-correct', 'extract-entities', 'extract-map-observations'):
    definition = get_registry().get(operation_id)
    defaults = {parameter.name: parameter.default for parameter in definition.parameters}
    print(operation_id, defaults['jobs'])
print('ocr-per-file', {p.name: p.default for p in get_registry().get('ocr').parameters}['per_file_jobs'])
"""
    env = os.environ.copy()
    env.update({"JOBS": "7", "PER_FILE_JOBS": "3", "PYTHONPATH": str(root / "src")})

    result = subprocess.run(
        [sys.executable, "-c", code], cwd=root, env=env, text=True,
        capture_output=True, check=True,
    )

    assert result.stdout.splitlines() == [
        "run-pipeline 7", "ocr 7", "ocr-tesseract 7", "detect-redactions 7",
        "llm-correct 7", "extract-entities 7", "extract-map-observations 7",
        "ocr-per-file 3",
    ]
