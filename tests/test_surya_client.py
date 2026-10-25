"""Tester för Surya-klienten: protokoll, felkoder och processlivscykel mot falska workers."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

import surya_client
import surya_worker
from surya_client import SuryaPageError, SuryaUnavailable, SuryaWorker

HEADER = textwrap.dedent(
    """
    import json, sys, os, time, subprocess
    def reply(**kw):
        sys.stdout.write(json.dumps(kw) + "\\n"); sys.stdout.flush()
    """
)


def fake(tmp_path: Path, body: str) -> Path:
    """En falsk worker: läser en begäran per rad och kör ``body`` med ``req``."""
    script = tmp_path / "fake_worker.py"
    script.write_text(
        HEADER
        + "if '--identity' in sys.argv:\n"
        + "    reply(protocol=1, type='identity', status='ok', identity='fake-id'); sys.exit(0)\n"
        + "for line in sys.stdin:\n    req = json.loads(line)\n"
        + textwrap.indent(textwrap.dedent(body), "    ")
    )
    return script


def worker(tmp_path: Path, body: str, timeout: float = 2.0) -> SuryaWorker:
    return SuryaWorker(
        python=Path(sys.executable), worker=fake(tmp_path, body), page_timeout=timeout
    )


ECHO = """
reply(protocol=1, type='page', pdf_stem=req['pdf_stem'], page=req['page'], status='read',
      model='m', blocks=[{'html': '<p>hej</p>', 'label': 'Text'}])
"""


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_read_page_roundtrip(tmp_path):
    with worker(tmp_path, ECHO) as w:
        page = w.read_page("Pol-1", 3, tmp_path / "x.png")
        again = w.read_page("Pol-1", 4, tmp_path / "x.png")
    assert page.status == "read"
    assert page.model == "m"
    assert page.blocks == [{"html": "<p>hej</p>", "label": "Text"}]
    assert again.status == "read"


def test_runtime_error_raises_unavailable(tmp_path):
    body = """
reply(protocol=1, type='page', pdf_stem=req['pdf_stem'], page=req['page'], status='error', code='NEEDS_LLAMA_CPP', message='x')
"""
    with worker(tmp_path, body) as w, pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "NEEDS_LLAMA_CPP"


def test_page_error_is_recoverable(tmp_path):
    body = (
        """
if req['page'] == 1:
    reply(protocol=1, type='page', pdf_stem=req['pdf_stem'], page=1, status='error', code='INFERENCE_FAILED', message='x')
else:
"""
        + "    "
        + ECHO.strip().replace("\n", "\n    ")
        + "\n"
    )
    with worker(tmp_path, body) as w:
        with pytest.raises(SuryaPageError) as caught:
            w.read_page("Pol-1", 1, tmp_path / "x.png")
        assert caught.value.code == "INFERENCE_FAILED"
        assert w.read_page("Pol-1", 2, tmp_path / "x.png").status == "read"


def test_page_timeout(tmp_path):
    started = time.monotonic()
    w = worker(tmp_path, "time.sleep(60)\n", timeout=1)
    with pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "WORKER_DIED"
    assert time.monotonic() - started < 15
    assert w._process is None


def test_eof_is_worker_died(tmp_path):
    with worker(tmp_path, "sys.exit(1)\n") as w, pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "WORKER_DIED"


def test_worker_crash_kills_group(tmp_path):
    marker = tmp_path / "pids"
    body = f"""
