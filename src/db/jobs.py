"""Adminjobb (admin_jobs): skapa, starta, uppdatera, avbryta och avsluta
bakgrundsjobb."""

from __future__ import annotations

import sqlite3

from ._schema import (
    ActiveAdminJobError,
    DuplicateAdminJobError,
    InvalidAdminJobTransition,
    now,
)

# --- admin_jobs ---------------------------------------------------------

_ACTIVE_ADMIN_JOB_STATUSES = ("queued", "running", "cancel_requested")
_TERMINAL_ADMIN_JOB_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


def _admin_job_active_slot_value(status: str) -> int | None:
    """Returnera active_slot-värdet (1 aktivt, None terminalt) för en status."""
    return 1 if status in _ACTIVE_ADMIN_JOB_STATUSES else None


def create_admin_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    operation: str,
    params_json: str,
    log_path: str,
) -> sqlite3.Row:
    """Skapa en köad jobbrad med active_slot=1.

    Kastar ``ActiveAdminJobError`` om ett annat jobb redan äger den aktiva
    platsen — den partiella unika indexeringen gör regeln atomisk.
    """
    try:
        conn.execute(
            """
            INSERT INTO admin_jobs(
                id, operation, params_json, status, active_slot,
                created_at, completed_units, log_path
            )
            VALUES (?, ?, ?, 'queued', 1, ?, 0, ?)
            """,
            (job_id, operation, params_json, now(), log_path),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # Särskilj PK-kollision (dubblett-jobb-id) från kollision på den
        # partiella unika indexeringen av active_slot — bara det senare
        # betyder att ett annat jobb är aktivt.
        if get_admin_job(conn, job_id) is not None:
            raise DuplicateAdminJobError(job_id) from exc
        active = get_active_admin_job(conn)
        if active is not None:
            raise ActiveAdminJobError(active["id"]) from exc
        raise
    return get_admin_job(conn, job_id)  # type: ignore[return-value]


def claim_admin_job(conn: sqlite3.Connection, job_id: str, *, pid: int) -> bool:
    """Övergå queued → running och spara PID. Returnerar False vid misslyckad claim."""
    cur = conn.execute(
        """
        UPDATE admin_jobs
           SET status='running', pid=?, started_at=?, heartbeat_at=?
         WHERE id=? AND status='queued'
        """,
        (pid, now(), now(), job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def get_admin_job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    """Hämta jobbraden för job_id, eller None om den saknas."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM admin_jobs WHERE id=?", (job_id,)
    ).fetchone()
    return row


def get_active_admin_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Returnera det aktiva jobbet (active_slot=1), eller None."""
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM admin_jobs WHERE active_slot=1"
    ).fetchone()
    return row


def list_admin_jobs(
    conn: sqlite3.Connection, *, limit: int | None = None
) -> list[sqlite3.Row]:
    """Returnera jobb nyast först, eventuellt begränsat till ``limit``."""
    query = "SELECT * FROM admin_jobs ORDER BY created_at DESC, id DESC"
    if limit is not None:
        query += f" LIMIT {int(limit)}"
    return list(conn.execute(query))


def update_admin_job_progress(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    step: str | None,
    completed: int | None = None,
    total: int | None = None,
    message: str | None = None,
) -> None:
    """Uppdatera steg, enheter och senaste meddelande för ett jobb."""
    cur = conn.execute(
        """
        UPDATE admin_jobs
           SET current_step=COALESCE(?, current_step),
               completed_units=COALESCE(?, completed_units),
               total_units=COALESCE(?, total_units),
               message=COALESCE(?, message)
         WHERE id=?
        """,
        (step, completed, total, message, job_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt jobb-id: {job_id}")
    conn.commit()


def heartbeat_admin_job(conn: sqlite3.Connection, job_id: str) -> None:
    """Uppdatera jobbets heartbeat-tidpunkt."""
    cur = conn.execute(
        "UPDATE admin_jobs SET heartbeat_at=? WHERE id=?", (now(), job_id)
    )
    if cur.rowcount == 0:
        raise KeyError(f"okänt jobb-id: {job_id}")
    conn.commit()


def request_admin_job_cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    """Begär kontrollerad avbrytning av ett aktivt jobb.

    Övergår queued/running → cancel_requested. Returnerar True om jobbet nu är
    i cancel_requested, False om jobbet saknas eller redan är terminalt.
    """
    conn.execute(
        """
        UPDATE admin_jobs
           SET status='cancel_requested', cancel_requested_at=?
         WHERE id=? AND status IN ('queued', 'running')
        """,
        (now(), job_id),
    )
    conn.commit()
    row = get_admin_job(conn, job_id)
    return bool(row and row["status"] == "cancel_requested")


def _finish_admin_job_status_where(status: str) -> str:
    """Returnera villkoret för de nuvarande statusar som får övergå till ``status``."""
    if status == "succeeded":
        return "status='running'"
    if status == "failed":
        return "status IN ('queued', 'running')"
    if status in ("cancelled", "interrupted"):
        return "status IN ('queued', 'running', 'cancel_requested')"
    raise ValueError(f"Okänd terminal jobbstatus: {status!r}")


def finish_admin_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str,
    exit_code: int | None,
    error: str | None = None,
) -> None:
    """Övergå ett jobb till terminal status och frigör active_slot atomiskt."""
    if status not in _TERMINAL_ADMIN_JOB_STATUSES:
        raise ValueError(f"Inte en terminal jobbstatus: {status!r}")

    cur = conn.execute(
        f"""
        UPDATE admin_jobs
           SET status=?, active_slot=NULL, finished_at=?, exit_code=?, error=?
         WHERE id=? AND {_finish_admin_job_status_where(status)}
        """,
        (status, now(), exit_code, error, job_id),
    )
    if cur.rowcount == 0:
        raise InvalidAdminJobTransition(
            f"Jobbet {job_id} kan inte övergå till {status!r} från sitt nuvarande tillstånd"
        )
    conn.commit()


def mark_admin_job_interrupted(conn: sqlite3.Connection, job_id: str) -> None:
    """Markera ett aktivt jobb som avbrutet av en omstart/krasch."""
    finish_admin_job(conn, job_id, status="interrupted", exit_code=None)


def delete_admin_job(conn: sqlite3.Connection, job_id: str) -> bool:
    """Ta bort ett terminalt jobb ur historiken. Returnerar True vid borttagning.

    Aktiva jobb (active_slot=1) skyddas från borttagning.
    """
    cur = conn.execute(
        "DELETE FROM admin_jobs WHERE id=? AND active_slot IS NULL", (job_id,)
    )
    conn.commit()
    return cur.rowcount == 1
