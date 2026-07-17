#!/bin/bash
# Installera alla beroenden för palmemordsarkivet.
# Idempotent — kan köras om utan problem.
#
# Kör:  ./install.sh
#       ./install.sh --surya    # inkludera Surya-OCR (tyngre, bättre på svåra sidor)
#       ./install.sh --dev      # installera även pytest/ruff/mypy

set -euo pipefail

WITH_SURYA=true
WITH_DEV=false
for arg in "$@"; do
  case "$arg" in
    --no-surya) WITH_SURYA=false ;;
    --dev) WITH_DEV=true ;;
    -h|--help)
      echo "Användning: $(basename "$0") [--no-surya] [--dev]"
      echo "  --no-surya   hoppa över Surya-OCR (snabbare install, sämre OCR på svåra sidor)"
      echo "  --dev        installera pytest, ruff och mypy för utveckling/tester"
      exit 0 ;;
    *) echo "okänd flagga: $arg" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

# ── macOS-krav ────────────────────────────────────────────────────────────────
if [[ "$(uname)" != "Darwin" ]]; then
  echo "VARNING: skriptet är testat på macOS. Fortsätter ändå…"
fi

# ── Homebrew ──────────────────────────────────────────────────────────────────
if ! command -v brew &>/dev/null; then
  echo "Homebrew saknas. Installera från https://brew.sh och kör sedan om."
  exit 1
fi

echo "→ brew-paket (hoppar över redan installerade)…"
brew install --quiet ocrmypdf tesseract-lang poppler unpaper hunspell

# hunspell svensk ordlista
SV_AFF="$(brew --prefix)/share/hunspell/sv_SE.aff"
SPELL_DIR="$HOME/Library/Spelling"
if [[ ! -f "$SPELL_DIR/sv_SE.aff" ]]; then
  if [[ -f "$SV_AFF" ]]; then
    mkdir -p "$SPELL_DIR"
    ln -sfn "$(brew --prefix)/share/hunspell/sv_SE.aff" "$SPELL_DIR/sv_SE.aff"
    ln -sfn "$(brew --prefix)/share/hunspell/sv_SE.dic" "$SPELL_DIR/sv_SE.dic"
    echo "  sv_SE-ordlista länkad till ~/Library/Spelling/"
  else
    echo "  VARNING: sv_SE-ordlista hittades inte under $(brew --prefix)/share/hunspell/"
    echo "  Ladda ner sv_SE.{aff,dic} manuellt och lägg i ~/Library/Spelling/"
  fi
else
  echo "  sv_SE-ordlista finns redan"
fi

# ── Python ────────────────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.13 python3.12 python3.11 python3; do
  if command -v "$cmd" &>/dev/null; then
    ver=$("$cmd" -c 'import sys; print(sys.version_info >= (3,11))')
    if [[ "$ver" == "True" ]]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [[ -z "$PYTHON" ]]; then
  echo "Python 3.11+ krävs. Installera t.ex. via: brew install python@3.13"
  exit 1
fi

echo "→ Python: $($PYTHON --version)"

# ── Virtualenv ────────────────────────────────────────────────────────────────
if [[ ! -d ".venv" ]]; then
  echo "→ skapar .venv…"
  "$PYTHON" -m venv .venv
else
  echo "→ .venv finns redan"
fi

PIP=".venv/bin/pip"
"$PIP" install --upgrade pip --quiet

# ── Python-beroenden ──────────────────────────────────────────────────────────
if [[ "$WITH_DEV" == "true" ]]; then
  echo "→ pip install -e .[dev,web]…"
  "$PIP" install --quiet -e ".[dev,web]"
else
  echo "→ pip install -e .[web]…"
  "$PIP" install --quiet -e ".[web]"
fi

if [[ "$WITH_SURYA" == "true" ]]; then
  echo "→ pip install -e .[surya]…"
  "$PIP" install --quiet -e ".[surya]"
fi

# ── Tesseract-modell ──────────────────────────────────────────────────────────
echo "→ tessdata (swe_best)…"
./setup_tessdata.sh

# ── Klart ─────────────────────────────────────────────────────────────────────
echo ""
echo "✓ Installation klar!"
echo ""
echo "Sätt en av dessa miljövariabler för att kunna ställa frågor:"
echo "  export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...   # Pro/Max-abonnemang"
echo "  export ANTHROPIC_API_KEY=sk-ant-...               # API-credits"
echo ""
echo "Kör sedan:"
echo "  ./download.sh     # ladda ner ~3 700 PDF:er (tar några timmar)"
echo "  ./ocr.sh          # OCR till text (tar flera timmar)"
echo "  ./ingest.sh       # bygg vektorindex"
echo "  ./web.sh          # starta webgränssnittet"
echo ""
echo "Utveckling/tester:"
echo "  ./install.sh --dev"
echo "  ./test.sh"
