"""Tester för ``scripts/install.py --surya-layout`` (kommandona byggs utan att köras)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("venv_exists", [False, True])
def test_install_fails_when_identity_unresolved(
    tmp_path: Path, monkeypatch, capsys, venv_exists: bool
) -> None:
    import subprocess

    venv = tmp_path / "surya-venv"
    if venv_exists:
        (venv / "bin").mkdir(parents=True)
    monkeypatch.setattr(install, "SURYA_LAYOUT_VENV", venv)
    monkeypatch.setattr(install, "venv_python_version", lambda path: "3.12")
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


def test_venv_without_pip_gets_pip_bootstrapped(tmp_path) -> None:
    # En venv skapad av `uv venv`/`uv sync` saknar pip; install.py ska då köra ensurepip.
    (tmp_path / "bin").mkdir()
    assert install.pip_bootstrap_command(tmp_path) == [
        str(tmp_path / "bin" / "python"),
        "-m",
        "ensurepip",
        "--upgrade",
    ]


def test_venv_with_pip_needs_no_bootstrap(tmp_path) -> None:
    (tmp_path / "lib" / "python3.12" / "site-packages" / "pip").mkdir(parents=True)
    assert install.pip_bootstrap_command(tmp_path) is None


def test_newest_python_is_chosen() -> None:
    found = {"python3": "/b/python3", "python3.12": "/u/python3.12", "python3.15": "/b/python3.15"}
    versions = {"/b/python3": (3, 15), "/u/python3.12": (3, 12), "/b/python3.15": (3, 15)}
    exe = install.newest_python(which=found.get, version_of=versions.get)
    assert versions[exe] == (3, 15)


def test_python_older_than_311_is_ignored() -> None:
    found = {"python3": "/b/python3"}
    assert install.newest_python(which=found.get, version_of={"/b/python3": (3, 10)}.get) is None


def test_sv_dictionary_is_written_when_both_files_download(tmp_path) -> None:
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return url.rsplit("/", 1)[1].encode()

    assert install.download_sv_dictionary(tmp_path / "Spelling", fetch=fetch) is True
    assert (tmp_path / "Spelling" / "sv_SE.aff").read_bytes() == b"sv_SE.aff"
    assert (tmp_path / "Spelling" / "sv_SE.dic").read_bytes() == b"sv_SE.dic"
    assert all(url.startswith("https://") for url in fetched)


def test_sv_dictionary_writes_nothing_when_a_download_fails(tmp_path) -> None:
    def fetch(url: str) -> bytes:
        if url.endswith(".dic"):
            raise OSError("nätet nere")
        return b"aff"

    assert install.download_sv_dictionary(tmp_path / "Spelling", fetch=fetch) is False
    assert not (tmp_path / "Spelling").exists() or not any((tmp_path / "Spelling").iterdir())


def test_sv_dictionary_url_is_pinned_to_a_commit() -> None:
    assert "/master/" not in install.SV_DICTIONARY_URL
    assert install.SV_DICTIONARY_COMMIT in install.SV_DICTIONARY_URL


def test_index_is_ensured_with_the_venv_interpreter(tmp_path) -> None:
    cmd = install.ensure_index_command(tmp_path)
    assert cmd[:2] == [str(tmp_path / "bin" / "python"), "-c"]
    assert "ingest.ensure_table()" in cmd[2]


def test_missing_llm_key_is_pointed_out() -> None:
    hint = install.missing_llm_key_hint({})
    assert hint is not None and "CLAUDE_CODE_OAUTH_TOKEN" in hint


def test_any_llm_key_silences_the_hint() -> None:
    assert install.missing_llm_key_hint({"DEEPSEEK_API_KEY": "x"}) is None
    assert install.missing_llm_key_hint({"OPENAI_API_KEY": ""}) is not None


def test_pip_extras_include_graph() -> None:
    assert install.pip_extras(with_dev=True) == "dev,web,graph"
    assert install.pip_extras(with_dev=False) == "web,graph"


def test_brew_installs_podman() -> None:
    assert "podman" in install.BREW_PACKAGES
