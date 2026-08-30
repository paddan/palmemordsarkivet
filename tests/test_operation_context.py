"""Tester för OperationContext, processgrupper och kontrollerad avbrytning."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from operations.context import (
    OperationContext,
    ProgressSink,
    TerminalSink,
    _redact_argv,
    _redact_env,
)
from operations.exceptions import OperationCancelled, OperationFailed
from operations.models import ProgressUpdate


class RecordingSink(ProgressSink):
    """Spelar in loggrader (level, message) och progressuppdateringar."""

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []
        self.progress: list[ProgressUpdate] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))

    def write_progress(self, update: ProgressUpdate) -> None:
        self.progress.append(update)

    def write_traceback(self, exc: BaseException) -> None:
        self.logs.append(("error", f"TRACEBACK: {exc}"))

    def info_messages(self) -> list[str]:
        return [message for level, message in self.logs if level == "info"]


def test_run_process_streams_output(tmp_path: Path) -> None:
    sink = RecordingSink()
    context = OperationContext(sink=sink, cancel_requested=lambda: False)

    rc = context.run_process(
        [sys.executable, "-c", "print('rad ett'); print('rad två')"],
        cwd=tmp_path,
    )

    assert rc == 0
    assert sink.info_messages() == ["rad ett", "rad två"]


def test_run_process_logs_redacted_command_without_secret_values(tmp_path: Path) -> None:
    sink = RecordingSink()
    context = OperationContext(sink=sink, cancel_requested=lambda: False)

    context.run_process(
        [sys.executable, "-c", "pass", "--api-key", "hemlig-token"],
        cwd=tmp_path,
    )

    debug_lines = [message for level, message in sink.logs if level == "debug"]
    assert debug_lines, "förväntade ett debug-kommandologg"
    assert "hemlig-token" not in debug_lines[0]
    assert "***" in debug_lines[0]


def test_check_cancelled_raises_when_requested() -> None:
    context = OperationContext(sink=RecordingSink(), cancel_requested=lambda: True)

    with pytest.raises(OperationCancelled):
        context.check_cancelled()


def test_step_and_progress_track_current_step() -> None:
    sink = RecordingSink()
    context = OperationContext(sink=sink, cancel_requested=lambda: False)

    context.step("Prov", total=2)
    context.progress(1, 2, "Halvvägs")

    assert [update.step for update in sink.progress] == ["Prov", "Prov"]
    assert sink.progress[0].total == 2
    assert sink.progress[1].completed == 1
    assert sink.progress[1].message == "Halvvägs"


def test_cancel_terminates_process_group(tmp_path: Path) -> None:
    cancel = threading.Event()
    sink = RecordingSink()
    context = OperationContext(
        sink=sink,
        cancel_requested=cancel.is_set,
        terminate_grace_seconds=0.2,
    )

    pidfile = tmp_path / "pid.txt"
    code = (
        "import os, time, pathlib; "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )

    result: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            context.run_process([sys.executable, "-c", code], cwd=tmp_path)
        except BaseException as exc:  # noqa: BLE001 - vi fångar allt för att inspektera
            result["exc"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    for _ in range(200):
        if pidfile.exists():
            break
        time.sleep(0.05)
    assert pidfile.exists(), "barnprocessen startade aldrig"
    pid = int(pidfile.read_text(encoding="utf-8"))

    cancel.set()
    thread.join(timeout=10)
    assert not thread.is_alive(), "run_process återvände aldrig efter cancel"
    assert isinstance(result["exc"], OperationCancelled)

    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_keyboard_interrupt_terminates_process_group(tmp_path: Path, monkeypatch) -> None:
    """Regression: Ctrl-C får inte lämna barnprocesser föräldralösa."""
    sink = RecordingSink()
    context = OperationContext(
        sink=sink,
        cancel_requested=lambda: False,
        terminate_grace_seconds=0.2,
    )

    pidfile = tmp_path / "pid.txt"
    code = (
        "import os, time, pathlib; "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )

    def check_until_running() -> None:
        if pidfile.exists():
            raise KeyboardInterrupt

    monkeypatch.setattr(context, "check_cancelled", check_until_running)

    result: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            context.run_process([sys.executable, "-c", code], cwd=tmp_path)
        except BaseException as exc:  # noqa: BLE001 - vi fångar allt för att inspektera
            result["exc"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()

    for _ in range(200):
        if pidfile.exists():
            break
        time.sleep(0.05)
    assert pidfile.exists(), "barnprocessen startade aldrig"
    pid = int(pidfile.read_text(encoding="utf-8"))

    thread.join(timeout=10)
    assert not thread.is_alive(), "run_process återvände aldrig efter Ctrl-C"
    assert isinstance(result["exc"], KeyboardInterrupt)

    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_run_process_returns_nonzero_exit_code(tmp_path: Path) -> None:
    context = OperationContext(sink=RecordingSink(), cancel_requested=lambda: False)

    rc = context.run_process(
        [sys.executable, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
        cwd=tmp_path,
    )

    assert rc == 3


def test_run_process_reads_trailing_output(tmp_path: Path) -> None:
    """Regression: all output läses även när processen skriver mycket före avslut."""
    sink = RecordingSink()
    context = OperationContext(sink=sink, cancel_requested=lambda: False)

    lines = 3000
    code = f"for i in range({lines}): print(f'rad {{i}}')"
    rc = context.run_process([sys.executable, "-c", code], cwd=tmp_path)

    assert rc == 0
    assert len(sink.info_messages()) == lines
    assert sink.info_messages()[-1] == f"rad {lines - 1}"


def test_run_process_fails_when_grandchild_holds_pipe_open(tmp_path: Path) -> None:
    """En barnbarnprocess som håller stdout-pipen öppen får aldrig hänga jobbet."""
    sink = RecordingSink()
    context = OperationContext(
        sink=sink,
        cancel_requested=lambda: False,
        pipe_drain_seconds=0.2,
    )

    # bash avslutas direkt medan bakgrundssleepen ärver stdout-pipen.
    with pytest.raises(OperationFailed, match="output-pipen"):
        context.run_process(["bash", "-c", "sleep 30 &"], cwd=tmp_path)


def test_terminal_sink_writes_progress_message_to_err(capsys) -> None:
    sink = TerminalSink()
    sink.write_progress(ProgressUpdate(step="OCR", completed=3, total=10, message="Tre klara"))
    sink.write_log("loggrad", "info")

    captured = capsys.readouterr()
    assert "OCR: Tre klara" in captured.err
    assert "loggrad" in captured.out


def test_redact_env_masks_secret_like_keys() -> None:
    redacted = _redact_env(
        {"ANTHROPIC_API_KEY": "hemlig", "OPENAI_API_KEY": "hemlig", "HOME": "/tmp"}
    )

    assert redacted["ANTHROPIC_API_KEY"] == "***"
    assert redacted["OPENAI_API_KEY"] == "***"
    assert redacted["HOME"] == "/tmp"


def test_redact_env_masks_neo4j_auth() -> None:
    redacted = _redact_env({"NEO4J_AUTH": "neo4j/hemligt-lösenord", "HOME": "/tmp"})

    assert redacted["NEO4J_AUTH"] == "***"
    assert redacted["HOME"] == "/tmp"


def test_redact_argv_masks_value_after_secret_flag() -> None:
    redacted = _redact_argv(["--jobs", "4", "--api-key", "hemlig-token", "sista"])

    assert redacted == ["--jobs", "4", "--api-key", "***", "sista"]


def test_redact_argv_masks_neo4j_auth_token() -> None:
    """Regression: NEO4J_AUTH=<lösenord> får aldrig loggas i kommandoraden."""
    redacted = _redact_argv(
        ["-e", "NEO4J_AUTH=neo4j/hemligt-lösenord", "NEO4J_server_memory_heap_max__size=2G"]
    )

    assert redacted == ["-e", "***", "NEO4J_server_memory_heap_max__size=2G"]
