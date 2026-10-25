"""Installera alla beroenden (Homebrew, venv, pip, tessdata)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from surya_client import LLAMA_UNRESOLVED  # noqa: E402

# Surya 2-layout kör i en egen venv (Python 3.12) bredvid projektets .venv.
SURYA_LAYOUT_VENV = Path.home() / ".local/share/palmemordsarkivet/surya-venv"
SURYA_LAYOUT_PACKAGE = "surya-ocr==0.22.1"
SURYA_LAYOUT_PYTHON_VERSION = "3.12"
SURYA_LAYOUT_BREW_TOOLS = (("llama-server", "llama.cpp"), ("uv", "uv"))


def _python311() -> str | None:
    for cmd in ("python3.13", "python3.12", "python3.11", "python3"):
        exe = shutil.which(cmd)
        if exe is None:
            continue
        try:
            out = subprocess.check_output([exe, "-c", "import sys; print(sys.version_info >= (3,11))"], text=True)
            if out.strip() == "True":
                return exe
        except Exception:
            continue
    return None


def llama_server_present(env: dict[str, str] | None = None) -> bool:
    """``llama-server`` finns i PATH, eller ``LLAMA_CPP_BINARY`` pekar på en befintlig fil."""
    env = os.environ if env is None else env
    explicit = env.get("LLAMA_CPP_BINARY", "")
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
    subprocess.run(["brew", "install", "--quiet", "ocrmypdf", "tesseract-lang", "poppler", "unpaper", "hunspell"], check=False)

    # Hunspell svensk ordlista: symlänka brew-ordlistan om den saknas.
    # brew-hunspells sv_SE.aff/.dic till ~/Library/Spelling om de inte redan finns.
    brew_prefix = subprocess.check_output(["brew", "--prefix"], text=True).strip()
    sv_aff = Path(brew_prefix) / "share" / "hunspell" / "sv_SE.aff"
    sv_dic = Path(brew_prefix) / "share" / "hunspell" / "sv_SE.dic"
    spell_dir = Path.home() / "Library" / "Spelling"
    if (spell_dir / "sv_SE.aff").is_file():
        print("  sv_SE-ordlista finns redan")
    elif sv_aff.is_file():
        spell_dir.mkdir(parents=True, exist_ok=True)
        for src, name in ((sv_aff, "sv_SE.aff"), (sv_dic, "sv_SE.dic")):
            target = spell_dir / name
            target.unlink(missing_ok=True)
            target.symlink_to(src)
        print("  sv_SE-ordlista länkad till ~/Library/Spelling/")
    else:
        print(f"  VARNING: sv_SE-ordlista hittades inte under {sv_aff.parent}/")
        print("  Ladda ner sv_SE.{aff,dic} manuellt och lägg i ~/Library/Spelling/")

    python = _python311()
    if python is None:
        print("Python 3.11+ krävs.", file=sys.stderr)
        return 1
    print(f"→ Python: {python}")

    venv = ROOT / ".venv"
    if not venv.exists():
        subprocess.run([python, "-m", "venv", str(venv)], check=True)

    pip = venv / "bin" / "pip"
    subprocess.run([str(pip), "install", "--upgrade", "pip", "--quiet"], check=True)
    extras = "dev,web" if with_dev else "web"
    subprocess.run([str(pip), "install", "--quiet", "-e", f".[{extras}]"], cwd=ROOT, check=True)
    if with_surya:
        subprocess.run([str(pip), "install", "--quiet", "-e", ".[surya]"], cwd=ROOT, check=True)

    print("→ tessdata (swe_best)…")
    subprocess.run([str(venv / "bin" / "python"), str(ROOT / "scripts" / "setup_tessdata.py")], check=True)

    print("\n✓ Installation klar!")
    print("Kör sedan:")
    print("  .venv/bin/python scripts/download.py")
    print("  .venv/bin/python scripts/ocr.py")
    print("  .venv/bin/python scripts/ingest.py")
    print("  .venv/bin/python scripts/web.py")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
