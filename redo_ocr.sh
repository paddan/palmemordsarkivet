#!/bin/bash
# Kör om OCR på filer med dåligt textlager.
#
# Plockar text-layer-filer ur quality.csv vars score är under tröskeln,
# raderar deras .txt + .pdf i ocr/, och kör ocrmypdf --redo-ocr som
# tar bort det dåliga textlagret och OCR:ar om från originalbilden.
#
# Kör:
#   python quality.py            # generera/uppdatera quality.csv
#   ./redo_ocr.sh                # tröskel 50, bara source=text-layer
#   THRESHOLD=70 ./redo_ocr.sh   # mer aggressiv
#   SOURCE=ocr ./redo_ocr.sh     # kör om dåliga Tesseract-filer också

set -u

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-$ROOT/files}
OCR=${OCR:-$ROOT/ocr}
TXT=${TXT:-$ROOT/text}
CSV=${CSV:-$ROOT/quality.csv}
PAGES_JSONL=${PAGES_JSONL:-$ROOT/quality_pages.jsonl}
THRESHOLD=${THRESHOLD:-50}
SOURCE=${SOURCE:-text-layer}      # text-layer | ocr | any
JOBS=${JOBS:-4}
PER_FILE_JOBS=${PER_FILE_JOBS:-2}
MODE=${MODE:-files}               # files | pages
PAGES_OUT=${PAGES_OUT:-$ROOT/text_pages}

if [ "$MODE" = "pages" ]; then
  [ -f "$PAGES_JSONL" ] || { echo "Saknar $PAGES_JSONL — kör 'python quality.py --per-page' först."; exit 1; }
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
        sys.executable, str(root / "ocr_pages.py"),
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

# Plocka filnamnen ur CSV: kolumn 1 = file, 2 = source, 3 = score
mapfile -t TARGETS < <(
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
  if ocrmypdf \
        -l swe \
        --redo-ocr \
        --rotate-pages \
        --deskew \
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

printf '%s\n' "${TARGETS[@]}" \
  | xargs -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {}

echo
echo "Klart. Kör 'python quality.py' igen för att se förbättringen."