child = subprocess.Popen(['sleep', '60'])
open({str(marker)!r}, 'w').write(f"{{os.getpid()}} {{child.pid}}")
os._exit(1)
"""
    w = worker(tmp_path, body)
    with pytest.raises(SuryaUnavailable):
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    w.close()
    worker_pid, child_pid = map(int, marker.read_text().split())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and (alive(child_pid) or alive(worker_pid)):
        time.sleep(0.05)
    assert not alive(child_pid)
    assert not alive(worker_pid)
    with pytest.raises(ProcessLookupError):
        os.killpg(worker_pid, 0)


@pytest.mark.parametrize(
    "reply_args",
    [
        "pdf_stem='ANNAN', page=req['page'], status='read', model='m', blocks=[]",
        "pdf_stem=req['pdf_stem'], page=99, status='read', model='m', blocks=[]",
        "status='error', code='MALFORMED_REQUEST', message='x'",
        "pdf_stem=req['pdf_stem'], status='read', model='m', blocks=[]",
    ],
)
def test_mismatched_result_is_protocol_mismatch(tmp_path, reply_args):
    body = f"reply(protocol=1, type='page', {reply_args})\n"
    w = worker(tmp_path, body)
    with pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "PROTOCOL_MISMATCH"
    assert w._process is None


def test_bad_json_is_protocol_mismatch(tmp_path):
    w = worker(tmp_path, "print('inte json', flush=True)\n")
    with pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "PROTOCOL_MISMATCH"


def test_spawn_failure_is_unavailable(tmp_path):
    w = SuryaWorker(python=tmp_path / "finns-inte", worker=tmp_path / "w.py")
    with pytest.raises(SuryaUnavailable) as caught:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
    assert caught.value.code == "WORKER_DIED"


def test_identity_and_availability(tmp_path, monkeypatch):
    w = worker(tmp_path, ECHO)
    assert w.identity() == "fake-id"
    monkeypatch.setattr(surya_client, "WORKER_PATH", fake(tmp_path, ECHO))
    monkeypatch.setenv("PALME_SURYA_PYTHON", sys.executable)
    assert surya_client.surya_layout_available() is True
    monkeypatch.setenv("PALME_SURYA_PYTHON", str(tmp_path / "saknas"))
    assert surya_client.surya_layout_available() is False


def test_identity_none_when_worker_silent(tmp_path):
    script = tmp_path / "tyst.py"
    script.write_text("import sys; sys.exit(3)\n")
    w = SuryaWorker(python=Path(sys.executable), worker=script)
    assert w.identity() is None


def test_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("PALME_SURYA_PYTHON", raising=False)
    assert surya_client.surya_python() == surya_client.DEFAULT_SURYA_PYTHON
    monkeypatch.setenv("PALME_SURYA_PYTHON", str(tmp_path / "py"))
    assert surya_client.surya_python() == tmp_path / "py"


def test_constants_match_worker():
    assert surya_client.PROTOCOL_VERSION == surya_worker.PROTOCOL_VERSION
    assert surya_client.PAGE_TYPE == surya_worker.PAGE_TYPE
    assert surya_client.IDENTITY_TYPE == surya_worker.IDENTITY_TYPE
    assert surya_client.IDENTITY_FLAG == surya_worker.IDENTITY_FLAG
    assert surya_client.MAX_RESULT_BYTES == surya_worker.MAX_RESULT_BYTES
    assert surya_client.STATUS_READ == surya_worker.STATUS_READ
    assert surya_client.STATUS_EMPTY == surya_worker.STATUS_EMPTY
    assert surya_client.STATUS_ERROR == surya_worker.STATUS_ERROR
    assert surya_client.STATUS_OK == surya_worker.STATUS_OK
    assert {
        surya_worker.NEEDS_SURYA,
        surya_worker.NEEDS_LLAMA_CPP,
        surya_worker.NEEDS_SURYA_MODEL,
        surya_worker.SURYA_START_FAILED,
    } == surya_client.UNAVAILABLE_CODES


# --- avbrott, identitetsprobe och tillgänglighet (Ruling X/Y/AB) -------------------


def _lingering_worker(tmp_path: Path) -> Path:
    """Svarar på sidor men stannar kvar 60 s efter stängd stdin (graciös väntan märks)."""
    script = tmp_path / "lingering.py"
    script.write_text(
        HEADER
        + "for line in sys.stdin:\n    req = json.loads(line)\n"
        + textwrap.indent(textwrap.dedent(ECHO), "    ")
        + "time.sleep(60)\n"
    )
    return script


def test_exit_with_exception_kills_group_without_graceful_wait(tmp_path):
    w = SuryaWorker(python=Path(sys.executable), worker=_lingering_worker(tmp_path))
    with pytest.raises(RuntimeError), w:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
        assert w._process is not None
        pid = w._process.pid
        started = time.monotonic()
        raise RuntimeError("avbrott")
    assert time.monotonic() - started < surya_client.CLOSE_WAIT_SECONDS / 2
    assert not alive(pid)


def test_exit_without_exception_waits_gracefully(tmp_path):
    """Normal utgång stänger stdin och väntar på workern (som här själv avslutar)."""
    with worker(tmp_path, ECHO) as w:
        w.read_page("Pol-1", 1, tmp_path / "x.png")
        assert w._process is not None
        pid = w._process.pid
    assert not alive(pid)


def test_identity_timeout_kills_whole_group(tmp_path, monkeypatch):
    marker = tmp_path / "child.pid"
    script = tmp_path / "hang.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setattr(surya_client, "IDENTITY_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()
    assert surya_client._run_identity(Path(sys.executable), script) is None
    assert time.monotonic() - started < 8
    child_pid = int(marker.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and alive(child_pid):
        time.sleep(0.05)
    assert not alive(child_pid)


def test_identity_timeout_is_30_seconds():
    assert surya_client.IDENTITY_TIMEOUT_SECONDS == 30.0


def test_availability_false_when_llama_server_unresolved(tmp_path, monkeypatch):
    script = tmp_path / "id.py"
    script.write_text(
        HEADER
        + "reply(protocol=1, type='identity', status='ok', "
        "identity='surya-ocr 0.22.1 backend unresolved llama-server unresolved')\n"
    )
    monkeypatch.setattr(surya_client, "WORKER_PATH", script)
    monkeypatch.setenv("PALME_SURYA_PYTHON", sys.executable)
    assert surya_client.surya_layout_available() is False


def test_exit_code_constant():
    assert surya_client.EXIT_SURYA_UNAVAILABLE == 3


def test_unavailable_reason_variants(tmp_path, monkeypatch):
    monkeypatch.setenv("PALME_SURYA_PYTHON", str(tmp_path / "saknas"))
    assert "tolk saknas" in surya_client.surya_layout_unavailable_reason()

    silent = tmp_path / "silent.py"
    silent.write_text("import sys; sys.exit(1)\n")
    monkeypatch.setattr(surya_client, "WORKER_PATH", silent)
    monkeypatch.setenv("PALME_SURYA_PYTHON", sys.executable)
    assert "svarade inte på --identity" in surya_client.surya_layout_unavailable_reason()

    unresolved = tmp_path / "id.py"
    unresolved.write_text(
        HEADER + "reply(protocol=1, type='identity', status='ok', identity='x llama-server unresolved')\n"
    )
    monkeypatch.setattr(surya_client, "WORKER_PATH", unresolved)
    assert "LLAMA_CPP_BINARY" in surya_client.surya_layout_unavailable_reason()

    monkeypatch.setattr(surya_client, "WORKER_PATH", fake(tmp_path, ECHO))
    assert surya_client.surya_layout_unavailable_reason() is None
