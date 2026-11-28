"""Installera alla beroenden (Homebrew, venv, pip, tessdata)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from surya_client import LLAMA_UNRESOLVED  # noqa: E402

# Surya 2-layout kör i en egen venv (Python 3.12) bredvid projektets .venv.
SURYA_LAYOUT_VENV = Path.home() / ".local/share/palmemordsarkivet/surya-venv"
SURYA_LAYOUT_PACKAGE = "surya-ocr==0.22.1"
SURYA_LAYOUT_PYTHON_VERSION = "3.12"
SURYA_LAYOUT_BREW_TOOLS = (("llama-server", "llama.cpp"), ("uv", "uv"))

# brew-paket som huvudinstallationen kräver. podman kör Neo4j (kunskapsgrafen).
BREW_PACKAGES = (
    "ocrmypdf", "tesseract-lang", "poppler", "qpdf", "unpaper", "hunspell", "podman",
)


def pip_extras(with_dev: bool) -> str:
    """Pip-extras för huvudinstallationen; graph (neo4j) ingår alltid."""
    return "dev,web,graph" if with_dev else "web,graph"


# Homebrews hunspell levererar inga ordlistor; LibreOffices svenska (LGPL-3,
# Göran Andersson) hämtas då i stället. Pinnad commit, så en ändring uppströms
# inte tyst byter ordlista (och därmed kvalitetspoängen).
SV_DICTIONARY_COMMIT = "4f1df64b2e8e14751aa7c2205d170ca016071315"
SV_DICTIONARY_URL = (
    f"https://raw.githubusercontent.com/LibreOffice/dictionaries/{SV_DICTIONARY_COMMIT}"
    "/sv_SE/dictionaries/{name}"
)


def _python_version(exe: str) -> tuple[int, int] | None:
    try:
        out = subprocess.check_output([exe, "-c", "import sys; print(*sys.version_info[:2])"], text=True)
        major, minor = out.split()
        return int(major), int(minor)
    except Exception:
        return None


def newest_python(
    which: Callable[[str], str | None] = shutil.which,
    version_of: Callable[[str], tuple[int, int] | None] = _python_version,
) -> str | None:
    """Den nyaste tolken i PATH som är minst Python 3.11, eller ``None``.

    Letar efter ``python3`` och ``python3.11``–``python3.30`` och väljer högst
    version, så en ny Homebrew-Python används utan att listan behöver ändras.
    """
    best: tuple[tuple[int, int], str] | None = None
    for cmd in ["python3", *(f"python3.{minor}" for minor in range(30, 10, -1))]:
        exe = which(cmd)
        if exe is None:
            continue
        version = version_of(exe)
        if version is None or version < (3, 11):
            continue
        if best is None or version > best[0]:
            best = (version, exe)
    return None if best is None else best[1]


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — fast https-adress
        data: bytes = response.read()
    return data


def download_sv_dictionary(spell_dir: Path, fetch: Callable[[str], bytes] = _fetch) -> bool:
    """Hämta sv_SE.aff/.dic till ``spell_dir``; ``False`` (och inget skrivet) vid fel.

    Filerna skrivs först när båda hämtats, så en avbruten hämtning inte lämnar
    en halv ordlista som hunspell sedan läser.
    """
    try:
        files = {name: fetch(SV_DICTIONARY_URL.format(name=name)) for name in ("sv_SE.aff", "sv_SE.dic")}
    except OSError as exc:
        print(f"  VARNING: kunde inte hämta sv_SE-ordlistan ({type(exc).__name__}: {exc})")
        return False
    spell_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        tmp = spell_dir / f".{name}.tmp"
        tmp.write_bytes(data)
        tmp.replace(spell_dir / name)
    return True


# Nycklarna för molnbackendarna (se src/backends.py); en räcker.
LLM_KEY_ENVS = (
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
)


def missing_llm_key_hint(env: Mapping[str, str] | None = None) -> str | None:
    """Påminnelse när ingen LLM-nyckel finns i miljön, annars ``None``.

    Nyckeln kan inte sättas av installationen (den sparas aldrig), men utan den
    stoppar Utredning direkt — därför sägs det här i stället för först i webben.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    if any(environ.get(name) for name in LLM_KEY_ENVS):
        return None
    return (
        "OBS: ingen LLM-nyckel i miljön — Utredning svarar inte utan en. Sätt t.ex.\n"
        "  export CLAUDE_CODE_OAUTH_TOKEN=...   # skapas med `claude setup-token`\n"
        "eller ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY eller OPENROUTER_API_KEY\n"
        "(lokal Ollama behöver ingen nyckel). Se docs/kom-igang.md, steg 2."
    )


def ensure_index_command(venv: Path) -> list[str]:
    """Kommando som skapar en tom chunk-tabell i LanceDB om indexet saknas.

    Webgränssnittet kräver tabellen; utan den kraschade ``./web.sh`` direkt
    efter installationen. Körs med venvens tolk eftersom lancedb bara finns där.
    """
    code = (
        "import sys; sys.path[:0] = ['src', 'src/rag']; import ingest; "
        "print('  tomt sökindex skapat — fyll det med ./run_pipeline.sh' "
        "if ingest.ensure_table() else '  sökindex finns redan')"
    )
    return [str(venv / "bin" / "python"), "-c", code]


