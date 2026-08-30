"""Progress-sinkar, loggning, subprocesser och kontrollerad avbrytning.

Laget är Streamlit-fritt: domän- och orkestreringskod rapporterar progress och
logg genom ``OperationContext`` och vet ingenting om var statusen presenteras.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TextIO

from .exceptions import OperationCancelled, OperationFailed
from .models import ProgressUpdate

_SECRET_ENV_SUFFIXES = ("_KEY", "_TOKEN", "_PASSWORD", "_AUTH")
_SECRET_FLAG_HINTS = ("key", "token", "password", "secret")


def _redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Maskera miljövärden vars nyckel ser ut att innehålla en hemlighet."""
    return {
        key: ("***" if key.upper().endswith(_SECRET_ENV_SUFFIXES) else value)
        for key, value in env.items()
    }


def _token_contains_secret(token: str) -> bool:
    """Returnera True när ett NYCKEL=VÄRDE-token bär ett hemligt värde.

    Fångar t.ex. ``-e NEO4J_AUTH=neo4j/<lösenord>`` där värdet ligger inbakat i
    samma token som nyckeln (säkerhet i djupet utöver flagg-maskeringen).
    """
    if "=" not in token:
        return False
    key = token.split("=", 1)[0].upper()
    return key.endswith(_SECRET_ENV_SUFFIXES) or any(
        hint in key.lower() for hint in _SECRET_FLAG_HINTS
    )


def _redact_argv(argv: Sequence[str]) -> list[str]:
    """Maskera argumentvärden som följer på hemlighetslika flaggor.

    Maskerar även tokens som själva bär hemligheten inbakat, t.ex.
    ``NEO4J_AUTH=neo4j/<lösenord>``.
    """
    result: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            result.append("***")
            redact_next = False
            continue
        if _token_contains_secret(token):
            result.append("***")
            continue
        result.append(token)
        lowered = token.lstrip("-").lower()
        redact_next = any(hint in lowered for hint in _SECRET_FLAG_HINTS)
    return result


class ProgressSink:
    """Abstrakt mottagare för logg- och progresshändelser."""

    def write_log(self, message: str, level: str = "info") -> None:
        """Skriv en loggrad till målet (t.ex. terminal eller jobblogg)."""
        raise NotImplementedError

    def write_progress(self, update: ProgressUpdate) -> None:
        """Skriv en strukturerad lägesuppdatering."""
        raise NotImplementedError

    def write_traceback(self, exc: BaseException) -> None:
        """Skriv ett fullständigt traceback till målet."""
        raise NotImplementedError


class TerminalSink(ProgressSink):
    """Skriver logg och progress till givna textströmmar (CLI-förgrundskörning)."""

    def __init__(
        self,
        *,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        # Läs sys.stdout/sys.stderr vid anropstillfället så att tester som
        # ersätter strömmarna (t.ex. capsys) fångar utskrifterna.
        self._out = out if out is not None else sys.stdout
        self._err = err if err is not None else sys.stderr

    def write_log(self, message: str, level: str = "info") -> None:
        stream = self._out if level in ("info", "success") else self._err
        print(message, file=stream)

    def write_progress(self, update: ProgressUpdate) -> None:
        if not update.message:
            return
        prefix = f"{update.step}: " if update.step else ""
        print(f"{prefix}{update.message}", file=self._err)

    def write_traceback(self, exc: BaseException) -> None:
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=self._err)


def ensure_terminal_context(context: OperationContext | None) -> OperationContext:
    """Returnera ``context`` eller en ny terminal-context för förgrundskörning."""
    if context is not None:
        return context
    return OperationContext(sink=TerminalSink(), cancel_requested=lambda: False)


