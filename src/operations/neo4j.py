"""Migrerad logik från ``neo4j.sh`` + ``load_graph.sh``.

Lokal Podman/Neo4j-livscykel. Lösenordet genereras första gången och lagras i
``neo4j/.password`` med mode 600 — värdet visas aldrig i logg eller adminsida.
"""

from __future__ import annotations

import secrets
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from .context import OperationContext, ensure_terminal_context

ROOT = Path(__file__).resolve().parents[2]
NEO4J_DIR = ROOT / "neo4j"
PASSWORD_FILE = NEO4J_DIR / ".password"
CONTAINER = "palme-neo4j"


def _ctx(context):
    return ensure_terminal_context(context)


def _podman(
    ctx: OperationContext,
    *argv: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> int:
    rc = ctx.run_process(["podman", *argv], cwd=ROOT, env=env)
    if check and rc != 0:
        raise RuntimeError(f"podman {' '.join(argv)} misslyckades (exitkod {rc})")
    return rc


def _ensure_password(ctx: OperationContext) -> str:
    """Returnera Neo4j-lösenordet och skapa det vid behov."""
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text(encoding="utf-8").strip()
    NEO4J_DIR.mkdir(parents=True, exist_ok=True)
    password = secrets.token_hex(16)
    PASSWORD_FILE.write_text(password + "\n", encoding="utf-8")
    PASSWORD_FILE.chmod(0o600)
    ctx.log(f"Genererade nytt lösenord → {PASSWORD_FILE}")
    return password


def neo4j_status(context: OperationContext | None = None) -> int:
    ctx = _ctx(context)
    rc = ctx.run_process(
        ["podman", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}: {{.Status}}"],
        cwd=ROOT,
    )
    if rc != 0:
        ctx.log(f"{CONTAINER}: kör inte")
    return 0


def neo4j_start(context: OperationContext | None = None) -> int:
    ctx = _ctx(context)
    if ctx.run_process(["podman", "info"], cwd=ROOT) != 0:
        ctx.log("Startar podman-maskinen…")
        _podman(ctx, "machine", "start")

    password = _ensure_password(ctx)

    if ctx.run_process(
        ["podman", "ps", "--format", "{{.Names}}"], cwd=ROOT
    ) == 0 and _container_running(ctx):
        ctx.log("Neo4j kör redan.")
        return _wait_http(ctx, password)

    # Befintlig stoppad container.
    if _container_exists(ctx):
        ctx.log("Startar befintlig container…")
        _podman(ctx, "start", CONTAINER)
    else:
        ctx.log("Skapar container palme-neo4j (hämtar neo4j:5 första gången)…")
        (NEO4J_DIR / "data").mkdir(parents=True, exist_ok=True)
        # Lösenordet skickas via processmiljön i stället för i argv, så att det
        # aldrig syns i jobbloggen (run_process loggar kommandoraden).
        _podman(
            ctx, "run", "-d", "--name", CONTAINER, "--restart", "unless-stopped",
            "-p", "7474:7474", "-p", "7687:7687",
            "-e", "NEO4J_server_memory_heap_max__size=2G",
            "-v", f"{NEO4J_DIR / 'data'}:/data",
            "neo4j:5",
            env={"NEO4J_AUTH": f"neo4j/{password}"},
        )
    return _wait_http(ctx, password)


def neo4j_stop(context: OperationContext | None = None) -> int:
    ctx = _ctx(context)
    ctx.run_process(["podman", "stop", CONTAINER], cwd=ROOT)
    ctx.log("Neo4j stoppad.")
    return 0


def _container_running(ctx: OperationContext) -> bool:
    import subprocess

    ctx.check_cancelled()
    result = subprocess.run(
        ["podman", "ps", "--format", "{{.Names}}"], cwd=ROOT,
        capture_output=True, text=True, timeout=10,
    )
    return CONTAINER in result.stdout.splitlines()


def _container_exists(ctx: OperationContext) -> bool:
    import subprocess

    ctx.check_cancelled()
    result = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"], cwd=ROOT,
        capture_output=True, text=True, timeout=10,
    )
    return CONTAINER in result.stdout.splitlines()


def _wait_http(ctx: OperationContext, password: str) -> int:
    ctx.log("Väntar på Neo4j")
    for _ in range(45):
        try:
            urllib.request.urlopen("http://localhost:7474", timeout=2)
            ctx.log(" — klar.")
            ctx.log("Browser:   http://localhost:7474  (användare: neo4j)")
            ctx.log("Ladda grafen:  .venv/bin/python scripts/load_graph.py")
            return 0
        except Exception:
            time.sleep(2)
    ctx.log("Neo4j svarade inte inom 90 s.", level="error")
    return 1
