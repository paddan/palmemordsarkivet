"""Tester för operations/cli.py — förgrundskörningens felhantering."""

from __future__ import annotations

from operations.cli import run_operation_cli
from operations.exceptions import OperationCancelled, OperationFailed
from operations.registry import get_registry


def _register_throwing_operation(monkeypatch, exc: Exception) -> None:
    """Registrera en tillfällig operation som kastar ``exc`` vid körning."""

    def _throw(context, params) -> None:
        raise exc

    from operations.models import OperationDefinition

    definition = OperationDefinition(
        id="kastar",
        label="Kastar",
        group="test",
        description="Testoperation som kastar.",
        parameters=(),
        admin_visible=False,
        mutating=False,
        confirmation=None,
        run=_throw,
    )
    monkeypatch.setattr(get_registry(), "_definitions", {"kastar": definition})


def test_nonzero_operation_result_ger_exitkod_1(monkeypatch, capsys) -> None:
    """En domän-runner som rapporterar fel får inte översättas till succé."""
    from operations.models import OperationDefinition

    definition = OperationDefinition(
        id="returnerar-fel",
        label="Returnerar fel",
        group="test",
        description="Testoperation som returnerar ett felresultat.",
        parameters=(),
        admin_visible=False,
        mutating=False,
        confirmation=None,
        run=lambda context, params: 1,
    )
    monkeypatch.setattr(get_registry(), "_definitions", {"returnerar-fel": definition})

    assert run_operation_cli("returnerar-fel", []) == 1
    assert "misslyckades" in capsys.readouterr().err


def test_operation_failed_ger_exitkod_1_utan_traceback(monkeypatch, capsys):
    _register_throwing_operation(monkeypatch, OperationFailed("Steget ingest misslyckades"))
    rc = run_operation_cli("kastar", [])
    assert rc == 1
    assert "Steget ingest misslyckades" in capsys.readouterr().err


def test_operation_cancelled_ger_exitkod_130(monkeypatch, capsys):
    _register_throwing_operation(monkeypatch, OperationCancelled("avbruten"))
    rc = run_operation_cli("kastar", [])
    assert rc == 130
    assert "avbruten" in capsys.readouterr().err


def test_okand_operation_ger_exitkod_2(monkeypatch, capsys):
    rc = run_operation_cli("finns-inte", [])
    assert rc == 2
    assert "finns-inte" in capsys.readouterr().err


def test_okand_flagga_ger_exitkod_2():
    # argparse avslutar med SystemExit(2) vid okänd flagga; run_operation_cli
    # fångar SystemExit och returnerar dess kod.
    rc = run_operation_cli("quality", ["--denna-flagga-finns-inte"])
    assert rc == 2


def test_merge_pages_validation_ger_exitkod_2_utan_traceback(capsys):
    rc = run_operation_cli("merge-pages", [])

    assert rc == 2
    captured = capsys.readouterr()
    assert "--stem" in captured.err
    assert "Traceback" not in captured.err