class OperationContext:
    """Rapporterar progress/logg och kör externa processer med avbrytning.

    ``cancel_requested`` är en nollställbar funktion som returnerar True när
    användaren eller workern har begärt avbrott. ``run_process`` startar externa
    processer i en egen processgrupp så att hela trädet kan termineras
    kontrollerat.
    """

    def __init__(
        self,
        *,
        sink: ProgressSink,
        cancel_requested: Callable[[], bool],
        terminate_grace_seconds: float = 5.0,
        pipe_drain_seconds: float = 10.0,
    ) -> None:
        self._sink = sink
        self._cancel_requested = cancel_requested
        self._terminate_grace_seconds = terminate_grace_seconds
        self._pipe_drain_seconds = pipe_drain_seconds
        self._current_step = ""

    def step(self, name: str, *, completed: int = 0, total: int | None = None) -> None:
        """Påbörja ett nytt steg och rapportera dess initiala progress."""
        self._current_step = name
        self._sink.write_progress(
            ProgressUpdate(step=name, completed=completed, total=total, message="")
        )

    def progress(self, completed: int, total: int | None, message: str = "") -> None:
        """Rapportera framsteg inom det aktuella steget."""
        self._sink.write_progress(
            ProgressUpdate(
                step=self._current_step,
                completed=completed,
                total=total,
                message=message,
            )
        )

    def log(self, message: str, *, level: str = "info") -> None:
        """Skriv en loggrad."""
        self._sink.write_log(message, level)

    def check_cancelled(self) -> None:
        """Kasta ``OperationCancelled`` om avbrott har begärts."""
        if self._cancel_requested():
            raise OperationCancelled("Operationen avbröts")

    def run_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> int:
        """Kör en extern process (ingen shell) och strömma dess output till loggen.

        Stdout slås samman med stderr. Vid avbrott (cancel eller Ctrl-C)
        termineras hela processgruppen med SIGTERM följt av SIGKILL efter en
        grace-period.
        """
        self.check_cancelled()

        full_env = os.environ.copy()
        if env:
            full_env.update(env)

        self._sink.write_log(
            f"$ {' '.join(_redact_argv(list(argv)))} (cwd={cwd})",
            "debug",
        )

        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # Fixera kodningen i stället för locale (text=True): felkodade
            # byte-strömmar får aldrig döda reader-tråden.
            encoding="utf-8",
            errors="replace",
            text=True,
            bufsize=1,
            start_new_session=True,
        )

        def _read_output() -> None:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\n")
                # Loggsinken får inte döda reader-tråden: vi måste fortsätta
                # tömma pipen så att processen inte blockeras av en full buffer.
                with contextlib.suppress(Exception):
                    self._sink.write_log(line, "info")

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        try:
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                self.check_cancelled()
                time.sleep(0.05)
        except OperationCancelled:
            self._terminate_process_group(process)
            raise
        except KeyboardInterrupt:
            # Ctrl-C når bara oss (barnet lever i en egen session) — se till
            # att processgruppen termineras innan avbrottet återkastas.
            self._terminate_process_group(process)
            raise

        # Vänta tills reader-tråden tömts helt (pipe-EOF) så att trailing
        # output inte tappas. Ett kvarvarande barnbarn kan hålla pipen öppen
        # efter att direktbarnet avslutats — en evig join får aldrig hänga jobbet.
        reader.join(timeout=self._pipe_drain_seconds)
        if reader.is_alive():
            self._terminate_process_group(process)
            raise OperationFailed(
                "Underprocessen lämnade kvar en process som håller output-pipen öppen"
            )
        return returncode

    def _terminate_process_group(self, process: subprocess.Popen[str]) -> None:
        """Terminera processgruppen kontrollerat (SIGTERM → grace → SIGKILL)."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        try:
            deadline = time.monotonic() + self._terminate_grace_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    return
                time.sleep(0.05)
        except KeyboardInterrupt:
            # Ett nytt Ctrl-C under grace-perioden hoppar direkt till SIGKILL —
            # processen ska dö oavsett.
            pass

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        process.wait()
