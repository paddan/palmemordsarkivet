#!/usr/bin/env bash
# Tar bort gamla state-filer efter verifierad migrering till state.db.
#
# Kör detta först när du har verifierat att hela pipelinen fungerar mot state.db
# i några körningar. Skriptet är destruktivt — backup state.db först om du är
# osäker.
#
# Vad det rensar:
#   downloaded/files/manifest.csv
#   downloaded/wpu_files/manifest.csv
#   generated/text/.normalize_stamp
#   generated/.quality_stamp
#   generated/text/*.redact
#   generated/text_pages/<stem>/page-*.json
#   generated/text_pages/<stem>/ (tomma katalogen)
#   generated/quality.csv
#   generated/quality_pages.jsonl
#   generated/text_wpu/<stem>.done

set -euo pipefail
cd "$(dirname "$0")"

read -r -p "Radera gamla state-filer (manifest, stamps, .redact, page-*.json, quality.csv/jsonl)? [y/N] " yn
[[ "$yn" =~ ^[Yy]$ ]] || { echo "Avbruten."; exit 0; }

removed=0
remove_if_exists() {
  if [ -e "$1" ]; then
    rm -f "$1"
    removed=$((removed + 1))
  fi
}

remove_if_exists downloaded/files/manifest.csv
remove_if_exists downloaded/wpu_files/manifest.csv
remove_if_exists generated/text/.normalize_stamp
remove_if_exists generated/.quality_stamp
remove_if_exists generated/quality.csv
remove_if_exists generated/quality_pages.jsonl

redact_n=$(find generated/text -maxdepth 1 -name '*.redact' 2>/dev/null | wc -l | tr -d ' ')
json_n=$(find generated/text_pages -name 'page-*.json' 2>/dev/null | wc -l | tr -d ' ')

if [ "$redact_n" -gt 0 ]; then
  find generated/text -maxdepth 1 -name '*.redact' -delete 2>/dev/null || true
  echo "  - rensade $redact_n .redact-markörer"
fi
if [ "$json_n" -gt 0 ]; then
  find generated/text_pages -name 'page-*.json' -delete 2>/dev/null || true
  echo "  - rensade $json_n page-*.json-markörer"
fi

# Efter rensning av page-*.json: rmdir tomma stem-mappar i text_pages/
emptied=0
while IFS= read -r -d '' d; do
  rmdir "$d" 2>/dev/null && emptied=$((emptied + 1)) || true
done < <(find generated/text_pages -mindepth 1 -maxdepth 1 -type d -empty -print0 2>/dev/null)
if [ "$emptied" -gt 0 ]; then
  echo "  - rensade $emptied tomma text_pages-mappar"
fi
# rmdir parent om den nu är tom
rmdir generated/text_pages 2>/dev/null || true

# Ta även bort text_wpu/.done-filer (migrerade till wpu_decisions-tabellen)
wpu_n=$(find generated/text_wpu -maxdepth 1 -name '*.done' 2>/dev/null | wc -l | tr -d ' ')
if [ "$wpu_n" -gt 0 ]; then
  find generated/text_wpu -maxdepth 1 -name '*.done' -delete 2>/dev/null || true
  echo "  - rensade $wpu_n text_wpu/.done-markörer"
fi
rmdir generated/text_wpu 2>/dev/null || true

echo "Klart — $((removed + (redact_n > 0 ? 1 : 0) + (json_n > 0 ? 1 : 0) + (wpu_n > 0 ? 1 : 0))) artefakttyper rensade."
echo "state.db är nu den enda källan till sanning."
