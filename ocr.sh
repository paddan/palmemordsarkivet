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

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Default-läge (full pipeline):
  1. ./ocr_tesseract.sh                       # Tesseract på alla nya filer
  2. ./quality.sh --per-page                  # quality.csv + quality_pages.jsonl
  3. ./ocr.sh --redo --mode pages             # Surya på sidor under tröskeln
  4. ./quality.sh                             # uppdaterad quality.csv
  5. ./merge_wpu.sh                           # wpu.nu-text vs palme, bäst vinner
                                              # (körs bara om files_wpu/ finns)

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
  --from-list FILE        --redo --mode files: läs filnamn från textfil (en per
                          rad) istället för att filtrera quality.csv. Användbart
                          för att om-OCR:a filer som ingest.py flaggade som
                          'inga användbara chunks' (skrivs till unusable.txt).
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
FROM_LIST=${FROM_LIST:-}

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
    --from-list)       FROM_LIST="$2"; shift 2 ;;
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

  if [ -d "$ROOT/files_wpu" ]; then
    step "5/5  Sammanfoga wpu.nu-text (./merge_wpu.sh)"
    ./merge_wpu.sh
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

  "$PYBIN" - "$PAGES_JSONL" "$THRESHOLD" "$IN" "$PAGES_OUT" "$ROOT" "$OCR" "$NO_UPDATE_PDF" "$TXT" <<'PYEOF'
import json, subprocess, sys
from collections import defaultdict
from pathlib import Path

jsonl, thr, in_dir, out_dir, root, ocr_dir, no_update, txt_dir = sys.argv[1:9]
thr = float(thr)
in_dir = Path(in_dir); out_dir = Path(out_dir); root = Path(root)
txt_dir = Path(txt_dir)

# Importera merge_pages direkt så vi slipper subprocess per fil.
sys.path.insert(0, str(root / "src"))
from merge_pages import merge_one  # noqa: E402

raw = defaultdict(list)
with open(jsonl, encoding="utf-8") as f:
    for line in f:
        try:
            row = json.loads(line)
        except Exception:
            continue
        score = float(row.get("score") or 0.0)
        if score < thr:
            raw[row["file"]].append(int(row["page"]))

# Filtrera bort sidor som redan har en .json-markör (= Surya har försökt
# tidigare, även om resultatet var lika dåligt). Annars loopar vi över samma
# sidor varje körning utan att göra något.
bad = defaultdict(list)
skipped_already = 0
for txt_name, pages in raw.items():
    stem = txt_name[:-4] if txt_name.endswith(".txt") else txt_name
    stem_dir = out_dir / stem
    for p in pages:
        if (stem_dir / f"page-{p:03d}.json").exists():
            skipped_already += 1
        else:
            bad[txt_name].append(p)

total_bad = sum(len(v) for v in raw.values())
if not bad:
    print(f"Inga nya dåliga sidor (THRESHOLD={thr}, "
          f"{skipped_already}/{total_bad} redan försökta).")
    sys.exit(0)

print(f"Hittade {sum(len(v) for v in bad.values())} nya dåliga sidor i "
      f"{len(bad)} filer ({skipped_already} redan försökta hoppas över).")
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
    r = subprocess.run(cmd, check=False)
    # Per-dokument-merge direkt efter ocr_pages.py är klar: ersätt motsvarande
    # sidor i text/<stem>.txt så att ingest fångar dem via mtime.
    if r.returncode == 0:
        try:
            merge_one(stem, txt_dir, out_dir)
        except Exception as e:  # noqa: BLE001
            print(f"  [merge_pages] FEL {stem}: {e}", file=sys.stderr)
PYEOF
  exit 0
fi

# MODE=files: plocka filnamnen ur CSV (eller --from-list).
PYBIN="$ROOT/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

TARGETS=()
if [ -n "${FROM_LIST:-}" ]; then
  [ -f "$FROM_LIST" ] || { echo "Saknar --from-list-fil: $FROM_LIST"; exit 1; }
  while IFS= read -r line; do
    line="${line%.txt}"
    [ -n "$line" ] && TARGETS+=("$line")
  done < "$FROM_LIST"
else
  # Plocka filnamnen ur CSV via python — awk förstår inte CSV-kvotering
  # (filnamn kan innehålla komman och blir då dubbelkvoterade).
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
fi

if [ ${#TARGETS[@]} -eq 0 ]; then
  if [ -n "${FROM_LIST:-}" ]; then
    echo "Inga filer i $FROM_LIST."
  else
    echo "Inga filer matchade (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)."
  fi
  exit 0
fi

if [ -n "${FROM_LIST:-}" ]; then
  echo "Kör om OCR på ${#TARGETS[@]} filer från $FROM_LIST..."
else
  echo "Kör om OCR på ${#TARGETS[@]} filer (THRESHOLD=$THRESHOLD, SOURCE=$SOURCE)..."
fi

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

# || true: enskilda filfel ska inte stoppa batchen (set -e annars).
printf '%s\0' ${TARGETS[@]+"${TARGETS[@]}"} \
  | xargs -0 -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {} || true

echo
echo "Klart. Kör './quality.sh' igen för att se förbättringen."
