"""Tester för versionsstyrd jobbmodell (admin_jobs) i src/db.py."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import db
from db import (
    SCHEMA_VERSION,
    ActiveAdminJobError,
    InvalidAdminJobTransition,
    claim_admin_job,
    connect,
    create_admin_job,
    finish_admin_job,
    get_active_admin_job,
    get_admin_job,
    heartbeat_admin_job,
    init_schema,
    list_admin_jobs,
    mark_admin_job_interrupted,
    request_admin_job_cancel,
    update_admin_job_progress,
)


def _fresh(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(tmp_path / "state.db")
    init_schema(conn)
    return conn


def _create(conn: sqlite3.Connection, *, job_id: str = "job-1", operation: str = "ingest") -> sqlite3.Row:
    return create_admin_job(
        conn,
        job_id=job_id,
        operation=operation,
        params_json='{"jobs": 4}',
        log_path=f"generated/admin_jobs/{job_id}.log",
    )


def test_init_schema_creates_admin_jobs_table(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "admin_jobs" in tables
    finally:
        conn.close()


def test_init_schema_migrates_v6_fixture_preserving_data(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy_v6.db"
    fixture = Path(__file__).parent / "fixtures" / "state_db_v6.sql"
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(fixture.read_text(encoding="utf-8"))
        raw.commit()
    finally:
        raw.close()

    conn = connect(db_path)
    try:
        init_schema(conn)

        assert db.schema_version(conn) == SCHEMA_VERSION
        migrated = db.get_pdf_file(conn, "v6-legacy")
        assert migrated is not None
        assert migrated["tesseract_failed"] == 1
        assert migrated["tesseract_blacklisted_at"] == "2026-07-18T00:10:00+00:00"

        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "admin_jobs" in tables
    finally:
        conn.close()


def test_create_admin_job_sets_queued_and_claims_slot(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        job = _create(conn)
        assert job["status"] == "queued"
        assert job["active_slot"] == 1
        assert job["operation"] == "ingest"
        assert job["completed_units"] == 0
        assert job["log_path"] == "generated/admin_jobs/job-1.log"
    finally:
        conn.close()


def test_only_one_active_job_is_allowed(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn, job_id="job-1")
        with pytest.raises(ActiveAdminJobError, match="job-1"):
            _create(conn, job_id="job-2")
    finally:
        conn.close()


def test_claim_transitions_queued_to_running(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        assert claim_admin_job(conn, "job-1", pid=1234) is True
        row = get_admin_job(conn, "job-1")
        assert row["status"] == "running"
        assert row["pid"] == 1234
        assert row["started_at"] is not None
    finally:
        conn.close()


def test_claim_fails_when_job_is_not_queued(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        claim_admin_job(conn, "job-1", pid=1)
        assert claim_admin_job(conn, "job-1", pid=2) is False
    finally:
        conn.close()


def test_progress_heartbeat_and_finish_lifecycle(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        claim_admin_job(conn, "job-1", pid=1234)
        update_admin_job_progress(
            conn, "job-1", step="OCR", completed=3, total=10, message="Tre klara"
        )
        heartbeat_admin_job(conn, "job-1")

        row = get_admin_job(conn, "job-1")
        assert row["current_step"] == "OCR"
        assert row["completed_units"] == 3
        assert row["total_units"] == 10
        assert row["message"] == "Tre klara"
        assert row["heartbeat_at"] is not None

        finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
        row = get_admin_job(conn, "job-1")
        assert row["status"] == "succeeded"
        assert row["active_slot"] is None
        assert row["finished_at"] is not None
        assert row["exit_code"] == 0
    finally:
        conn.close()


def test_finish_releases_slot_so_a_new_job_can_start(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn, job_id="job-1")
        claim_admin_job(conn, "job-1", pid=1)
        finish_admin_job(conn, "job-1", status="failed", exit_code=1, error="boom")

        _create(conn, job_id="job-2")
        assert get_active_admin_job(conn)["id"] == "job-2"
    finally:
        conn.close()


def test_invalid_finish_transition_is_rejected(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        # Ett jobb som aldrig claimats får inte slutföras som lyckat.
        with pytest.raises(InvalidAdminJobTransition):
            finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
    finally:
        conn.close()


def test_succeeded_never_after_cancel_requested(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        claim_admin_job(conn, "job-1", pid=1)
        assert request_admin_job_cancel(conn, "job-1") is True
        assert get_admin_job(conn, "job-1")["status"] == "cancel_requested"

        with pytest.raises(InvalidAdminJobTransition):
            finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)

        finish_admin_job(conn, "job-1", status="cancelled", exit_code=130)
        assert get_admin_job(conn, "job-1")["status"] == "cancelled"
    finally:
        conn.close()


def test_cancel_request_is_idempotent_and_rejected_terminally(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        claim_admin_job(conn, "job-1", pid=1)
        assert request_admin_job_cancel(conn, "job-1") is True
        assert request_admin_job_cancel(conn, "job-1") is True
        assert get_admin_job(conn, "job-1")["cancel_requested_at"] is not None

        finish_admin_job(conn, "job-1", status="cancelled", exit_code=130)
        assert request_admin_job_cancel(conn, "job-1") is False
    finally:
        conn.close()


def test_mark_interrupted_from_running(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn)
        claim_admin_job(conn, "job-1", pid=1)
        mark_admin_job_interrupted(conn, "job-1")
        row = get_admin_job(conn, "job-1")
        assert row["status"] == "interrupted"
        assert row["active_slot"] is None
    finally:
        conn.close()


def test_delete_admin_job_only_terminal(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        _create(conn, job_id="job-1")
        claim_admin_job(conn, "job-1", pid=1)
        # Aktivt jobb skyddas.
        assert db.delete_admin_job(conn, "job-1") is False

        finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
        assert db.delete_admin_job(conn, "job-1") is True
        assert db.get_admin_job(conn, "job-1") is None
    finally:
        conn.close()


def test_list_admin_jobs_is_newest_first(tmp_path: Path) -> None:
    conn = _fresh(tmp_path)
    try:
        first = _create(conn, job_id="job-1", operation="download")
        claim_admin_job(conn, "job-1", pid=1)
        finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
        _create(conn, job_id="job-2", operation="ingest")

        jobs = list_admin_jobs(conn)
        assert [job["id"] for job in jobs] == ["job-2", "job-1"]
        assert first["id"] == "job-1"
    finally:
        conn.close()
