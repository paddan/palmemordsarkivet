"""Installera alla beroenden (Homebrew, venv, pip, tessdata)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    with_surya = True
    with_dev = False
    for arg in args:
        if arg == "--no-surya":
            with_surya = False
        elif arg == "--dev":
            with_dev = True
        elif arg in ("-h", "--help"):
            print("Användning: .venv/bin/python scripts/install.py [--no-surya] [--dev]")
            return 0
        else:
            print(f"okänd flagga: {arg}", file=sys.stderr)
            return 2

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
