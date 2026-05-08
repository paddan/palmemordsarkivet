#!/bin/bash
# Kör om OCR på filer med dåligt textlager.
#
# Plockar text-layer-filer ur quality.csv vars score är under tröskeln,
# raderar deras .txt + .pdf i ocr/, och kör ocrmypdf --redo-ocr som
# tar bort det dåliga textlagret och OCR:ar om från originalbilden.
#
# Förkrav:
#   ./quality.sh                 # generera/uppdatera quality.csv

set -u

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Default visas inom parentes. Env-vars (versaler) fungerar som fallback men
flaggor vinner.

  --root DIR              projektrot ($PWD om ej satt via ROOT)
  --in DIR                ingångskatalog med PDF:er (\$ROOT/files)
  --ocr DIR               output-katalog för OCR-PDF:er (\$ROOT/ocr)
  --txt DIR               output-katalog för .txt (\$ROOT/text)
  --csv FILE              quality.csv (\$ROOT/quality.csv)
  --pages-jsonl FILE      quality_pages.jsonl (\$ROOT/quality_pages.jsonl)
  --threshold N           score-tröskel; sidor/filer under detta körs om (50)
  --source S              text-layer | ocr | any (text-layer)
  --jobs N                antal filer parallellt (4)
  --per-file-jobs N       OCR-trådar per fil (2)
  --mode MODE             files | pages (files)
  --pages-out DIR         output-katalog för per-sida (\$ROOT/text_pages)
  -h, --help              visa denna hjälp
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-}
OCR=${OCR:-}
TXT=${TXT:-}
CSV=${CSV:-}
PAGES_JSONL=${PAGES_JSONL:-}
THRESHOLD=${THRESHOLD:-50}
SOURCE=${SOURCE:-text-layer}
JOBS=${JOBS:-4}
PER_FILE_JOBS=${PER_FILE_JOBS:-2}
MODE=${MODE:-files}
PAGES_OUT=${PAGES_OUT:-}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)            ROOT="$2"; shift 2 ;;
    --in)              IN="$2"; shift 2 ;;
    --ocr)             OCR="$2"; shift 2 ;;
    --txt)             TXT="$2"; shift 2 ;;
    --csv)             CSV="$2"; shift 2 ;;
    --pages-jsonl)     PAGES_JSONL="$2"; shift 2 ;;
    --threshold)       THRESHOLD="$2"; shift 2 ;;
    --source)          SOURCE="$2"; shift 2 ;;
    --jobs)            JOBS="$2"; shift 2 ;;
    --per-file-jobs)   PER_FILE_JOBS="$2"; shift 2 ;;
    --mode)            MODE="$2"; shift 2 ;;
    --pages-out)       PAGES_OUT="$2"; shift 2 ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IN=${IN:-$ROOT/files}
OCR=${OCR:-$ROOT/ocr}
TXT=${TXT:-$ROOT/text}
CSV=${CSV:-$ROOT/quality.csv}
PAGES_JSONL=${PAGES_JSONL:-$ROOT/quality_pages.jsonl}
PAGES_OUT=${PAGES_OUT:-$ROOT/text_pages}

if [ "$MODE" = "pages" ]; then
  [ -f "$PAGES_JSONL" ] || { echo "Saknar $PAGES_JSONL — kör './quality.sh --per-page' först."; exit 1; }
else
  [ -f "$CSV" ] || { echo "Saknar $CSV — kör quality.py först."; exit 1; }
  command -v ocrmypdf >/dev/null || { echo "saknar ocrmypdf"; exit 1; }
  command -v pdftotext >/dev/null || { echo "saknar pdftotext"; exit 1; }
fi

if [ "$MODE" = "pages" ]; then
  # Per-sida läge: kör ocr_pages.py --engine surya på dåliga sidor.
  echo "MODE=pages — använder $PAGES_JSONL (THRESHOLD=$THRESHOLD)..."
  PYBIN="$ROOT/.venv/bin/python"
  [ -x "$PYBIN" ] || PYBIN="python3"

  "$PYBIN" - "$PAGES_JSONL" "$THRESHOLD" "$IN" "$PAGES_OUT" "$ROOT" <<'PYEOF'
import json, subprocess, sys
from collections import defaultdict
from pathlib import Path

jsonl, thr, in_dir, out_dir, root = sys.argv[1:6]
thr = float(thr)
in_dir = Path(in_dir); out_dir = Path(out_dir); root = Path(root)

bad = defaultdict(list)
with open(jsonl, encoding="utf-8") as f:
    for line in f:
        try:
            row = json.loads(line)
        except Exception:
            continue
        score = float(row.get("score") or 0.0)
        if score < thr:
            bad[row["file"]].append(int(row["page"]))

if not bad:
    print(f"Inga dåliga sidor (THRESHOLD={thr}).")
    sys.exit(0)

print(f"Hittade {sum(len(v) for v in bad.values())} dåliga sidor i {len(bad)} filer.")
for txt_name, pages in bad.items():
    stem = txt_name[:-4] if txt_name.endswith(".txt") else txt_name
    pdf = in_dir / f"{stem}.pdf"
    if not pdf.exists():
        print(f"  SAKNAS: {pdf}")
        continue
    pages_arg = ",".join(str(p) for p in sorted(set(pages)))
    cmd = [
        sys.executable, str(root / "src" / "ocr_pages.py"),
        "--in", str(pdf),
        "--out-dir", str(out_dir),
        "--engine", "surya",
        "--pages", pages_arg,
    ]
    print(f"[redo-pages] {stem}: sidor {pages_arg}")
    subprocess.run(cmd, check=False)
PYEOF
  exit 0
fi

# Plocka filnamnen ur CSV: kolumn 1 = file, 2 = source, 3 = score.
# Använder while-read istället för mapfile (mapfile saknas i macOS bash 3.2).
TARGETS=()
while IFS= read -r line; do
  [ -n "$line" ] && TARGETS+=("$line")
done < <(
  awk -F, -v t="$THRESHOLD" -v s="$SOURCE" '
    NR == 1 { next }
    {
      file = $1; source = $2; score = $3 + 0
      if (score < t && (s == "any" || source == s)) {
        sub(/\.txt$/, "", file)
        print file
      }
    }
  ' "$CSV"
)

if [ ${#TARGETS[@]} -eq 0 ]; then
  echo "Inga filer matchade (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)."
  exit 0
fi

echo "Kör om OCR på ${#TARGETS[@]} filer (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)..."

redo_one() {
  local base="$1" pdf out_pdf out_txt log
  pdf="$IN/$base.pdf"
  out_pdf="$OCR/$base.pdf"
  out_txt="$TXT/$base.txt"

  [ -f "$pdf" ] || { echo "  SAKNAS: $pdf"; return 1; }

  echo "[redo] $base"
  rm -f "$out_pdf" "$out_txt"
  log=$(mktemp)
  # --redo-ocr är inkompatibel med --deskew/--clean-final/--remove-background
  if ocrmypdf \
        -l swe \
        --redo-ocr \
        --rotate-pages \
        --clean \
        --jobs "$PER_FILE_JOBS" \
        --quiet \
        "$pdf" "$out_pdf" 2>"$log"; then
    pdftotext -layout "$out_pdf" "$out_txt"
    rm -f "$log"
  else
    echo "[fel] $base — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    rm -f "$log"
    return 1
  fi
}
export -f redo_one
export IN OCR TXT PER_FILE_JOBS

printf '%s\0' "${TARGETS[@]}" \
  | xargs -0 -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {}

echo
echo "Klart. Kör './quality.sh' igen för att se förbättringen."
