"""Tester för jobbservice, worker och jobb-CLI."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db  # noqa: E402
from operations.exceptions import OperationCancelled, OperationFailed  # noqa: E402
from operations.job_service import (  # noqa: E402
    cancel_job,
    deserialize_params,
    follow_log,
    read_log_tail,
    reconcile_active_job,
    serialize_params,
    start_job,
)
from operations.models import OperationDefinition, ParameterDefinition  # noqa: E402
from operations.registry import OperationRegistry  # noqa: E402
from operations.worker import run_job  # noqa: E402
from scripts import jobs as jobs_cli  # noqa: E402


def _definition(operation_id: str = "fake", parameters=()) -> OperationDefinition:
    return OperationDefinition(
        id=operation_id,
        label="Falsk",
        group="Test",
        description="Testoperation",
        parameters=parameters,
        admin_visible=False,
        mutating=True,
        confirmation=None,
        run=lambda context, params: None,
    )


def _registry(run) -> OperationRegistry:
    definition = OperationDefinition(
        id="fake",
        label="Falsk",
        group="Test",
        description="Testoperation",
        parameters=(),
        admin_visible=False,
        mutating=True,
        confirmation=None,
        run=run,
    )
    registry = OperationRegistry()
    registry.register(definition)
    return registry


def _create_job(tmp_path: Path, db_path: Path, *, job_id: str = "job-1") -> None:
    conn = db.connect(db_path)
    db.init_schema(conn)
    db.create_admin_job(
        conn,
        job_id=job_id,
        operation="fake",
        params_json="{}",
        log_path=str(tmp_path / f"{job_id}.log"),
    )
    conn.close()


def test_run_job_runs_fake_runner_and_succeeds(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    events: list[str] = []

    def fake_runner(context, params) -> None:
        context.step("Prov", total=2)
        context.progress(1, 2, "Halvvägs")
        events.append("ran")
        context.progress(2, 2, "Klar")

    result = run_job(
        "job-1", db_path=db_path, registry=_registry(fake_runner), heartbeat_interval=0.01
    )

    assert result is not None
    assert result["status"] == "succeeded"
    assert result["completed_units"] == 2
    assert result["active_slot"] is None
    assert events == ["ran"]


def test_run_job_marks_failed_on_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    def failing_runner(context, params) -> None:
        raise RuntimeError("boom")

    result = run_job("job-1", db_path=db_path, registry=_registry(failing_runner))

    assert result is not None
    assert result["status"] == "failed"
    assert result["error"] == "boom"
    assert result["active_slot"] is None
    log_text = (tmp_path / "job-1.log").read_text(encoding="utf-8")
    assert "RuntimeError" in log_text


def test_run_job_marks_failed_on_nonzero_operation_result(tmp_path: Path) -> None:
    """Domänfunktionens icke-nollkod ska bli failed, inte succeeded."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    result = run_job("job-1", db_path=db_path, registry=_registry(lambda context, params: 1))

    assert result is not None
    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert result["active_slot"] is None
    assert "exitkod 1" in (result["error"] or "")