def pip_bootstrap_command(venv: Path) -> list[str] | None:
    """Kommando som installerar pip i ``venv``, eller ``None`` om pip redan finns.

    En venv skapad av ``uv venv``/``uv sync`` saknar pip, och då hade
    ``pip install`` nedan kraschat på en saknad fil.
    """
    if any(venv.glob("lib/python*/site-packages/pip")):
        return None
    return [str(venv / "bin" / "python"), "-m", "ensurepip", "--upgrade"]


def llama_server_present(env: Mapping[str, str] | None = None) -> bool:
    """``llama-server`` finns i PATH, eller ``LLAMA_CPP_BINARY`` pekar på en befintlig fil."""
    environ: Mapping[str, str] = os.environ if env is None else env
    explicit = environ.get("LLAMA_CPP_BINARY", "")
    if explicit and Path(explicit).is_file():
        return True
    return shutil.which("llama-server") is not None


def identity_reports_missing_llama(identity: str) -> bool:
    """Workerns ``--identity`` säger att llama-server inte kunde lösas."""
    return LLAMA_UNRESOLVED in identity


LLAMA_MISSING_MESSAGE = (
    "Workern hittar inte llama-server. Sätt LLAMA_CPP_BINARY till sökvägen "
    "(t.ex. /opt/homebrew/bin/llama-server); brews bin-katalog kan också saknas i PATH."
)


def venv_version_problem(version: str | None, venv: Path) -> str | None:
    """Felmeddelande om en befintlig venv inte kör Python 3.12, annars ``None``.

    En befintlig venv återanvänds i stället för att skrivas över; har den fel
    Python-version installeras ``surya-ocr`` annars i en tolk där PyTorch-hjulen
    saknas, och felet syns först vid importen.
    """
    if version == SURYA_LAYOUT_PYTHON_VERSION:
        return None
    hittad = f"Python {version}" if version else "en tolk som inte gick att köra"
    return (
        f"Venv:en {venv} finns redan men har {hittad}; Surya 2-layout kräver Python "
        f"{SURYA_LAYOUT_PYTHON_VERSION}. Ta bort katalogen och kör om, eller låt "
        "PALME_SURYA_PYTHON peka på en annan venv med surya-ocr==0.22.1."
    )


