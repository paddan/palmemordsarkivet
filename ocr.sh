#!/bin/bash
# Full OCR-pipeline för palmemordsarkivet.
#
# Default-läge (utan flaggor): Tesseract → kvalitetsbedömning → Surya på dåliga
# sidor → ny kvalitetsbedömning. Anropar ocr_tesseract.sh och quality.sh och
# rekursivt sig själv (./ocr.sh --redo --mode pages).
#
# --redo-läge: kör om OCR på filer/sidor som ligger under en kvalitetströskel.
#   --mode files  — ocrmypdf --redo-ocr på hela filer från quality.csv
#   --mode pages  — Surya på enstaka sidor från quality_pages.jsonl (default)
#
# Användning:
#   ./ocr.sh                                # full pipeline (default)
#   ./ocr.sh --skip-redo                    # bara Tesseract + bedömning
#   ./ocr.sh --redo                         # bara redo-steget (default mode pages)
#   ./ocr.sh --redo --mode files            # om-OCR av hela filer
#   ./ocr.sh --help                         # alla flaggor

set -u

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Default-läge (full pipeline):
  1. ./ocr_tesseract.sh                       # Tesseract på alla nya filer
  2. ./quality.sh --per-page                  # quality.csv + quality_pages.jsonl
  3. ./ocr.sh --redo --mode pages             # Surya på sidor under tröskeln
  4. ./quality.sh                             # uppdaterad quality.csv

Steg 3 hoppas över om --skip-redo eller om Surya inte är installerat.

Flaggor (default visas inom parentes):

  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  --skip-redo             hoppa Surya-steget i full pipeline
  --redo                  hoppa direkt till redo-logiken (steg 3 ovan)
  --mode files|pages      bara med --redo (pages)
                            files = ocrmypdf --redo-ocr på hela filer från
                                    quality.csv
                            pages = Surya på enstaka sidor från
                                    quality_pages.jsonl
  --threshold N           score-tröskel för om-OCR (50)
  --source S              text-layer | ocr | any — bara med --redo --mode files (any)
  --jobs N                antal filer parallellt (4)
  --per-file-jobs N       OCR-trådar per fil (2)
  --in DIR                ingångskatalog med PDF:er (\$ROOT/files)
  --ocr DIR               output-katalog för OCR-PDF:er (\$ROOT/ocr)
  --txt DIR               output-katalog för .txt (\$ROOT/text)
  --csv FILE              quality.csv (\$ROOT/quality.csv)
  --pages-jsonl FILE      quality_pages.jsonl (\$ROOT/quality_pages.jsonl)
  --pages-out DIR         output-katalog för per-sida (\$ROOT/text_pages)
  --no-update-pdf         hoppa PDF-textlager-patchen efter Surya per sida
                          (default: textlagret i \$OCR/<stem>.pdf uppdateras)
  -h, --help              visa denna hjälp
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
THRESHOLD=${THRESHOLD:-50}
SKIP_REDO=0
REDO_ONLY=0
NO_UPDATE_PDF=0
MODE=${MODE:-pages}
SOURCE=${SOURCE:-any}
JOBS=${JOBS:-4}
PER_FILE_JOBS=${PER_FILE_JOBS:-2}
IN=${IN:-}
OCR=${OCR:-}
TXT=${TXT:-}
CSV=${CSV:-}
PAGES_JSONL=${PAGES_JSONL:-}
PAGES_OUT=${PAGES_OUT:-}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)            ROOT="$2"; shift 2 ;;
    --threshold)       THRESHOLD="$2"; shift 2 ;;
    --skip-redo)       SKIP_REDO=1; shift ;;
    --redo)            REDO_ONLY=1; shift ;;
    --no-update-pdf)   NO_UPDATE_PDF=1; shift ;;
    --mode)            MODE="$2"; shift 2 ;;
    --source)          SOURCE="$2"; shift 2 ;;
    --jobs)            JOBS="$2"; shift 2 ;;
    --per-file-jobs)   PER_FILE_JOBS="$2"; shift 2 ;;
    --in)              IN="$2"; shift 2 ;;
    --ocr)             OCR="$2"; shift 2 ;;
    --txt)             TXT="$2"; shift 2 ;;
    --csv)             CSV="$2"; shift 2 ;;
    --pages-jsonl)     PAGES_JSONL="$2"; shift 2 ;;
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

