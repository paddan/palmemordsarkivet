"""CLI för att starta, övervaka och avbryta bakgrundsjobb.

Exempel:
    .venv/bin/python scripts/jobs.py start run-pipeline --jobs 4
    .venv/bin/python scripts/jobs.py status
    .venv/bin/python scripts/jobs.py list
    .venv/bin/python scripts/jobs.py log --follow
    .venv/bin/python scripts/jobs.py cancel
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TextIO

# Se till att projektets src/-träd används även när jobs.py importeras som modul.
_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC in sys.path:
    sys.path.remove(_SRC)
sys.path.insert(0, _SRC)

import db  # noqa: E402
from operations.cli import parse_operation_args  # noqa: E402
from operations.exceptions import OperationFailed  # noqa: E402
from operations.job_service import (  # noqa: E402
    cancel_job,
    default_db_path,
    follow_log,
    read_log_tail,
    reconcile_active_job,
    start_job,
)
from operations.registry import get_registry  # noqa: E402


def _format_job_summary(job: dict) -> str:
    lines = [
        f"Jobb:   {job['id']}",
        f"Status: {job['status']}",
        f"Operation: {job['operation']}",
    ]
    if job.get("started_at"):
        lines.append(f"Startad: {job['started_at']}")
    if job.get("current_step"):
        total = f"/{job['total_units']}" if job["total_units"] is not None else ""
        lines.append(f"Steg:   {job['current_step']} ({job['completed_units']}{total})")
    if job.get("message"):
        lines.append(f"Meddelande: {job['message']}")
    if job.get("error"):
        lines.append(f"Fel:    {job['error']}")
    if job.get("log_path"):
        lines.append(f"Logg:   {job['log_path']}")
    return "\n".join(lines)


def _cmd_start(
    operation_id: str,
    flags: list[str],
    *,
    out: TextIO,
    err: TextIO,
    db_path,
    registry,
) -> int:
    try:
        definition = registry.get(operation_id)
    except KeyError as exc:
        print(exc, file=err)
        return 2
    try:
        params = parse_operation_args(definition, flags)
    except SystemExit as exc:
        return int(exc.code)

    try:
        job = start_job(operation_id, params, registry=registry, db_path=db_path)
    except (db.ActiveAdminJobError, OperationFailed) as exc:
        # Driftfel (t.ex. redan aktivt jobb eller workerstart som misslyckades):
        # vänligt felmeddelande i stället för rå traceback.
        print(f"Fel: {exc}", file=err)
        return 1
    except (ValueError, KeyError) as exc:
        print(f"Fel: {exc}", file=err)
        return 2

    print(f"Startat jobb {job['id']} ({job['operation']})", file=out)
    print(f"Logg: {job['log_path']}", file=out)
    return 0


def _cmd_status(job_id: str | None, *, out: TextIO, db_path) -> int:
    job = reconcile_active_job(db_path=db_path)
    if job_id is not None:
        conn = db.connect(db_path)
        db.init_schema(conn)
        try:
            row = db.get_admin_job(conn, job_id)
            job = dict(row) if row is not None else None
        finally:
            conn.close()
    if job is None:
        print("Inget aktivt jobb.", file=out)
        return 0
    print(_format_job_summary(job), file=out)
    return 0


def _cmd_list(*, out: TextIO, db_path) -> int:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        jobs = db.list_admin_jobs(conn, limit=50)
        if not jobs:
            print("Inga jobb.", file=out)
            return 0
        for row in jobs:
            job = dict(row)
            print(f"{job['id']}  {job['status']:<14} {job['operation']}", file=out)
    finally:
        conn.close()
    return 0


def _cmd_log(
    job_id: str | None,
    follow: bool,
    *,
    out: TextIO,
    db_path,
) -> int:
    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        row = db.get_admin_job(conn, job_id) if job_id else db.get_active_admin_job(conn)
        if row is None:
            print("Inget jobb att visa logg för.", file=out)
            return 0
        resolved_job_id = row["id"]
        log_path = Path(row["log_path"])
        if not follow:
            out.write(read_log_tail(log_path, lines=50))
            return 0
    finally:
        conn.close()

    terminal_statuses = {"succeeded", "failed", "cancelled", "interrupted"}

    def _is_done() -> bool:
        conn = db.connect(db_path)
        try:
            row = db.get_admin_job(conn, resolved_job_id)
            return row is not None and row["status"] in terminal_statuses
        finally:
            conn.close()

    for line in follow_log(log_path, stop=_is_done):
        out.write(line)
    return 0


def _cmd_cancel(job_id: str | None, *, out: TextIO, db_path) -> int:
    if cancel_job(job_id=job_id, db_path=db_path):
        print("Avbrytning begärd.", file=out)
        return 0
    print("Inget aktivt jobb att avbryta.", file=out)
    return 0


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    db_path=None,
    registry=None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    db_path = db_path or default_db_path()
    registry = registry or get_registry()

    parser = argparse.ArgumentParser(prog="jobs", description="Hantera bakgrundsjobb")
    subparsers = parser.add_subparsers(dest="command")

    start_parser = subparsers.add_parser("start", help="Starta ett bakgrundsjobb")
    start_parser.add_argument("operation", help="Operation att starta")
    start_parser.add_argument("flags", nargs=argparse.REMAINDER, help="Operationens flaggor")

    status_parser = subparsers.add_parser("status", help="Visa aktivt jobb")
    status_parser.add_argument("--job-id", help="Visa ett specifikt jobb")

    subparsers.add_parser("list", help="Lista jobb")

    log_parser = subparsers.add_parser("log", help="Visa jobblogg")
    log_parser.add_argument("--job-id", help="Visa loggen för ett specifikt jobb")
    log_parser.add_argument("--follow", action="store_true", help="Följ loggen live")

    cancel_parser = subparsers.add_parser("cancel", help="Avbryt aktivt jobb")
    cancel_parser.add_argument("--job-id", help="Avbryt ett specifikt jobb")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    if args.command == "start":
        return _cmd_start(args.operation, args.flags, out=out, err=err, db_path=db_path, registry=registry)
    if args.command == "status":
        return _cmd_status(args.job_id, out=out, db_path=db_path)
    if args.command == "list":
        return _cmd_list(out=out, db_path=db_path)
    if args.command == "log":
        return _cmd_log(args.job_id, args.follow, out=out, db_path=db_path)
    if args.command == "cancel":
        return _cmd_cancel(args.job_id, out=out, db_path=db_path)

    parser.print_help(file=err)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