def venv_python_version(venv: Path) -> str | None:
    """Python-versionen (``major.minor``) i en befintlig venv, eller ``None``."""
    try:
        out = subprocess.run(
            [str(venv / "bin" / "python"), "-c",
             "import sys; print('%d.%d' % sys.version_info[:2])"],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def surya_layout_commands(
    have: dict[str, bool], venv: Path, venv_exists: bool, root: Path = ROOT,
) -> list[list[str]]:
    """Kommandona för ``--surya-layout`` som ren funktion (inget körs här).

    ``have`` säger vilka kommandon som redan finns (``llama-server``, ``uv``);
    bara saknade paket installeras med brew. Venv:en skapas bara om den saknas,
    och sist körs workerns ``--identity`` som kontroll. Projektets egen ``.venv``
    berörs aldrig.
    """
    commands: list[list[str]] = []
    missing = [pkg for cmd, pkg in SURYA_LAYOUT_BREW_TOOLS if not have.get(cmd, False)]
    if missing:
        commands.append(["brew", "install", "--quiet", *missing])
    python = venv / "bin" / "python"
    if not venv_exists:
        commands.append(["uv", "venv", "--python", SURYA_LAYOUT_PYTHON_VERSION, str(venv)])
    commands.append(["uv", "pip", "install", "--python", str(python), SURYA_LAYOUT_PACKAGE])
    commands.append([str(python), str(root / "src" / "surya_worker.py"), "--identity"])
    return commands


def install_surya_layout() -> int:
    """Installera runtimen för Surya 2-layout (separat venv, llama.cpp, uv)."""
    if shutil.which("brew") is None:
        print("Homebrew saknas. Installera från https://brew.sh och kör sedan om.", file=sys.stderr)
        return 1
    have = {cmd: shutil.which(cmd) is not None for cmd, _ in SURYA_LAYOUT_BREW_TOOLS}
    if llama_server_present():
        have["llama-server"] = True
    venv_exists = SURYA_LAYOUT_VENV.exists()
    if venv_exists:
        problem = venv_version_problem(venv_python_version(SURYA_LAYOUT_VENV), SURYA_LAYOUT_VENV)
        if problem:
            print(problem, file=sys.stderr)
            return 1
    commands = surya_layout_commands(have, SURYA_LAYOUT_VENV, venv_exists)
    print(f"→ Surya 2-layout i {SURYA_LAYOUT_VENV}")
    for command in commands[:-1]:
        print("  $", " ".join(command))
        if subprocess.run(command, check=False).returncode != 0:
            print(f"Steget misslyckades: {' '.join(command)}", file=sys.stderr)
            return 1
    print("→ kontroll (workerns --identity):")
    check = subprocess.run(commands[-1], check=False, stdout=subprocess.PIPE, text=True)
    if check.stdout:
        print(check.stdout, end="")
    print(
        "\nObs: modellvikterna (~1,36 GiB, datalab-to/surya-ocr-2-gguf) laddas ned vid första "
        "sidan.\nHar du redan en venv med surya-ocr==0.22.1 (t.ex. infoscrys) kan "
        "PALME_SURYA_PYTHON peka på dess python i stället för den här installationen."
    )
    if identity_reports_missing_llama(check.stdout or ""):
        print(LLAMA_MISSING_MESSAGE, file=sys.stderr)
        return 1
    return check.returncode


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    with_surya = True
    with_dev = False
    surya_layout = False
    for arg in args:
        if arg == "--no-surya":
            with_surya = False
        elif arg == "--dev":
            with_dev = True
        elif arg == "--surya-layout":
            surya_layout = True
        elif arg in ("-h", "--help"):
            print("Användning: .venv/bin/python scripts/install.py [--no-surya] [--dev] [--surya-layout]")
            print("  --surya-layout  installera bara Surya 2-layouts separata venv (kräver brew)")
            return 0
        else:
            print(f"okänd flagga: {arg}", file=sys.stderr)
            return 2

    if surya_layout:
        return install_surya_layout()

    if shutil.which("brew") is None:
        print("Homebrew saknas. Installera från https://brew.sh och kör sedan om.", file=sys.stderr)
        return 1

    print("→ brew-paket…")
    subprocess.run(["brew", "install", "--quiet", *BREW_PACKAGES], check=False)

    # Hunspell svensk ordlista: symlänka brew-ordlistan om den saknas.
    # brew-hunspells sv_SE.aff/.dic till ~/Library/Spelling om de inte redan finns.
    brew_prefix = subprocess.check_output(["brew", "--prefix"], text=True).strip()
    sv_aff = Path(brew_prefix) / "share" / "hunspell" / "sv_SE.aff"
    sv_dic = Path(brew_prefix) / "share" / "hunspell" / "sv_SE.dic"
    spell_dir = Path.home() / "Library" / "Spelling"
    if all((spell_dir / name).is_file() for name in ("sv_SE.aff", "sv_SE.dic")):
        print("  sv_SE-ordlista finns redan")
    elif sv_aff.is_file():
        spell_dir.mkdir(parents=True, exist_ok=True)
        for src, name in ((sv_aff, "sv_SE.aff"), (sv_dic, "sv_SE.dic")):
            target = spell_dir / name
            target.unlink(missing_ok=True)
            target.symlink_to(src)
        print("  sv_SE-ordlista länkad till ~/Library/Spelling/")
    elif download_sv_dictionary(spell_dir):
        print("  sv_SE-ordlista hämtad från LibreOffice till ~/Library/Spelling/")
    else:
        print("  Ladda ner sv_SE.{aff,dic} manuellt och lägg i ~/Library/Spelling/")

    python = newest_python()
    if python is None:
        print("Python 3.11+ krävs.", file=sys.stderr)
        return 1
    print(f"→ Python: {python}")

    venv = ROOT / ".venv"
    if not venv.exists():
        subprocess.run([python, "-m", "venv", str(venv)], check=True)
    else:
        current, newest = _python_version(str(venv / "bin" / "python")), _python_version(python)
        if current and newest and current < newest:
            print(
                f"  OBS: .venv kör Python {current[0]}.{current[1]} men {newest[0]}.{newest[1]} finns. "
                "Ta bort .venv och kör om ./install.sh för att byta."
            )

    bootstrap = pip_bootstrap_command(venv)
    if bootstrap is not None:
        print("→ .venv saknar pip (skapad av uv?) — installerar pip…")
        subprocess.run(bootstrap, check=True)
    # `python -m pip`: ensurepip skapar bara pip3/pip3.X, inte skriptet `pip`.
    pip = [str(venv / "bin" / "python"), "-m", "pip"]
    subprocess.run([*pip, "install", "--upgrade", "pip", "--quiet"], check=True)
    extras = pip_extras(with_dev)
    subprocess.run([*pip, "install", "--quiet", "-e", f".[{extras}]"], cwd=ROOT, check=True)
    if with_surya:
        subprocess.run([*pip, "install", "--quiet", "-e", ".[surya]"], cwd=ROOT, check=True)

    print("→ sökindex…")
    subprocess.run(ensure_index_command(venv), cwd=ROOT, check=True)

    print("→ tessdata (swe_best)…")
    subprocess.run([str(venv / "bin" / "python"), str(ROOT / "scripts" / "setup_tessdata.py")], check=True)

    print("\n✓ Installation klar!")
    print("Starta webgränssnittet med ./web.sh. Fyll eller uppdatera sökindexet med:")
    print("  ./run_pipeline.sh            # download → OCR → ingest (flera timmar)")
    print("  ./run_pipeline.sh --test 5   # snabb provkörning med 5 dokument")
    hint = missing_llm_key_hint()
    if hint:
        print(f"\n{hint}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
