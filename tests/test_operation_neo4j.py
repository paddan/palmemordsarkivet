"""Tester för Neo4j-livscykeln: lösenordshantering och argv-läckage."""

from __future__ import annotations

import stat
from pathlib import Path

from operations import neo4j
from operations.context import OperationContext, ProgressSink


class RecordingSink(ProgressSink):
    """Spelar in loggrader och progressuppdateringar."""

    def __init__(self) -> None:
        self.logs: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))

    def write_progress(self, update) -> None:
        pass

    def write_traceback(self, exc: BaseException) -> None:
        pass


def _context() -> OperationContext:
    return OperationContext(sink=RecordingSink(), cancel_requested=lambda: False)


def test_ensure_password_creates_file_with_mode_600(tmp_path: Path, monkeypatch) -> None:
    neo4j_dir = tmp_path / "neo4j"
    password_file = neo4j_dir / ".password"
    monkeypatch.setattr(neo4j, "NEO4J_DIR", neo4j_dir)
    monkeypatch.setattr(neo4j, "PASSWORD_FILE", password_file)

    ctx = _context()
    password = neo4j._ensure_password(ctx)

    assert password_file.exists()
    assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
    assert len(password) == 32
    assert password_file.read_text(encoding="utf-8").strip() == password

    # Andra anropet återanvänder det befintliga lösenordet.
    assert neo4j._ensure_password(ctx) == password


def test_neo4j_start_passes_password_via_env_not_argv(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    class FakeContext:
        def run_process(self, argv, *, cwd, env=None):
            calls.append({"argv": list(argv), "env": dict(env or {})})
            return 0

        def log(self, message, *, level="info"):
            pass

    monkeypatch.setattr(neo4j, "_ensure_password", lambda ctx: "hemligt-lösenord")
    monkeypatch.setattr(neo4j, "_container_running", lambda ctx: False)
    monkeypatch.setattr(neo4j, "_container_exists", lambda ctx: False)
    monkeypatch.setattr(neo4j, "_wait_http", lambda ctx, password: 0)
    monkeypatch.setattr(neo4j, "NEO4J_DIR", tmp_path / "neo4j")

    rc = neo4j.neo4j_start(FakeContext())  # type: ignore[arg-type]

    assert rc == 0
    run_calls = [call for call in calls if call["argv"][:2] == ["podman", "run"]]
    assert len(run_calls) == 1
    argv_text = " ".join(run_calls[0]["argv"])
    assert "hemligt-lösenord" not in argv_text
    assert "NEO4J_AUTH=" not in argv_text
    assert run_calls[0]["env"] == {"NEO4J_AUTH": "neo4j/hemligt-lösenord"}
