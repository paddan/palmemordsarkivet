#!/bin/bash
# Detekterar maskeringsblock i PDF:er och infogar [MASKAD] i OCR-text.
# Renderar sidor vid låg DPI (72) — tillräckligt för stora svarta block.
# Inkrementellt: hoppar över filer som redan markerats i state.db (pdf_files.redaction_checked_at).
# Filer med markör filtreras bort innan xargs — inga onödiga subprocesser.
#
# Användning:
#   ./detect_redactions.sh               # alla filer
#   ./detect_redactions.sh --rebuild     # nollställ redaction-flaggor i state.db

set -eu

usage() {
  cat <<EOF
Användning: $(basename "$0") [flaggor]

  --root DIR              projektrot (\$PWD om ej satt via ROOT)
  --in DIR                PDF-katalog (\$ROOT/downloaded/files)
  --txt DIR               text-katalog (\$ROOT/generated/text)
  --jobs N                parallella processer (4)
  --dpi N                 render-DPI (72)
  --rebuild               kör om alla filer (nollställ redaction-flaggor i state.db)
  --rebuild-text          regenerera .txt från generated/ocr/*.pdf innan körning,
                          tar även bort .redact-markörer (innebär --rebuild).
                          Kör ./normalize.sh efteråt för att normalisera om.
  --files-from FILE       bearbeta bara filer listade i FILE (ett filnamn per rad,
                          .txt-suffix trimmas). Används av ocr.sh --from-list.
  -h, --help              visa denna hjälp
EOF
}

ROOT=${ROOT:-$(cd "$(dirname "$0")" && pwd)}
IN=${IN:-}
TXT=${TXT:-}
JOBS=${JOBS:-4}
DPI=${DPI:-72}
REBUILD=0
REBUILD_TEXT=0
FILES_FROM=${FILES_FROM:-}

while [ $# -gt 0 ]; do
  case "$1" in
    --root)          ROOT="$2"; shift 2 ;;
    --in)            IN="$2"; shift 2 ;;
    --txt)           TXT="$2"; shift 2 ;;
    --jobs)          JOBS="$2"; shift 2 ;;
    --dpi)           DPI="$2"; shift 2 ;;
    --rebuild)       REBUILD=1; shift ;;
    --rebuild-text)  REBUILD_TEXT=1; REBUILD=1; shift ;;
    --files-from)    FILES_FROM="$2"; shift 2 ;;
    -h|--help)       usage; exit 0 ;;
    *) echo "okänd flagga: $1" >&2; usage >&2; exit 2 ;;
  esac
done

IN=${IN:-$ROOT/downloaded/files}
TXT=${TXT:-$ROOT/generated/text}

cd "$ROOT"
source .venv/bin/activate

PYBIN="$ROOT/.venv/bin/python"
WPU_DIR="$ROOT/downloaded/wpu_files"

pending_stems() {
  # Filer som ännu inte redaktionskontrollerats (redaction_checked_at IS NULL).
  # Filtreras valfritt mot FILES_FROM om angett.
  local files_from="${1:-}"
  "$ROOT/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$ROOT/src')
import db
conn = db.connect(); db.init_schema(conn)
files_from = sys.argv[1] if len(sys.argv) > 1 else ''
if files_from:
    stems = set()
    with open(files_from) as fh:
        for line in fh:
            s = line.strip().removesuffix('.txt')
            if s:
                stems.add(s)
    rows = conn.execute(
        'SELECT pdf_stem FROM pdf_files WHERE redaction_checked_at IS NULL'
    )
    for r in rows:
        if r['pdf_stem'] in stems:
            print(r['pdf_stem'])
else:
    for r in conn.execute(
        'SELECT pdf_stem FROM pdf_files WHERE redaction_checked_at IS NULL'
    ):
        print(r['pdf_stem'])
" 2>/dev/null "$files_from"
}

total_in_db() {
  "$ROOT/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$ROOT/src')
import db
conn = db.connect(); db.init_schema(conn)
print(conn.execute('SELECT COUNT(*) FROM pdf_files').fetchone()[0])
" 2>/dev/null
}

if [ "$REBUILD_TEXT" = "1" ]; then
  OCR_DIR="$ROOT/generated/ocr"
  echo "Regenererar .txt-filer från $OCR_DIR/*.pdf ..."
  count=0
  errors=0
  while IFS= read -r -d '' pdf; do
    stem=$(basename "$pdf" .pdf)
    if pdftotext -layout "$pdf" "$TXT/$stem.txt" 2>/dev/null; then
      count=$((count + 1))
    else
      echo "  [fel] pdftotext: $stem" >&2
      errors=$((errors + 1))
    fi
  done < <(find "$OCR_DIR" -name '*.pdf' -print0)
  echo "  $count .txt-filer regenererade${errors:+, $errors fel}."
fi

if [ "$REBUILD" = "1" ]; then
  echo "Nollställer redaction-flaggor i state.db..."
  "$ROOT/.venv/bin/python" -c "
import sys; sys.path.insert(0, '$ROOT/src')
import db
conn = db.connect(); db.init_schema(conn)
conn.execute('UPDATE pdf_files SET redaction_checked_at=NULL, has_redactions=NULL')
conn.commit()
"
  # Rensa även gamla .redact-markörfiler (kvar från tidigare versioner)
  find "$TXT" -name '*.redact' -delete 2>/dev/null || true
fi

# Hämta väntande och totalt antal direkt från state.db — en enda query.
ALL_TOTAL=$(total_in_db)

PENDING=()
while IFS= read -r stem; do
  [ -n "$stem" ] || continue
  txt_file="$TXT/$stem.txt"
  [ -f "$txt_file" ] || continue
  PENDING+=("$txt_file")
done < <(pending_stems "$FILES_FROM")

SKIPPED=$(( ALL_TOTAL - ${#PENDING[@]} ))
TOTAL=${#PENDING[@]}

if [ "$TOTAL" -eq 0 ]; then
  echo "Redaktionsdetektering klar — $SKIPPED/$ALL_TOTAL filer redan kontrollerade."
  exit 0
fi

if [ "$SKIPPED" -gt 0 ]; then
  skip_note=" ($SKIPPED redan klara hoppas över)"
else
  skip_note=""
fi
echo "Kontrollerar maskeringar i $TOTAL filer${skip_note} (DPI=$DPI, jobs=$JOBS)..."

PROGRESS_DIR=$(mktemp -d)
START_TS=$(date +%s)
export TOTAL PROGRESS_DIR START_TS TXT DPI PYBIN IN WPU_DIR ROOT

printf '%s\0' "${PENDING[@]}" \
  | xargs -0 -n 1 -P "$JOBS" bash -c '
    stem=$(basename "$1" .txt)

    pdf="$IN/${stem}.pdf"
    if [ ! -f "$pdf" ] && [ -d "$WPU_DIR" ]; then
      pdf="$WPU_DIR/${stem}.pdf"
    fi
    if [ ! -f "$pdf" ]; then
      result="ingen pdf"
    else
      result=$("$PYBIN" "$ROOT/src/ocr_pages.py" \
        --engine detect-only \
        --in "$pdf" \
        --txt-dir "$TXT" \
        --ocr-dir "$ROOT/generated/ocr" \
        --dpi "$DPI" 2>/dev/null || echo "fel")
    fi

    touch "$PROGRESS_DIR/${stem}.done"
    count=$(find "$PROGRESS_DIR" -name "*.done" | wc -l | tr -d " ")
    now=$(date +%s)
    elapsed=$((now - START_TS))
    if [ "$elapsed" -gt 0 ] && [ "$count" -gt 0 ]; then
      remaining=$((TOTAL - count))
      eta_s=$(( remaining * elapsed / count ))
      printf "[%s %d/%d eta %dm%02ds] %s\n" "$result" "$count" "$TOTAL" "$((eta_s/60))" "$((eta_s%60))" "$stem"
    else
      printf "[%s %d/%d] %s\n" "$result" "$count" "$TOTAL" "$stem"
    fi
  ' _

rm -rf "$PROGRESS_DIR"
echo "Klart."
if [ "$REBUILD_TEXT" = "1" ]; then
  echo "Kör nu './normalize.sh' för att normalisera om de regenererade textfilerna."
fi
