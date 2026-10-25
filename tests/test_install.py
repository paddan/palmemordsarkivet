"""Tester för ``scripts/install.py --surya-layout`` (kommandona byggs utan att köras)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("palme_install", ROOT / "scripts" / "install.py")
assert _spec and _spec.loader
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

VENV = Path("/x/surya-venv")


def test_everything_missing_installs_all() -> None:
    cmds = install.surya_layout_commands({}, VENV, venv_exists=False, root=Path("/r"))
    assert cmds == [
        ["brew", "install", "--quiet", "llama.cpp", "uv"],
        ["uv", "venv", "--python", "3.12", "/x/surya-venv"],
        ["uv", "pip", "install", "--python", "/x/surya-venv/bin/python", "surya-ocr==0.22.1"],
        ["/x/surya-venv/bin/python", "/r/src/surya_worker.py", "--identity"],
    ]


def test_existing_tools_and_venv_are_not_reinstalled() -> None:
    have = {"llama-server": True, "uv": True}
    cmds = install.surya_layout_commands(have, VENV, venv_exists=True, root=Path("/r"))
    assert [c[0] for c in cmds] == ["uv", "/x/surya-venv/bin/python"]
    assert cmds[0][:3] == ["uv", "pip", "install"]


def test_only_missing_brew_package_is_installed() -> None:
    cmds = install.surya_layout_commands({"uv": True}, VENV, venv_exists=True)
    assert cmds[0] == ["brew", "install", "--quiet", "llama.cpp"]


def test_commands_never_touch_project_venv() -> None:
    cmds = install.surya_layout_commands({}, VENV, venv_exists=False)
    assert all(str(ROOT / ".venv") not in " ".join(c) for c in cmds)
    assert all(c[0] != "pip" for c in cmds)


def test_flag_not_run_without_it(monkeypatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(install, "install_surya_layout", lambda: called.append(1) or 0)
    assert install.main(["--help"]) == 0
    assert called == []


def test_llama_server_present_via_env_binary(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "llama-server"
    binary.write_text("", encoding="utf-8")
    monkeypatch.setattr(install.shutil, "which", lambda cmd: None)
    assert install.llama_server_present({"LLAMA_CPP_BINARY": str(binary)}) is True
    assert install.llama_server_present({"LLAMA_CPP_BINARY": str(tmp_path / "saknas")}) is False
    assert install.llama_server_present({}) is False


def test_unresolved_identity_is_detected_and_message_names_env() -> None:
    from surya_client import LLAMA_UNRESOLVED

    assert install.identity_reports_missing_llama(f"surya 0.22.1; {LLAMA_UNRESOLVED}") is True
    assert install.identity_reports_missing_llama("surya 0.22.1; llama-server /x/y") is False
    assert "LLAMA_CPP_BINARY" in install.LLAMA_MISSING_MESSAGE
    assert "PATH" in install.LLAMA_MISSING_MESSAGE


def test_install_fails_when_identity_unresolved(monkeypatch, capsys) -> None:
    import subprocess

    monkeypatch.setattr(install.shutil, "which", lambda cmd: "/bin/x")
    monkeypatch.setattr(install, "llama_server_present", lambda env=None: True)

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="x llama-server unresolved\n")

    monkeypatch.setattr(install.subprocess, "run", fake_run)
    assert install.install_surya_layout() == 1
    assert "LLAMA_CPP_BINARY" in capsys.readouterr().err


def test_existing_venv_with_python_312_is_accepted() -> None:
    assert install.venv_version_problem("3.12", VENV) is None


def test_existing_venv_with_other_python_is_refused() -> None:
    problem = install.venv_version_problem("3.14", VENV)
    assert problem is not None
    assert "Python 3.14" in problem and "3.12" in problem and str(VENV) in problem
    assert "PALME_SURYA_PYTHON" in problem


def test_unrunnable_venv_python_is_refused() -> None:
    problem = install.venv_version_problem(None, VENV)
    assert problem is not None and "gick att köra" in problem


def test_venv_python_version_reads_real_interpreter(tmp_path: Path) -> None:
    import sys

    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    assert install.venv_python_version(venv) == f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert install.venv_python_version(tmp_path / "saknas") is None
