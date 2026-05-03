#!/bin/bash
# Sätter upp en projekt-lokal tessdata-katalog med swe_best.traineddata
# (mer noggrann än default-modellen från tesseract-lang).
#
# Kör en gång:  ./setup_tessdata.sh
# Sen körs ocr.sh automatiskt mot denna katalog.

set -eu

ROOT=${ROOT:-$HOME/projects/palmemordsarkivet}
DEST="$ROOT/tessdata"
SYS_TESSDATA="$(brew --prefix)/share/tessdata"

mkdir -p "$DEST"

# 1. Symlinka allt utom swe.traineddata från systemets tessdata
#    (konfig-filer, eng/osd-modeller, pdf.ttf etc.)
for src in "$SYS_TESSDATA"/* "$SYS_TESSDATA"/.*; do
  base=$(basename "$src")
  case "$base" in
    "."|".."|"swe.traineddata") continue ;;
  esac
  if [ ! -e "$DEST/$base" ]; then
    ln -sfn "$src" "$DEST/$base"
  fi
done
echo "symlänkade systemfiler från $SYS_TESSDATA"

# 2. Ladda ner swe_best.traineddata (~12 MB)
SWE="$DEST/swe.traineddata"
if [ -f "$SWE" ] && [ ! -L "$SWE" ]; then
  echo "swe.traineddata finns redan ($(du -h "$SWE" | cut -f1))"
else
  rm -f "$SWE"
  echo "Hämtar swe_best.traineddata från tessdata_best…"
  curl -fL --progress-bar \
    -o "$SWE" \
    https://github.com/tesseract-ocr/tessdata_best/raw/main/swe.traineddata
  echo "Klart: $(du -h "$SWE" | cut -f1)"
fi

echo
echo "tessdata-katalog: $DEST"
ls -lh "$DEST"
echo
echo "ocr.sh använder denna automatiskt via TESSDATA_PREFIX."
