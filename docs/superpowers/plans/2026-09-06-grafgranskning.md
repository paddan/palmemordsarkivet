# Återkommande grafgranskning — implementationsplan

**Mål:** Körbar kvalitetskontroll, källbaserad granskning av noder och relationer, bestående beslut och explicit transaktionell uppdatering av Neo4j.

**Arkitektur:** Original i doc_entities bevaras. Sidans payload-fingerprint gör att beslut blir inaktuella vid ny extraktion. Separata SQLite-tabeller lagrar beslut, LLM-förslag, historik och kontrollrapporter. Ren granskningslogik producerar inventering och korrigerade import-rader. Graf-sidan visar kontroller och granskning även utan Neo4j. Gemensamma registry-operationer erbjuder kontroll, valfri LLM-granskning och uppdatering i Admin/CLI.

**Avgränsning:** Regelbaserad kontroll kompletteras av valfri LLM-granskning. LLM:en lämnar endast källbundna förslag; inga förslag blir beslut eller grafändringar utan manuell acceptans. Återkommande betyder att kontrollen kan köras igen när material eller beslut ändrats. Ingen schemaläggning aktiveras. Befintliga neo4j.sh-ändringar bevaras. Ingen commit/push eller ändring av den levande grafen under utveckling.

## 1. Persistens (subagent)
- Filer: src/db.py, tests/test_graph_review_db.py.
- Migrera schema 7 → 8 med separata beslut, append-only historik och körningar.
- API: list_graph_review_decisions(conn) → list[dict]; save_graph_review_decision(conn, *, item_key, source_hash, action, target, note) → None; record_graph_review_run(conn, *, report) → int; list_graph_review_runs(conn, limit=10) → list[dict].
- Beslutets target är dict. action är keep/exclude/replace/reset. Kräver motivering; reset återställer original och journalförs.
- Verifiera migration, upsert/historik, JSON-roundtrip och rollback i isolerade databaser.
- Lagra LLM-förslag separat med profil, modell, källbelägg och status pending/accepted/rejected.

## 2. Ren granskning och projektion (subagent)
- Filer: src/graph/review.py, tests/test_graph_review.py.
- API: audit_entries(entries, decisions) → list[dict]; build_reviewed_rows(entries, decisions) → (mentions, relations).
- Inventera varje entitet/relation med item_key, source_hash, kind (entity/relation), pdf_stem, page_num, original, issues, decision, stale.
- Stabil item_key av dokument/sida/kind/index; source_hash av hela sid-payloaden. Beslut matchar båda.
- replace-target: entity {namn, typ}; relation {fran, typ, till}. Utesluten entitet tar med sina relationer; namnbyte flyttar relationens ändpunkt. Dubblerade namn med olika etiketter får inte godtyckliga relationer.
- Flagga korta/ensamma personnamn, maskering/OCR-skräp, saknade/tvetydiga relationsändpunkter, självkanter och tom relationstyp. Initialer är granskningsförslag, aldrig automatisk radering.
- Testa J/MJ/BeBe, dokumentisolering, stale-beslut, relationer efter namnbyte/borttagning och oförändrad originalpayload.

## 3. Operationer, import och UI (huvudagent)
- Filer: src/graph/review_service.py, src/graph/review_ui.py, src/pages/3_Graf.py, src/graph/load_neo4j.py, src/operations/registry.py, src/operations/adapters.py, scripts/graph_review.py, scripts/graph_sync.py.
- Kontroll sparar antal, fynd och fingerprint; UI erbjuder filtrering, full sidtext och PDF med sida, beslut med motivering och återställning.
- graph-review kör återkommande kontroll. graph-sync är förhandsvisning som standard; --apply ersätter importerade dokumentkanter och projektets RELATERAR i en explicit Neo4j-transaktion, och rensar endast isolerade entiteter som berörts av projektgrafen. Avbrytning/fel före commit ger rollback.
- Vanlig load-graph använder samma korrigerade projektion; graph-sync behövs för att ta bort äldre rader.
- All SQLite-SQL i db.py. UI startar operationsjobb, så aktiv-slot/cancel/logg återanvänds.
- Tester: verifiera default dry-run utan Neo4j-skrivning, stale snapshot, transaktionsrollback, CLI/registry-paritet och Streamlit-granskningsflöde.

## 4. Integration och granskning
- Lägg till `graph-review-llm` i registry/CLI och Graf-vyn. Använd vald namngiven
  LLM-profil. Skicka sidtext och flaggade poster per sida; kräv att modellens
  citerade belägg finns i sidtexten. Avvisa okända postnycklar och ogiltiga
  ersättningar. Förslag visas, accepteras eller avvisas manuellt.
- Uppdatera docs/teknisk-referens.md och AGENTS.md.
- Kör riktade tester och scripts/test.py --static. Oberoende subagent granskar diffens kritiska beteenden.
- Journalför endast faktiskt slutresultat i dagens Obsidian-journal.
