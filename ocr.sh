#!/bin/bash
# Full OCR-pipeline för palmemordsarkivet.
#
# Default-läge (utan flaggor): Tesseract → kvalitetsbedömning → Surya på dåliga
# sidor → ny kvalitetsbedömning. Anropar ocr_tesseract.sh och quality.sh och
# rekursivt sig själv (./ocr.sh --redo --mode pages).
#
# --redo-läge: kör om OCR på filer/sidor som ligger under en kvalitetströskel.
#   --mode files  — ocrmypdf --redo-ocr på hela filer från quality.csv
#   --mode pages  — Surya på enstaka sidor från quality_pages-tabellen (default)
#
# Användning:
#   ./ocr.sh                                # full pipeline (default)
#   ./ocr.sh --skip-redo                    # bara Tesseract + bedömning
#   ./ocr.sh --redo                         # bara redo-steget (default mode pages)
#   ./ocr.sh --redo --mode files            # om-OCR av hela filer
#   ./ocr.sh --help                         # alla flaggor

set -eu

trap 'trap - INT TERM; echo; echo "Avbruten — avslutar underprocesser..."; kill 0 2>/dev/null; exit 130' INT TERM

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

Default-läge (full pipeline):
  1. ./ocr_tesseract.sh                       # Tesseract på downloaded/files/
  1b./ocr_tesseract.sh --in downloaded/wpu_files # Tesseract på downloaded/wpu_files/ (om finns)
  2. ./merge_wpu.sh                           # jämför wpu vs palme; raderar
                                              # förloraren (om files_wpu/ finns)
  3. ./detect_redactions.sh                   # infoga [MASKAD] i text
  4. ./normalize.sh                           # regelbaserad textnormalisering
  5. ./quality.sh --per-page                  # quality + quality_pages (db)
  6. ./ocr.sh --redo --mode pages             # Surya på sidor under tröskeln
                                              # (palme + kvarvarande wpu)
  7. ./quality.sh                             # uppdaterad quality.csv

Steg 6 hoppas över om --skip-redo eller om Surya inte är installerat.

Flaggor (default visas inom parentes):

  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  --skip-redo             hoppa Surya-steget i full pipeline
  --redo                  hoppa direkt till redo-logiken (steg 3 ovan)
  --mode files|pages      bara med --redo (pages)
                            files = ocrmypdf --redo-ocr på hela filer från
                                    quality.csv
                            pages = Surya på enstaka sidor från
                                    quality_pages-tabellen i db
  --threshold N           score-tröskel för om-OCR (50)
  --source S              text-layer | ocr | any — bara med --redo --mode files (any)
  --jobs N                antal filer parallellt (4)
  --per-file-jobs N       OCR-trådar per fil (2)
  --in DIR                ingångskatalog med PDF:er (\$ROOT/downloaded/files)
  --ocr DIR               output-katalog för OCR-PDF:er (\$ROOT/generated/ocr)
  --txt DIR               output-katalog för .txt (\$ROOT/generated/text)
  --csv FILE              DEPRECATED — quality läses nu från db.quality-tabellen
  --pages-jsonl FILE      DEPRECATED — bad pages läses nu från db.quality_pages
  --pages-out DIR         output-katalog för per-sida (\$ROOT/generated/text_pages)
  --from-list FILE        --redo --mode files: läs filnamn från textfil (en per
                          rad) istället för att filtrera quality.csv. Användbart
                          för att om-OCR:a filer som ingest.py flaggade som
                          'inga användbara chunks' (skrivs till generated/unusable.txt).
  --no-update-pdf         hoppa PDF-textlager-patchen efter Surya per sida
                          (default: textlagret i \$OCR/<stem>.pdf uppdateras)
  --retry-failed          ta bort .ocr-failed-markörer så att misslyckade filer körs om
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
RETRY_FAILED=${RETRY_FAILED:-0}

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
    --retry-failed)    RETRY_FAILED=1; shift ;;
    -h|--help)         usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IN=${IN:-$ROOT/downloaded/files}
OCR=${OCR:-$ROOT/generated/ocr}
TXT=${TXT:-$ROOT/generated/text}
CSV=${CSV:-$ROOT/generated/quality.csv}
PAGES_JSONL=${PAGES_JSONL:-$ROOT/generated/quality_pages.jsonl}
PAGES_OUT=${PAGES_OUT:-$ROOT/generated/text_pages}