def test_run_job_marks_cancelled_on_operation_cancelled(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    def cancelling_runner(context, params) -> None:
        raise OperationCancelled("Operationen avbröts")

    result = run_job("job-1", db_path=db_path, registry=_registry(cancelling_runner))

    assert result is not None
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130


def test_run_job_marks_cancelled_when_cancel_requested_during_run(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    def runner_that_completes_anyway(context, params) -> None:
        cancel_conn = db.connect(db_path)
        db.init_schema(cancel_conn)
        db.request_admin_job_cancel(cancel_conn, "job-1")
        cancel_conn.close()
        context.progress(1, 1, "klar ändå")

    result = run_job("job-1", db_path=db_path, registry=_registry(runner_that_completes_anyway))

    assert result is not None
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130


def test_run_job_crash_before_claim_fails_job_and_releases_slot(tmp_path: Path, monkeypatch) -> None:
    """Regression: en worker som kraschar före claim får inte lämna ett
    pid-löst queued-jobb som blockerar jobbsystemet."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    def exploding_claim(conn, job_id, *, pid):
        raise RuntimeError("krasch före claim")

    monkeypatch.setattr("operations.worker.db.claim_admin_job", exploding_claim)

    with pytest.raises(RuntimeError, match="krasch före claim"):
        run_job("job-1", db_path=db_path, registry=_registry(lambda c, p: None))

    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        row = db.get_admin_job(conn, "job-1")
        assert row["status"] == "failed"
        assert row["active_slot"] is None
        assert "krasch före claim" in (row["error"] or "")
        # Sloten är fri: ett nytt jobb kan skapas direkt.
        db.create_admin_job(
            conn,
            job_id="job-2",
            operation="fake",
            params_json="{}",
            log_path=str(tmp_path / "job-2.log"),
        )
        assert db.get_active_admin_job(conn)["id"] == "job-2"
    finally:
        conn.close()


def test_run_job_failure_after_claim_fails_job_and_releases_slot(tmp_path: Path) -> None:
    """Ett startfel efter claim får inte lämna jobbet running och låsa sloten."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    result = run_job("job-1", db_path=db_path, registry=OperationRegistry())

    assert result is not None
    assert result["status"] == "failed"
    assert result["active_slot"] is None
    assert "fake" in (result["error"] or "")

    _create_job(tmp_path, db_path, job_id="job-2")
    conn = db.connect(db_path)
    try:
        assert db.get_active_admin_job(conn)["id"] == "job-2"
    finally:
        conn.close()


def test_run_job_claim_of_already_claimed_job_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=1234)
    conn.close()

    result = run_job("job-1", db_path=db_path, registry=_registry(lambda c, p: None))

    assert result is not None
    assert result["status"] == "running"
    assert result["pid"] == 1234


def test_run_job_marks_cancelled_when_cancel_races_success(tmp_path: Path, monkeypatch) -> None:
    """Regression (TOCTOU): cancel mellan statusläsning och succeeded får inte
    lämna jobbet fastnat i cancel_requested."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    real_finish = db.finish_admin_job

    def racing_finish(conn, job_id, *, status, exit_code, error=None):
        if status == "succeeded":
            # En parallell cancel hinner mellan statusläsningen och slutförandet.
            db.request_admin_job_cancel(conn, job_id)
            raise db.InvalidAdminJobTransition("kapplöpning")
        return real_finish(conn, job_id, status=status, exit_code=exit_code, error=error)

    monkeypatch.setattr("operations.worker.db.finish_admin_job", racing_finish)

    result = run_job("job-1", db_path=db_path, registry=_registry(lambda c, p: None))

    assert result is not None
    assert result["status"] == "cancelled"
    assert result["exit_code"] == 130
    assert result["active_slot"] is None


def test_start_job_creates_row_and_spawns_worker(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    log_root = tmp_path / "logs"

    spawns: list[tuple[list, dict]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            spawns.append((argv, kwargs))

    monkeypatch.setattr("operations.job_service.subprocess.Popen", FakePopen)

    job = start_job("fake", {}, registry=_registry(lambda c, p: None), db_path=db_path, log_root=log_root)

    assert job["operation"] == "fake"
    assert spawns
    argv, kwargs = spawns[0]
    assert argv[:2] == [sys.executable, "-m"]
    assert argv[2] == "operations.worker"
    assert argv[3] == "--job-id"
    assert argv[4] == job["id"]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["STATE_DB"] == str(db_path)

    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        row = db.get_admin_job(conn, job["id"])
        assert row["status"] == "queued"
        assert row["log_path"] == job["log_path"]
    finally:
        conn.close()


def test_start_job_creates_log_before_spawning_worker(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    log_root = tmp_path / "logs"
    log_existed_at_spawn: list[bool] = []

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            conn = db.connect(db_path)
            try:
                job = db.get_active_admin_job(conn)
                log_existed_at_spawn.append(Path(job["log_path"]).is_file())
            finally:
                conn.close()

    monkeypatch.setattr("operations.job_service.subprocess.Popen", FakePopen)

    start_job(
        "fake",
        {},
        registry=_registry(lambda c, p: None),
        db_path=db_path,
        log_root=log_root,
    )

    assert log_existed_at_spawn == [True]


def test_start_job_marks_failed_when_popen_fails(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"

    def failing_popen(*args, **kwargs):
        raise OSError("ingen process")

    monkeypatch.setattr("operations.job_service.subprocess.Popen", failing_popen)

    with pytest.raises(OperationFailed, match="worker"):
        start_job(
            "fake", {}, registry=_registry(lambda c, p: None), db_path=db_path,
            log_root=tmp_path / "logs",
        )

    conn = db.connect(db_path)
    db.init_schema(conn)
    try:
        jobs = db.list_admin_jobs(conn)
        assert len(jobs) == 1
        assert jobs[0]["status"] == "failed"
    finally:
        conn.close()


def test_reconcile_marks_interrupted_when_stale_heartbeat_and_no_process(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=99999999)
    conn.execute(
        "UPDATE admin_jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    conn.commit()
    conn.close()

    job = reconcile_active_job(db_path=db_path)

    assert job is not None
    assert job["status"] == "interrupted"


def test_reconcile_keeps_lock_when_process_is_alive(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.execute(
        "UPDATE admin_jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    conn.commit()
    conn.close()

    job = reconcile_active_job(db_path=db_path)

    assert job is not None
    assert job["status"] == "running"


def test_reconcile_interrupts_stale_pidless_queued_job(tmp_path: Path) -> None:
    """Regression: ett pid-löst queued-jobb som är gammalt antas vara lämnat
    av en kraschad worker och får inte blockera kön."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    conn.execute(
        "UPDATE admin_jobs SET created_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    conn.commit()
    conn.close()

    job = reconcile_active_job(db_path=db_path)

    assert job is not None
    assert job["status"] == "interrupted"

    # Sloten är fri igen: ett nytt jobb kan skapas utan ActiveAdminJobError.
    _create_job(tmp_path, db_path, job_id="job-2")
    conn = db.connect(db_path)
    try:
        assert db.get_active_admin_job(conn)["id"] == "job-2"
    finally:
        conn.close()


def test_reconcile_leaves_fresh_queued_job_untouched(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    job = reconcile_active_job(db_path=db_path)

    assert job is not None
    assert job["status"] == "queued"


def test_reconcile_treats_interrupt_race_as_already_handled(tmp_path: Path, monkeypatch) -> None:
    """Regression: parallellt avbrott (InvalidAdminJobTransition) är inte ett fel."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=99999999)
    conn.execute(
        "UPDATE admin_jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    conn.commit()
    conn.close()

    def racing_interrupt(conn, job_id):
        raise db.InvalidAdminJobTransition("parallell körning hann före")

    monkeypatch.setattr(
        "operations.job_service.db.mark_admin_job_interrupted", racing_interrupt
    )

    job = reconcile_active_job(db_path=db_path)

    assert job is not None
    assert job["status"] == "running"


def test_cancel_job_escalates_to_sigkill_after_grace(tmp_path: Path, monkeypatch) -> None:
    """Regression: ett jobb som ignorerar SIGTERM dödas med SIGKILL efter grace."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.close()

    signals: list[tuple[str, int, int]] = []
    monkeypatch.setattr(
        "operations.job_service.os.kill",
        lambda pid, sig: signals.append(("kill", pid, sig)),
    )
    monkeypatch.setattr(
        "operations.job_service.os.killpg",
        lambda pid, sig: signals.append(("killpg", pid, sig)),
    )

    # Jobbet "ignorerar" SIGTERM: statusen förblir cancel_requested tills
    # grace-perioden löpt ut och processgruppen dödas.
    assert cancel_job(db_path=db_path, kill_grace_seconds=0.1) is True

    sigterm = ("kill", os.getpid(), signal.SIGTERM)
    sigkill = ("killpg", os.getpid(), signal.SIGKILL)
    assert sigterm in signals
    assert sigkill in signals
    assert signals.index(sigterm) < signals.index(sigkill)

    conn = db.connect(db_path)
    try:
        assert db.get_admin_job(conn, "job-1")["status"] == "cancel_requested"
    finally:
        conn.close()


def test_cancel_job_skips_sigkill_when_job_finishes_promptly(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.close()

    def cooperative_kill(pid, sig):
        # SIGTERM: jobbet avslutar kontrollerat direkt.
        cancel_conn = db.connect(db_path)
        db.init_schema(cancel_conn)
        db.finish_admin_job(cancel_conn, "job-1", status="cancelled", exit_code=130)
        cancel_conn.close()

    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr("operations.job_service.os.kill", cooperative_kill)
    monkeypatch.setattr(
        "operations.job_service.os.killpg",
        lambda pid, sig: killpg_calls.append((pid, sig)),
    )

    assert cancel_job(db_path=db_path, kill_grace_seconds=5.0) is True
    assert killpg_calls == []


def test_cancel_job_sends_no_signal_on_stale_heartbeat(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.execute(
        "UPDATE admin_jobs SET heartbeat_at='2000-01-01T00:00:00+00:00' WHERE id='job-1'"
    )
    conn.commit()
    conn.close()

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "operations.job_service.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        "operations.job_service.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert cancel_job(db_path=db_path, kill_grace_seconds=0.1) is True
    assert signals == []

    conn = db.connect(db_path)
    try:
        assert db.get_admin_job(conn, "job-1")["status"] == "cancel_requested"
    finally:
        conn.close()


def test_cancel_queued_job_without_pid_sends_no_signal(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)  # queued med pid=None

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "operations.job_service.os.kill",
        lambda pid, sig: signals.append((pid, sig)),
    )
    monkeypatch.setattr(
        "operations.job_service.os.killpg",
        lambda pid, sig: signals.append((pid, sig)),
    )

    assert cancel_job(db_path=db_path, kill_grace_seconds=0.1) is True
    assert signals == []


def test_serialize_params_rejects_empty_required_path() -> None:
    definition = _definition(
        parameters=(
            ParameterDefinition("inp", ("--in",), "path", None, "PDF-fil", required=True),
        )
    )

    with pytest.raises(ValueError, match="Obligatorisk"):
        serialize_params(definition, {"inp": ""})
    with pytest.raises(ValueError, match="Obligatorisk"):
        serialize_params(definition, {})


def test_serialize_params_rejects_and_omits_secret() -> None:
    definition = _definition(
        parameters=(
            ParameterDefinition("api_key", ("--api-key",), "str", "", "nyckel", secret=True),
            ParameterDefinition("jobs", ("--jobs",), "int", 4, "Antal jobb"),
        )
    )

    with pytest.raises(ValueError, match="miljövariabel"):
        serialize_params(definition, {"api_key": "hemlig"})

    payload = json.loads(serialize_params(definition, {}))
    assert "api_key" not in payload
    assert payload == {"jobs": 4}


def test_serialize_deserialize_roundtrip_path() -> None:
    definition = _definition(
        parameters=(ParameterDefinition("out", ("--out",), "path", "generated/text", "Utkatalog"),)
    )

    payload = json.loads(serialize_params(definition, {"out": Path("generated/nytt")}))
    assert payload == {"out": "generated/nytt"}
    assert deserialize_params(definition, serialize_params(definition, {"out": Path("generated/nytt")})) == {
        "out": Path("generated/nytt")
    }


def test_read_log_tail_returns_last_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "j.log"
    log_path.write_text("rad ett\nrad två\nrad tre\n", encoding="utf-8")

    assert read_log_tail(log_path, lines=2) == "rad två\nrad tre\n"


def test_follow_log_streams_appended_lines(tmp_path: Path) -> None:
    log_path = tmp_path / "j.log"
    log_path.write_text("", encoding="utf-8")

    stop = threading.Event()

    def appender() -> None:
        time.sleep(0.1)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("rad från tråd\n")
        time.sleep(0.1)
        stop.set()

    thread = threading.Thread(target=appender, daemon=True)
    thread.start()

    lines = list(follow_log(log_path, stop=stop.is_set, poll_interval=0.02))
    thread.join(timeout=5)

    assert "rad från tråd\n" in lines


def test_jobs_cli_status(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    out = io.StringIO()
    err = io.StringIO()
    rc = jobs_cli.main(["status"], stdout=out, stderr=err, db_path=db_path)

    assert rc == 0
    assert "queued" in out.getvalue()


def test_jobs_cli_list(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    out = io.StringIO()
    rc = jobs_cli.main(["list"], stdout=out, stderr=io.StringIO(), db_path=db_path)

    assert rc == 0
    assert "job-1" in out.getvalue()
    assert "fake" in out.getvalue()


def test_jobs_cli_log(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    (tmp_path / "job-1.log").write_text("rad ett\nrad två\n", encoding="utf-8")

    out = io.StringIO()
    rc = jobs_cli.main(
        ["log", "--job-id", "job-1"], stdout=out, stderr=io.StringIO(), db_path=db_path
    )

    assert rc == 0
    assert "rad ett" in out.getvalue()
    assert "rad två" in out.getvalue()


def test_jobs_cli_log_follow_without_id_stops_when_original_job_finishes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    (tmp_path / "job-1.log").write_text("", encoding="utf-8")
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.close()

    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(
            jobs_cli.main(
                ["log", "--follow"],
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                db_path=db_path,
            )
        ),
        daemon=True,
    )
    thread.start()
    time.sleep(0.1)

    conn = db.connect(db_path)
    db.finish_admin_job(conn, "job-1", status="succeeded", exit_code=0)
    conn.close()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "log --follow fastnade efter att jobbet avslutats"
    assert result == [0]


def test_jobs_cli_cancel(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)
    conn = db.connect(db_path)
    db.claim_admin_job(conn, "job-1", pid=os.getpid())
    conn.close()

    # cancel_job själv testas separat (inkl. SIGKILL-eskalering) — här räcker
    # CLI-plumbningen.
    calls: list[dict] = []
    monkeypatch.setattr(
        "scripts.jobs.cancel_job", lambda **kwargs: calls.append(kwargs) or True
    )

    out = io.StringIO()
    rc = jobs_cli.main(
        ["cancel", "--job-id", "job-1"], stdout=out, stderr=io.StringIO(), db_path=db_path
    )

    assert rc == 0
    assert "Avbrytning begärd" in out.getvalue()
    assert calls == [{"job_id": "job-1", "db_path": db_path}]


def test_jobs_cli_start_with_active_job_gives_friendly_error(tmp_path: Path) -> None:
    """Regression: dubbelstart ger ett vänligt felmeddelande, inte traceback."""
    db_path = tmp_path / "state.db"
    _create_job(tmp_path, db_path)

    out = io.StringIO()
    err = io.StringIO()
    rc = jobs_cli.main(
        ["start", "fake"],
        stdout=out,
        stderr=err,
        db_path=db_path,
        registry=_registry(lambda c, p: None),
    )

    assert rc == 1
    assert "körs redan" in err.getvalue()
    assert "Traceback" not in err.getvalue()


def test_jobs_cli_start_with_worker_failure_gives_friendly_error(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "state.db"

    def failing_popen(*args, **kwargs):
        raise OSError("ingen process")

    monkeypatch.setattr("operations.job_service.subprocess.Popen", failing_popen)

    out = io.StringIO()
    err = io.StringIO()
    rc = jobs_cli.main(
        ["start", "fake"],
        stdout=out,
        stderr=err,
        db_path=db_path,
        registry=_registry(lambda c, p: None),
    )

    assert rc == 1
    assert "worker" in err.getvalue()
    assert "Traceback" not in err.getvalue()
