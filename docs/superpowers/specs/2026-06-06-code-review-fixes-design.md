# Design: korrigeringar efter projektgranskning

## Mål

Åtgärda samtliga verifierade fynd från kodgranskningen utan att ändra
pipeline-arkitekturen eller införa nya externa beroenden.

## Ingest och state-återställning

LanceDB:s befintliga `source`-värden används som sekundär sanning när
`state.db.ingest` saknas eller är ofullständig. En textfil vars source redan
finns i LanceDB behandlas som re-indexering, så gamla chunks raderas innan nya
läggs till. Om en re-indexerad fil inte längre ger användbara chunks raderas de
gamla chunkarna och SQLite-state registrerar noll chunks med aktuell mtime.

Detta gör återställning efter förlorad SQLite-state säker och förhindrar att
inaktuellt innehåll fortsätter vara sökbart.

## Pipeline-återupptagning

`run_pipeline.sh` ska alltid köra de idempotenta OCR- och ingest-stegen, även
när nedladdningssteget inte hittade nya PDF-filer. Nedladdningsräkningen behålls
endast som statusinformation.

## Tesseract-blacklist

`--retry-blacklist` återaktiverar blacklistade filer fullständigt genom att
nollställa både `tesseract_blacklisted_at` och `tesseract_failed` för exakt de
berörda raderna. `--retry-failed` fortsätter att inte påverka blacklisten.

## PDF-textlager

Surya-patchning av ett PDF-textlager är allt-eller-inget per dokument. Om någon
textrad inte kan infogas efter storleksanpassning kastas ett fel innan
temporärfilen ersätter originalet. Original-PDF:en förblir då oförändrad.

## WPU-beslut

Fantom-cleanup får bara radera beslut för WPU-filer vars text saknas men som
inte har ett uttryckligt avslutat Tesseract-resultat. En avsiktligt borttagen
förlorare har kvar `tesseract_done_at` och dess beslut ska därför behållas.

## Dokumentation och tester

README och AGENTS uppdateras så att de beskriver faktisk state- och
återupptagningslogik. Regressionstester täcker varje korrigerat beteende.