if ! printf '%s' "$THRESHOLD" | grep -qE '^[0-9]+(\.[0-9]+)?$'; then
  echo "Ogiltigt --threshold: '$THRESHOLD' (förväntar ett numeriskt värde)" >&2
  exit 2
fi

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

  retry_flag=()
  [ "$RETRY_FAILED" = "1" ] && retry_flag+=(--retry-failed)

  step "1/7  Tesseract-OCR (./ocr_tesseract.sh --jobs $JOBS)"
  ./ocr_tesseract.sh --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS" ${retry_flag[@]+"${retry_flag[@]}"}

  if [ -d "$ROOT/downloaded/wpu_files" ]; then
    # Wpu-filer får samma behandling som palme-filer: tesseract → generated/text/ + generated/ocr/.
    # Sen jämför merge_wpu och raderar förlorarens text/+ocr/-filer.
    step "1b/7  Tesseract-OCR av wpu-PDF:er"
    ./ocr_tesseract.sh --in "$ROOT/downloaded/wpu_files" --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS" ${retry_flag[@]+"${retry_flag[@]}"}

    step "2/7  Sammanfoga wpu.nu-text (./merge_wpu.sh)"
    ./merge_wpu.sh
  fi

  step "3/7  Redaktionsdetektering (./detect_redactions.sh)"
  ./detect_redactions.sh

  step "4/7  Normalisering (./normalize.sh)"
  ./normalize.sh

  step "5/7  Kvalitetsbedömning (./quality.sh --per-page)"
  ./quality.sh --per-page

  if [ "$SKIP_REDO" = "1" ]; then
    echo
    echo "===== Hoppar över steg 6 (--skip-redo) ====="
  elif ! "$ROOT/.venv/bin/python" -c "import surya" 2>/dev/null; then
    echo
    echo "===== Hoppar över steg 6 (Surya inte installerat) ====="
    echo "Installera med:  .venv/bin/pip install surya-ocr 'transformers<5'"
  else
    step "6/7  Om-OCR med Surya på sidor < $THRESHOLD (./ocr.sh --redo --mode pages)"
    redo_extra=()
    [ "$NO_UPDATE_PDF" = "1" ] && redo_extra+=(--no-update-pdf)
    ./ocr.sh --redo --mode pages --threshold "$THRESHOLD" --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS" ${redo_extra[@]+"${redo_extra[@]}"}

    step "7/7  Uppdaterad kvalitetsbedömning"
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
  PYBIN="$ROOT/.venv/bin/python"
  [ -x "$PYBIN" ] || PYBIN="python3"
  n=$("$PYBIN" -c "
import sys; sys.path.insert(0, '$ROOT/src')
import db
conn = db.connect(); db.init_schema(conn)
print(conn.execute('SELECT COUNT(*) FROM quality_pages').fetchone()[0])
" 2>/dev/null || echo 0)
  if [ "${n:-0}" = "0" ]; then
    echo "Inga rader i quality_pages-tabellen — kör './quality.sh --per-page' först."
    exit 1
  fi
elif [ -z "${FROM_LIST:-}" ]; then
  command -v ocrmypdf >/dev/null || { echo "saknar ocrmypdf"; exit 1; }
  command -v pdftotext >/dev/null || { echo "saknar pdftotext"; exit 1; }
fi

if [ "$MODE" = "pages" ]; then
  # Per-sida läge: kör ocr_pages.py --engine surya på dåliga sidor.
  echo "MODE=pages — läser quality_pages från db (THRESHOLD=$THRESHOLD)..."
  PYBIN="$ROOT/.venv/bin/python"
  [ -x "$PYBIN" ] || PYBIN="python3"

  "$PYBIN" - "$THRESHOLD" "$IN" "$PAGES_OUT" "$ROOT" "$OCR" "$NO_UPDATE_PDF" "$TXT" <<'PYEOF'
import subprocess, sys, time
from collections import defaultdict
from pathlib import Path

thr, in_dir, out_dir, root, ocr_dir, no_update, txt_dir = sys.argv[1:8]
thr = float(thr)
in_dir = Path(in_dir); out_dir = Path(out_dir); root = Path(root)
txt_dir = Path(txt_dir)
# wpu-PDF:er ligger i files_wpu/ men text/ kan innehålla deras text (merge_wpu
# valde wpu-versionen). Vid fallback för --redo måste vi leta där också.
wpu_dir = root / "downloaded" / "wpu_files"

# Importera merge_pages direkt så vi slipper subprocess per fil.
sys.path.insert(0, str(root / "src"))
from merge_pages import merge_one  # noqa: E402
from normalize_text import process_file as normalize_file  # noqa: E402
import db as state_db  # noqa: E402

conn = state_db.connect()
state_db.init_schema(conn)

raw = defaultdict(list)
for row in conn.execute(
    "SELECT pdf_stem, page_num FROM quality_pages "
    "WHERE score < ? AND COALESCE(image_page, 0) = 0 "
    "ORDER BY pdf_stem, page_num",
    (thr,),
):
    raw[row["pdf_stem"] + ".txt"].append(int(row["page_num"]))

# Filtrera bort sidor där Surya redan har försökt (enligt pdf_pages-tabellen).
# Annars loopar vi över samma sidor varje körning utan att göra något.
bad = defaultdict(list)
skipped_already = 0
for txt_name, pages in raw.items():
    stem = txt_name[:-4] if txt_name.endswith(".txt") else txt_name
    for p in pages:
        row = conn.execute(
            "SELECT engine FROM pdf_pages WHERE pdf_stem=? AND page_num=?",
            (stem, p),
        ).fetchone()
        if row and row["engine"] == "surya":
            skipped_already += 1
        else:
            bad[txt_name].append(p)

total_bad = sum(len(v) for v in raw.values())
if not bad:
    print(f"Inga nya dåliga sidor (THRESHOLD={thr}, "
          f"{skipped_already}/{total_bad} redan försökta).")
    sys.exit(0)

total_files = len(bad)
total_pages = sum(len(set(v)) for v in bad.values())
print(f"Hittade {total_pages} nya dåliga sidor i "
      f"{total_files} filer ({skipped_already} redan försökta hoppas över).")

def fmt_eta(seconds):
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s"

start_ts = time.monotonic()
done_files = 0
done_pages = 0
for txt_name, pages in bad.items():
    stem = txt_name[:-4] if txt_name.endswith(".txt") else txt_name
    pages_set = sorted(set(pages))
    n_pages = len(pages_set)
    pages_arg = ",".join(str(p) for p in pages_set)

    elapsed = time.monotonic() - start_ts
    if done_pages > 0:
        eta_str = fmt_eta(elapsed / done_pages * (total_pages - done_pages))
    else:
        eta_str = "--"

    pdf = in_dir / f"{stem}.pdf"
    if not pdf.exists():
        wpu_pdf = wpu_dir / f"{stem}.pdf"
        if wpu_pdf.exists():
            pdf = wpu_pdf
        else:
            done_files += 1
            continue
    print(f"[surya {done_files + 1}/{total_files} | {n_pages}s | eta {eta_str}] {stem}")
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
    t0 = time.monotonic()
    r = subprocess.run(cmd, check=False)
    took = time.monotonic() - t0
    done_files += 1
    done_pages += n_pages
    # Per-dokument-merge direkt efter ocr_pages.py är klar: ersätt motsvarande
    # sidor i text/<stem>.txt så att ingest fångar dem via mtime.
    if r.returncode == 0:
        print(f"  → ok ({took:.0f}s)")
        try:
            merge_one(stem, txt_dir)
        except Exception as e:  # noqa: BLE001
            print(f"  [merge_pages] FEL {stem}: {e}", file=sys.stderr)
        txt_file = txt_dir / f"{stem}.txt"
        if txt_file.exists():
            try:
                normalize_file(txt_file)
            except Exception as e:  # noqa: BLE001
                print(f"  [normalize] FEL {stem}: {e}", file=sys.stderr)
            # Stämpla text_mtime i state.db så normalize-stegen ser filen som hanterad.
            try:
                import db as _db
                _conn = _db.connect()
                _db.init_schema(_conn)
                _db.mark_normalized(_conn, stem, text_mtime=txt_file.stat().st_mtime)
                _conn.close()
            except Exception as e:  # noqa: BLE001
                print(f"  [normalize-stamp] FEL {stem}: {e}", file=sys.stderr)
    else:
        print(f"  → FEL ({took:.0f}s)", file=sys.stderr)
PYEOF
  exit 0
fi

# MODE=files: plocka filnamnen ur CSV (eller --from-list).
PYBIN="$ROOT/.venv/bin/python"
[ -x "$PYBIN" ] || PYBIN="python3"

# --from-list: nollställ markörer för de listade filerna och kör hela pipeline:n.
if [ -n "${FROM_LIST:-}" ]; then
  [ -f "$FROM_LIST" ] || { echo "Saknar --from-list-fil: $FROM_LIST"; exit 1; }

  TXT_DIR="${TXT:-$ROOT/generated/text}"
  count=0
  while IFS= read -r line; do
    stem="${line%.txt}"
    [ -n "$stem" ] || continue
    rm -f "$TXT_DIR/$stem.txt" "$TXT_DIR/$stem.redact"
    # Nollställ pipeline-state i state.db så hela kedjan körs om för stem:
    # pdf_pages (Surya) + tesseract-markörerna (annars hoppar ocr_tesseract.sh över).
    # Skicka stem via argv så filnamn med apostrof inte bryter shell-quotingen.
    ROOT="$ROOT" "$PYBIN" - "$stem" <<'PYEOF' 2>/dev/null || true
import sys, os
sys.path.insert(0, os.path.join(os.environ['ROOT'], 'src'))
import db
conn = db.connect()
db.init_schema(conn)
conn.execute('DELETE FROM pdf_pages WHERE pdf_stem=?', (sys.argv[1],))
conn.execute(
    'UPDATE pdf_files SET tesseract_done_at=NULL, tesseract_failed=0, '
    'tesseract_blacklisted_at=NULL WHERE pdf_stem=?',
    (sys.argv[1],),
)
conn.commit()
PYEOF
    count=$((count + 1))
  done < "$FROM_LIST"

  echo "Nollställde $count filer — kör full pipeline..."
  t0=$(date +%s)

  step "1/5  Tesseract-OCR"
  ./ocr_tesseract.sh --files-from "$FROM_LIST" --jobs "$JOBS" --per-file-jobs "$PER_FILE_JOBS"

  step "2/5  Redaktionsdetektering"
  ./detect_redactions.sh --files-from "$FROM_LIST"

  step "3/5  Normalisering"
  ./normalize.sh --files-from "$FROM_LIST"

  step "4/5  Kvalitetsbedömning"
  ./quality.sh --per-page --files-from "$FROM_LIST"

  if ! "$ROOT/.venv/bin/python" -c "import surya" 2>/dev/null; then
    echo
    echo "===== Hoppar över Surya (inte installerat) ====="
  else
    step "5/5  Om-OCR med Surya på sidor < $THRESHOLD"
    redo_extra=()
    [ "$NO_UPDATE_PDF" = "1" ] && redo_extra+=(--no-update-pdf)
    ./ocr.sh --redo --mode pages --threshold "$THRESHOLD" --jobs "$JOBS" \
             --per-file-jobs "$PER_FILE_JOBS" ${redo_extra[@]+"${redo_extra[@]}"}
    ./quality.sh --files-from "$FROM_LIST"
  fi

  t1=$(date +%s)
  echo
  echo "===== Klart på $((t1 - t0))s ====="
  exit 0
fi

# Utan --from-list: plocka filnamnen ur quality-tabellen i db.
TARGETS=()
while IFS= read -r line; do
  [ -n "$line" ] && TARGETS+=("$line")
done < <(
  "$PYBIN" -c "
import sys
sys.path.insert(0, sys.argv[1])
import db as state_db
thr = float(sys.argv[2]); src = sys.argv[3]
conn = state_db.connect(); state_db.init_schema(conn)
if src == 'any':
    rows = conn.execute('SELECT pdf_stem FROM quality WHERE score < ? ORDER BY score', (thr,))
else:
    rows = conn.execute('SELECT pdf_stem FROM quality WHERE score < ? AND source_type = ? ORDER BY score',
                        (thr, src))
for r in rows:
    print(r['pdf_stem'])
" "$ROOT/src" "$THRESHOLD" "$SOURCE"
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

  [ -f "$pdf" ] || return 1

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
    "$ROOT/.venv/bin/python" -c "
import sys; sys.path.insert(0, sys.argv[1])
from normalize_text import process_file
from pathlib import Path
process_file(Path(sys.argv[2]))
" "$ROOT/src" "$out_txt" 2>/dev/null || true
    rm -f "$log"
  else
    echo "[fel] $base — se loggen nedan:" >&2
    sed 's/^/    /' "$log" >&2
    rm -f "$log"
    return 1
  fi
}
export -f redo_one
export IN OCR TXT PER_FILE_JOBS ROOT

# || true: enskilda filfel ska inte stoppa batchen (set -e annars).
printf '%s\0' ${TARGETS[@]+"${TARGETS[@]}"} \
  | xargs -0 -I {} -P "$JOBS" bash -c 'redo_one "$@"' _ {} || true

echo
echo "Klart. Kör './quality.sh' igen för att se förbättringen."
