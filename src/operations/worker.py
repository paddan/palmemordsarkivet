"""Fristående bakgrundsworker och heartbeat.

Workern startas av ``job_service.start_job`` som en fristående process och kör
en registrerad operation med en jobb-context. Den överlever att Streamlit eller
webbläsaren stängs och skriver ett terminalt tillstånd i ``finally``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import db

from .context import OperationContext, ProgressSink
from .exceptions import OperationCancelled, ensure_successful_result
from .job_service import default_db_path, deserialize_params
from .models import ProgressUpdate
from .registry import OperationRegistry, get_registry


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class JobSink(ProgressSink):
    """ProgressSink som skriver progress till SQLite och logg till jobbloggen."""

    def __init__(self, *, conn, job_id: str, log_file: TextIO) -> None:
        self._conn = conn
        self._job_id = job_id
        self._log_file = log_file

    def write_log(self, message: str, level: str = "info") -> None:
        self._log_file.write(f"{_now()} [{level}] {message}\n")
        self._log_file.flush()

    def write_progress(self, update: ProgressUpdate) -> None:
        db.update_admin_job_progress(
            self._conn,
            self._job_id,
            step=update.step or None,
            completed=update.completed,
            total=update.total,
            message=update.message or None,
        )

    def write_traceback(self, exc: BaseException) -> None:
        self._log_file.write(f"{_now()} [error] Traceback:\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=self._log_file)
        self._log_file.flush()


def _heartbeat_loop(
    db_path: Path,
    job_id: str,
    cancel_event: threading.Event,
    interval: float,
    stop_event: threading.Event,
) -> None:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        while not stop_event.is_set():
            stop_event.wait(interval)
            if stop_event.is_set():
                break
            try:
                db.heartbeat_admin_job(conn, job_id)
                row = db.get_admin_job(conn, job_id)
                if row is not None and row["status"] == "cancel_requested":
                    cancel_event.set()
            except Exception:  # noqa: BLE001 - heartbeaten får aldrig krascha workern
                pass
    finally:
        conn.close()


def _install_cancel_handler(cancel_event: threading.Event):
    """Installera en SIGTERM-hanterare som sätter cancel-eventet."""

    def handler(signum: int, frame: object) -> None:
        cancel_event.set()

    return signal.signal(signal.SIGTERM, handler)


def _finish_cancelled_if_possible(conn, job_id: str) -> None:
    """Markera jobbet avbrutet om en cancel hann emellan två skrivningar.

    Används som fallback när ett terminalt skriv anrop misslyckats med
    ``InvalidAdminJobTransition`` för att en parallell cancel ändrade statusen.
    """
    # Redan terminalt — någon annan hann före.
    with contextlib.suppress(db.InvalidAdminJobTransition):
        db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)


def run_job(
    job_id: str,
    *,
    db_path: Path,
    registry: OperationRegistry | None = None,
    heartbeat_interval: float = 5.0,
) -> dict | None:
    """Kör ett köat jobb till ett terminalt tillstånd.

    Returnerar jobbraden som dict efter avslut, eller None om jobbet saknas.
    """
    registry = registry or get_registry()
    conn = db.connect(db_path)
    try:
        try:
            # Startup/claim är skyddat: kraschar workern här får jobbet aldrig
            # ligga kvar som ett pid-löst queued-jobb som blockerar kön.
            db.init_schema(conn)
            row = db.get_admin_job(conn, job_id)
            if row is None:
                return None

            if not db.claim_admin_job(conn, job_id, pid=os.getpid()):
                current = db.get_admin_job(conn, job_id)
                if current is not None and current["status"] == "cancel_requested":
                    with contextlib.suppress(db.InvalidAdminJobTransition):
                        db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)
                updated = db.get_admin_job(conn, job_id)
                return dict(updated) if updated else None
        except BaseException as exc:  # noqa: BLE001 - allt ska fångas och loggas
            # Redan terminalt.
            with contextlib.suppress(db.InvalidAdminJobTransition):
                db.finish_admin_job(
                    conn,
                    job_id,
                    status="failed",
                    exit_code=1,
                    error=f"Kunde inte starta jobbet: {exc}",
                )
            raise

        try:
            definition = registry.get(row["operation"])
            params = deserialize_params(definition, row["params_json"])

            log_path = Path(row["log_path"])
            log_path.parent.mkdir(parents=True, exist_ok=True)

            cancel_event = threading.Event()
            stop_heartbeat = threading.Event()
            heartbeat_thread = threading.Thread(
                target=_heartbeat_loop,
                args=(db_path, job_id, cancel_event, heartbeat_interval, stop_heartbeat),
                daemon=True,
            )
            heartbeat_thread.start()

            previous_handler = _install_cancel_handler(cancel_event)

            try:
                with log_path.open("a", encoding="utf-8") as log_file:
                    sink = JobSink(conn=conn, job_id=job_id, log_file=log_file)
                    context = OperationContext(
                        sink=sink,
                        cancel_requested=cancel_event.is_set,
                    )
                    try:
                        result = definition.run(context, params)
                        ensure_successful_result(result, definition.label)
                    except OperationCancelled:
                        try:
                            db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)
                        except db.InvalidAdminJobTransition:
                            _finish_cancelled_if_possible(conn, job_id)
                    except BaseException as exc:  # noqa: BLE001 - allt ska fångas och loggas
                        sink.write_traceback(exc)
                        try:
                            db.finish_admin_job(
                                conn, job_id, status="failed", exit_code=1, error=str(exc)
                            )
                        except db.InvalidAdminJobTransition:
                            _finish_cancelled_if_possible(conn, job_id)
                    else:
                        current = db.get_admin_job(conn, job_id)
                        if current is not None and current["status"] == "cancel_requested":
                            try:
                                db.finish_admin_job(conn, job_id, status="cancelled", exit_code=130)
                            except db.InvalidAdminJobTransition:
                                _finish_cancelled_if_possible(conn, job_id)
                        else:
                            try:
                                db.finish_admin_job(conn, job_id, status="succeeded", exit_code=0)
                            except db.InvalidAdminJobTransition:
                                # TOCTOU: en cancel hann mellan statusläsningen och
                                # slutförandet — markera jobbet avbrutet i stället.
                                _finish_cancelled_if_possible(conn, job_id)
            finally:
                stop_heartbeat.set()
                heartbeat_thread.join(timeout=2.0)
                signal.signal(signal.SIGTERM, previous_handler)
        except BaseException as exc:  # noqa: BLE001 - claimed jobb får aldrig fastna
            with contextlib.suppress(db.InvalidAdminJobTransition):
                db.finish_admin_job(
                    conn,
                    job_id,
                    status="failed",
                    exit_code=1,
                    error=f"Kunde inte starta jobbet: {exc}",
                )

        updated = db.get_admin_job(conn, job_id)
        return dict(updated) if updated else None
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    """Entrypoint för ``python -m operations.worker``."""
    parser = argparse.ArgumentParser(description="Bakgrundsworker för admin-jobb")
    parser.add_argument("--job-id", required=True, help="Jobb-id att köra")
    args = parser.parse_args(argv)

    db_path = Path(os.environ.get("STATE_DB") or default_db_path())
    result = run_job(args.job_id, db_path=db_path)
    return 0 if result is not None else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