cd "$ROOT"

step() {
  echo
  echo "===== $1 ====="
}

# --------------------------------------------------------------------
# Default-läge: full pipeline
# --------------------------------------------------------------------
if [ "$REDO_ONLY" = "0" ]; then
  set -e
  t0=$(date +%s)

  step "1/4  Tesseract-OCR (./ocr_tesseract.sh --jobs $JOBS)"
  ./ocr_tesseract.sh --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS"

  step "2/4  Kvalitetsbedömning (./quality.sh --per-page)"
  ./quality.sh --per-page

  if [ "$SKIP_REDO" = "1" ]; then
    echo
    echo "===== Hoppar över steg 3 (--skip-redo) ====="
  elif ! "$ROOT/.venv/bin/python" -c "import surya" 2>/dev/null; then
    echo
    echo "===== Hoppar över steg 3 (Surya inte installerat) ====="
    echo "Installera med:  .venv/bin/pip install surya-ocr 'transformers<5'"
  else
    step "3/4  Om-OCR med Surya på sidor < $THRESHOLD (./ocr.sh --redo --mode pages)"
    redo_extra=()
    [ "$NO_UPDATE_PDF" = "1" ] && redo_extra+=(--no-update-pdf)
    ./ocr.sh --redo --mode pages --threshold "$THRESHOLD" --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS" ${redo_extra[@]+"${redo_extra[@]}"}

    step "4/4  Uppdaterad kvalitetsbedömning"
    ./quality.sh
  fi

  t1=$(date +%s)
  elapsed=$((t1 - t0))
  echo
  echo "===== Klart på ${elapsed}s ====="
  exit 0
fi

# --------------------------------------------------------------------
# --redo-läge: kör om OCR på filer/sidor under tröskeln
# --------------------------------------------------------------------
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

  "$PYBIN" - "$PAGES_JSONL" "$THRESHOLD" "$IN" "$PAGES_OUT" "$ROOT" "$OCR" "$NO_UPDATE_PDF" <<'PYEOF'
import json, subprocess, sys
from collections import defaultdict
from pathlib import Path

jsonl, thr, in_dir, out_dir, root, ocr_dir, no_update = sys.argv[1:8]
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
        "--ocr-dir", ocr_dir,
    ]
    if no_update == "1":
        cmd.append("--no-update-pdf")
    print(f"[redo-pages] {stem}: sidor {pages_arg}")
    subprocess.run(cmd, check=False)
PYEOF
  exit 0
fi

# MODE=files: plocka filnamnen ur CSV via python — awk förstår inte CSV-kvotering
# (filnamn kan innehålla komman och blir då dubbelkvoterade).
PYBIN="$ROOT/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

TARGETS=()
while IFS= read -r line; do
  [ -n "$line" ] && TARGETS+=("$line")
done < <(
  "$PYBIN" - "$CSV" "$THRESHOLD" "$SOURCE" <<'PYEOF'
import csv, sys
csv_path, thr, src = sys.argv[1], float(sys.argv[2]), sys.argv[3]
with open(csv_path, encoding="utf-8", newline="") as f:
    for row in csv.DictReader(f):
        try:
            score = float(row.get("score") or 0)
        except ValueError:
            continue
        if score >= thr:
            continue
        if src != "any" and row.get("source") != src:
            continue
        name = row["file"]
        if name.endswith(".txt"):
            name = name[:-4]
        print(name)
PYEOF
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

printf '%s\0' ${TARGETS[@]+"${TARGETS[@]}"} \
  | xargs -0 -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {}

echo
echo "Klart. Kör './quality.sh' igen för att se förbättringen."
