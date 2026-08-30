"""Skapa, starta, inspektera och stoppa bakgrundsjobb.

Håller sig till ``src/db.py`` för all SQL och till ``operations.models`` för
parameterdefinitioner. Både CLI:n och adminsidan använder dessa funktioner, så
beteendet är identiskt oavsett gränssnitt.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import db

from .exceptions import OperationFailed
from .models import OperationDefinition, ParameterDefinition
from .registry import OperationRegistry, get_registry

SRC = Path(__file__).resolve().parents[1]
ROOT = SRC.parent

HEARTBEAT_STALE_SECONDS = 15.0
# Ett queued-jobb utan PID som är äldre än så här avbryts av reconcile —
# workern kraschade mellan radskapning och claim.
QUEUED_STALE_SECONDS = 60.0
# Hur länge cancel_job väntar på att workern ska avsluta kontrollerat innan
# processgruppen dödas med SIGKILL.
SIGKILL_ESCALATION_GRACE_SECONDS = 10.0

_TERMINAL_JOB_STATUSES = {"succeeded", "failed", "cancelled", "interrupted"}


def default_db_path() -> Path:
    """Returnera sökvägen till state.db (STATE_DB eller projektets default)."""
    return Path(os.environ.get("STATE_DB") or db.DEFAULT_DB)


def default_log_root() -> Path:
    """Returnera katalogen för jobbloggar."""
    return Path(os.environ.get("ADMIN_JOB_LOG_ROOT") or (ROOT / "generated" / "admin_jobs"))


def _row_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _missing_required_value(parameter: ParameterDefinition, value: object) -> bool:
    """Returnera True när en obligatorisk parameter saknar ett användbart värde.

    Ett tomt fält normaliseras annars till ``Path("")`` (cwd) — det måste
    stoppas innan jobbet skapas.
    """
    if not parameter.required:
        return False
    if value is None:
        return True
    return str(value).strip() in ("", ".")


def serialize_params(definition: OperationDefinition, params: Mapping[str, object]) -> str:
    """Normalisera och serialisera jobbparametrar som JSON.

    Hemliga parametrar avvisas om de har ett icke-tomt värde och skrivs aldrig
    till JSON:en — de ska komma från processmiljön i workern.
    """
    normalized: dict[str, object] = {}
    for parameter in definition.parameters:
        value = params.get(parameter.name, parameter.default)
        if parameter.secret:
            parameter.validate_background_value(value)
            continue
        if _missing_required_value(parameter, value):
            raise ValueError(f"Obligatorisk parameter saknas eller är tom: {parameter.name}")
        normalized[parameter.name] = parameter.validate_background_value(value)
    return json.dumps(normalized, default=str, ensure_ascii=False)


def deserialize_params(definition: OperationDefinition, params_json: str) -> dict[str, object]:
    """Läs tillbaka parametrar och återskapa deras typade värden (t.ex. Path)."""
    raw = json.loads(params_json)
    by_name = {parameter.name: parameter for parameter in definition.parameters}
    return {
        name: by_name[name].normalize_value(value) if name in by_name else value
        for name, value in raw.items()
    }


def start_job(
    operation_id: str,
    params: Mapping[str, object],
    *,
    registry: OperationRegistry | None = None,
    db_path: Path | None = None,
    log_root: Path | None = None,
) -> dict:
    """Skapa jobbraden och starta en fristående worker.

    Returnerar en dict med ``id``, ``operation`` och ``log_path``. Vid fel på
    workerstarten markeras jobbraden ``failed`` och ``OperationFailed`` kastas.
    """
    registry = registry or get_registry()
    definition = registry.get(operation_id)
    db_path = db_path or default_db_path()
    log_root = log_root or default_log_root()
    log_root.mkdir(parents=True, exist_ok=True)

    job_id = uuid.uuid4().hex
    log_path = log_root / f"{job_id}.log"
    params_json = serialize_params(definition, params)
    # Skapa filen innan jobbet blir synligt så `jobs log --follow` alltid kan
    # ansluta direkt, även innan workern hunnit öppna loggen för append.
    log_path.touch()

    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        db.create_admin_job(
            conn,
            job_id=job_id,
            operation=operation_id,
            params_json=params_json,
            log_path=str(log_path),
        )
    finally:
        conn.close()

    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    env["ADMIN_JOB_LOG_ROOT"] = str(log_root)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")

    try:
        subprocess.Popen(
            [sys.executable, "-m", "operations.worker", "--job-id", job_id],
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        conn = db.connect(db_path)
        db.init_schema(conn)
        try:
            db.finish_admin_job(
                conn,
                job_id,
                status="failed",
                exit_code=1,
                error=f"Kunde inte starta worker: {exc}",
            )
        finally:
            conn.close()
        raise OperationFailed(f"Kunde inte starta worker: {exc}") from exc

    return {"id": job_id, "operation": operation_id, "log_path": str(log_path)}


def _heartbeat_fresh(heartbeat_at: str | None, stale_seconds: float) -> bool:
    if not heartbeat_at:
        return False
    try:
        heartbeat = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return False
    age = (datetime.now(UTC) - heartbeat.astimezone(UTC)).total_seconds()
    return age < stale_seconds


def _timestamp_age_seconds(timestamp: str | None) -> float | None:
    """Returnera åldern i sekunder för ett ISO-timestamp, eller None om okänt."""
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return (datetime.now(UTC) - ts.astimezone(UTC)).total_seconds()


def _process_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reconcile_active_job(
    *,
    db_path: Path | None = None,
    stale_seconds: float = HEARTBEAT_STALE_SECONDS,
    queued_stale_seconds: float = QUEUED_STALE_SECONDS,
) -> dict | None:
    """Återställ tillståndet för ett aktivt jobb.

    Ett jobb med färsk heartbeat lämnas orört. Ett jobb med gammal heartbeat
    markeras ``interrupted`` endast om processen verifierats saknas — signalering
    sker aldrig enbart utifrån ett sparat PID. Ett pid-löst queued-jobb som är
    äldre än ``queued_stale_seconds`` antas vara kvarlämnat av en kraschad
    worker och markeras ``interrupted`` så att kön inte blockeras.
    """
    db_path = db_path or default_db_path()
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        job = db.get_active_admin_job(conn)
        if job is None:
            return None
        if job["status"] == "queued":
            age = _timestamp_age_seconds(job["created_at"])
            if (
                job["pid"] is None
                and age is not None
                and age >= queued_stale_seconds
            ):
                # Parallell körning kan ha hunnit före — behandla som åtgärdat.
                with contextlib.suppress(db.InvalidAdminJobTransition):
                    db.mark_admin_job_interrupted(conn, job["id"])
                updated = db.get_admin_job(conn, job["id"])
                return _row_dict(updated) if updated else None
            return _row_dict(job)
        if _heartbeat_fresh(job["heartbeat_at"], stale_seconds):
            return _row_dict(job)
        if _process_exists(job["pid"]):
            return _row_dict(job)
        # Parallell körning kan ha hunnit före — behandla som åtgärdat.
        with contextlib.suppress(db.InvalidAdminJobTransition):
            db.mark_admin_job_interrupted(conn, job["id"])
        updated = db.get_admin_job(conn, job["id"])
        return _row_dict(updated) if updated else None
    finally:
        conn.close()


def cancel_job(
    *,
    job_id: str | None = None,
    db_path: Path | None = None,
    stale_seconds: float = HEARTBEAT_STALE_SECONDS,
    kill_grace_seconds: float = SIGKILL_ESCALATION_GRACE_SECONDS,
) -> bool:
    """Begär kontrollerad avbrytning av ett aktivt jobb.

    Returnerar False om jobbet saknas eller redan är terminalt. SIGTERM skickas
    endast när heartbeaten är färsk, för att undvika att träffa ett återanvänt
    PID. Om jobbet inte når ett terminalt tillstånd inom ``kill_grace_seconds``
    eskalerar vi till SIGKILL mot processgruppen — workern startas med
    ``start_new_session``, så dess PID är även processgruppens ID.
    """
    db_path = db_path or default_db_path()
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        job = db.get_admin_job(conn, job_id) if job_id else db.get_active_admin_job(conn)
        if job is None:
            return False
        if not db.request_admin_job_cancel(conn, job["id"]):
            return False
        pid = job["pid"]
        if pid is None:
            # Jobbet claimades aldrig — det avslutas av en worker som ser
            # cancel_requested, eller av reconcile som avbryter det i efterhand.
            return True
        if not _heartbeat_fresh(job["heartbeat_at"], stale_seconds):
            # Gammal heartbeat: risk att PID:et återanvänts, signalera aldrig.
            return True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

        # Eskalering: vänta på terminalt tillstånd, annars SIGKILL till gruppen.
        deadline = time.monotonic() + kill_grace_seconds
        while True:
            current = db.get_admin_job(conn, job["id"])
            if (
                current is None
                or current["status"] in _TERMINAL_JOB_STATUSES
                or current["pid"] != pid
            ):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.2, remaining))
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)
        return True
    finally:
        conn.close()


def read_log_tail(log_path: Path, *, lines: int = 50) -> str:
    """Returnera de sista ``lines`` raderna ur en jobblogg."""
    try:
        with log_path.open(encoding="utf-8") as handle:
            all_lines = handle.readlines()
    except FileNotFoundError:
        return ""
    return "".join(all_lines[-lines:])


def follow_log(
    log_path: Path,
    *,
    stop: Callable[[], bool],
    poll_interval: float = 0.5,
) -> Iterator[str]:
    """Yield nya loggrader tills ``stop()`` returnerar True.

    Vid start hoppar vi till filens nuvarande slut; endast nytillkomna rader
    yieldas. När ``stop`` är sant töms återstoden innan generatorn avslutas.
    """
    with log_path.open(encoding="utf-8") as handle:
        handle.seek(0, 2)
        while True:
            yield from handle
            if stop():
                return
            time.sleep(poll_interval)
