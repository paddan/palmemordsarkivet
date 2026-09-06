"""Återkommande kvalitetskontroll och transaktionell grafuppdatering."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from contextlib import closing, contextmanager, nullcontext
from typing import Any, cast

import db
from graph.load_neo4j import CONSTRAINTS, GRAPH_OWNER, write_mentions, write_relations
from operations.context import ensure_terminal_context
from operations.exceptions import OperationFailed

# Samma ägarskap som importören: dokumentomnämnanden och källförsedda relationer.
_MANAGED = """MATCH (a)-[r]->(b)
WHERE (r.graph_owner = $owner OR ($adopt_legacy AND r.graph_owner IS NULL)) AND
      ((type(r) = 'NÄMNER' AND a:Dokument AND
       (b:Person OR b:Plats OR b:Organisation))
   OR (type(r) = 'RELATERAR' AND r.stem IS NOT NULL AND
       (a:Person OR a:Plats OR a:Organisation) AND
       (b:Person OR b:Plats OR b:Organisation)))
"""


def fingerprint(
    entries: list[dict], decisions: list[dict], name_rules: list[dict] | None = None,
) -> str:
    """Identifiera precis det underlag och de beslut som förhandsvisades."""
    raw = json.dumps([entries, decisions, name_rules or []], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def read_projection() -> tuple[dict, list[dict], list[dict]]:
    """Läs en sammanhängande SQLite-snapshot och beräkna den avsedda grafen."""
    from graph.review import audit_entries, build_reviewed_rows

    with closing(db.connect()) as conn:
        db.init_schema(conn)
        entries, decisions = db.read_graph_review_snapshot(conn)
        name_rules = db.list_graph_name_rules(conn)
    items = audit_entries(entries, decisions)
    mentions, relations = build_reviewed_rows(entries, decisions, name_rules)
    item_keys = {i["item_key"] for i in items}
    counts = Counter(issue for item in items for issue in item["issues"])
    report = {
        "fingerprint": fingerprint(entries, decisions, name_rules),
        "pages": len(entries), "items": len(items),
        "flagged": sum(bool(i["issues"]) for i in items),
        "pending": sum(bool(i["issues"]) and (not i["decision"] or i["stale"]) for i in items),
        "stale": sum(bool(i["stale"]) for i in items) + sum(
            d["item_key"] not in item_keys for d in decisions),
        "reviewed": sum(bool(i["decision"]) and not i["stale"] for i in items),
        "issues": dict(counts),
        "nodes": len({(m["label"], m["norm"]) for m in mentions}),
        "mentions": len({_mention_key(m) for m in mentions}),
        "relations": len({_relation_key(r) for r in relations}),
    }
    return report, mentions, relations


def save_run(report: dict) -> None:
    """Spara kontrollens slutresultat för senare jämförelser."""
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        db.record_graph_review_run(conn, report=report)


def run_review(context=None) -> int:
    """Kontrollera extraktion och beslut utan att ändra Neo4j."""
    ctx = context or ensure_terminal_context(None)
    ctx.check_cancelled()
    report, _, _ = read_projection()
    ctx.check_cancelled()
    report["kind"] = "review"
    save_run(report)
    ctx.log(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def connect_graph(*, uri: str, user: str):
    """Anslut med befintlig lösenordshantering, utan hemligheter i rapporter."""
    from graph.viz import resolve_password
    from neo4j import GraphDatabase

    password = resolve_password()
    if not password:
        raise OperationFailed("Neo4j-lösenord saknas. Starta Neo4j först.")
    return GraphDatabase.driver(uri, auth=(user, password))


def _mention_key(row: dict) -> tuple:
    return row["stem"], row["sida"], row["label"], row["norm"]


def _relation_key(row: dict) -> tuple:
    return tuple(row[k] for k in (
        "stem", "sida", "typ", "fran_label", "fran_norm", "till_label", "till_norm"))


def _live_rows(session, *, adopt_legacy: bool = False) -> tuple[list[dict], list[dict]]:
    """Läs importerade kanter på samma anslutning eller transaktion."""
    live_m = session.run("""MATCH (d:Dokument)-[r:NÄMNER]->(e)
        WHERE r.graph_owner = $owner OR ($adopt_legacy AND r.graph_owner IS NULL)
        UNWIND [l IN labels(e) WHERE l IN ['Person','Plats','Organisation']] AS label
        RETURN d.stem AS stem, r.sida AS sida, label, e.norm AS norm""",
        owner=GRAPH_OWNER, adopt_legacy=adopt_legacy).data()
    live_r = session.run("""MATCH (a)-[r:RELATERAR]->(b) WHERE r.stem IS NOT NULL
            AND (r.graph_owner = $owner OR ($adopt_legacy AND r.graph_owner IS NULL))
        UNWIND [l IN labels(a) WHERE l IN ['Person','Plats','Organisation']] AS fl
        UNWIND [l IN labels(b) WHERE l IN ['Person','Plats','Organisation']] AS tl
        RETURN r.stem AS stem, r.sida AS sida, r.typ AS typ,
            fl AS fran_label, a.norm AS fran_norm, tl AS till_label, b.norm AS till_norm""",
        owner=GRAPH_OWNER, adopt_legacy=adopt_legacy).data()
    return live_m, live_r


def _live_fingerprint(live_m: list[dict], live_r: list[dict]) -> str:
    """Inkludera även dubbletter i kontrollen av den levande grafen."""
    raw = [sorted(json.dumps(r, sort_keys=True) for r in rows) for rows in (live_m, live_r)]
    return hashlib.sha256(json.dumps(raw).encode()).hexdigest()


def compare_graph(driver, mentions: list[dict], relations: list[dict], *,
                  adopt_legacy: bool = False) -> dict:
    """Jämför källförsedda kanter i Neo4j med den granskade projektionen."""
    with driver.session() as session:
        live_m, live_r = _live_rows(session, adopt_legacy=adopt_legacy)
        legacy_edges = session.run("""MATCH (a)-[r]->(b)
            WHERE r.graph_owner IS NULL AND
              ((type(r) = 'NÄMNER' AND a:Dokument AND
                (b:Person OR b:Plats OR b:Organisation)) OR
               (type(r) = 'RELATERAR' AND r.stem IS NOT NULL AND
                (a:Person OR a:Plats OR a:Organisation) AND
                (b:Person OR b:Plats OR b:Organisation)))
            RETURN count(r) AS count""").single()["count"]
    actual_m, actual_r = {_mention_key(r) for r in live_m}, {_relation_key(r) for r in live_r}
    desired_m = {_mention_key(r) for r in mentions}
    desired_r = {_relation_key(r) for r in relations}
    return {
        "graph_fingerprint": _live_fingerprint(live_m, live_r),
        "mentions_add": len(desired_m - actual_m), "mentions_remove": len(actual_m - desired_m),
        "relations_add": len(desired_r - actual_r), "relations_remove": len(actual_r - desired_r),
        "duplicate_edges": len(live_m) - len(actual_m) + len(live_r) - len(actual_r),
        "legacy_edges": legacy_edges,
    }


def replace_graph(driver, mentions: list[dict], relations: list[dict], ctx, *,
                  expected_graph: str = "", expected_source: str = "",
                  adopt_legacy: bool = False) -> None:
    """Ersätt importerade kanter atomiskt; bevara andra kanter och fristående noder."""
    with driver.session() as session:
        for statement in CONSTRAINTS:
            session.run(statement).consume()
        with session.begin_transaction() as tx:
            ctx.check_cancelled()
            if expected_graph and _live_fingerprint(
                *_live_rows(tx, adopt_legacy=adopt_legacy)
            ) != expected_graph:
                raise OperationFailed("Neo4j har ändrats efter förhandsvisningen. Kör en ny.")
            records = tx.run(_MANAGED + "RETURN collect(DISTINCT elementId(a)) + "
                             "collect(DISTINCT elementId(b)) AS ids",
                             owner=GRAPH_OWNER, adopt_legacy=adopt_legacy).data()
            ids = records[0]["ids"] if records else []
            tx.run(_MANAGED + "DELETE r", owner=GRAPH_OWNER,
                   adopt_legacy=adopt_legacy).consume()
            for rows, writer in ((mentions, write_mentions), (relations, write_relations)):
                for offset in range(0, len(rows), 1000):
                    ctx.check_cancelled()
                    writer(tx, rows[offset:offset + 1000])
            # Endast tidigare importerade noder som fortfarande saknar alla kanter.
            tx.run("MATCH (n) WHERE elementId(n) IN $ids AND (n:Person OR n:Plats OR n:Organisation) "
                   "AND NOT (n)--() DELETE n",
                   ids=ids).consume()
            guard = guard_source(expected_source) if expected_source else nullcontext()
            with guard:
                ctx.check_cancelled()
                tx.commit()



@contextmanager
def guard_source(expected: str):
    """Lås källändringar under sista kontrollen och Neo4j-committen."""
    with closing(db.connect()) as conn, db.graph_review_write_transaction(conn):
        entries, decisions = db.read_graph_review_snapshot(conn)
        name_rules = db.list_graph_name_rules(conn)
        if fingerprint(entries, decisions, name_rules) != expected:
            raise OperationFailed("Underlaget ändrades efter förhandsvisningen. Kör om den.")
        yield


def require_preview(expected: str, uri: str, user: str, adopt_legacy: bool) -> dict[str, Any]:
    """Hämta den sparade förhandsvisningen för exakt denna databas och källa."""
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        reports = db.list_graph_review_runs(conn, limit=100)
    for run in reports:
        report = cast(dict[str, Any], run["report"])
        if (report.get("kind") == "preview" and report.get("fingerprint") == expected
                and report.get("uri") == uri and report.get("user") == user
                and report.get("adopt_legacy", False) == adopt_legacy):
            return report
    raise OperationFailed("En sparad förhandsvisning för denna Neo4j-anslutning saknas.")

def run_sync(*, uri: str = "bolt://localhost:7687", user: str = "neo4j",
             apply: bool = False, expected: str = "", adopt_legacy: bool = False,
             context=None) -> int:
    """Förhandsvisa som standard; applicera bara en uttryckligen vald snapshot."""
    ctx = context or ensure_terminal_context(None)
    ctx.check_cancelled()
    report, mentions, relations = read_projection()
    if apply and (not expected or expected != report["fingerprint"]):
        raise OperationFailed("Underlaget har ändrats eller förhandsvisning saknas. "
                              "Kör en ny förhandsvisning och ange dess --expected-värde.")
    if apply and report.get("stale", 0):
        raise OperationFailed("Inaktuella granskningsbeslut måste granskas eller återställas först.")
    preview = require_preview(expected, uri, user, adopt_legacy) if apply else None
    report.update(uri=uri, user=user, adopt_legacy=adopt_legacy)
    with connect_graph(uri=uri, user=user) as driver:
        report["diff"] = compare_graph(
            driver, mentions, relations, adopt_legacy=adopt_legacy,
        )
        graph_hash = report["diff"].pop("graph_fingerprint")
        report["graph_fingerprint"] = graph_hash
        if apply:
            if report["diff"]["legacy_edges"] and not adopt_legacy:
                raise OperationFailed(
                    "Neo4j innehåller äldre omärkta projektkanter. Kör en ny "
                    "förhandsvisning med --adopt-legacy innan de tas över och rensas."
                )
            # Kontrollera även efter nätverksanropet, före den första dataskrivningen.
            latest, _, _ = read_projection()
            if latest["fingerprint"] != expected:
                raise OperationFailed("Underlaget ändrades efter förhandsvisningen. Kör om den.")
            if preview is None or preview.get("graph_fingerprint") != graph_hash:
                raise OperationFailed("Neo4j har ändrats efter förhandsvisningen. Kör en ny.")
            replace_graph(driver, mentions, relations, ctx,
                          expected_graph=graph_hash, expected_source=expected,
                          adopt_legacy=adopt_legacy)
            verification = compare_graph(driver, mentions, relations)
            verification.pop("graph_fingerprint")
            verification.pop("legacy_edges")
            if any(verification.values()):
                raise OperationFailed("Grafen uppdaterades men efterkontrollen visar avvikelser. "
                                      "Kör en ny kontroll.")
            report["verified"] = True
    report["kind"] = "sync" if apply else "preview"
    save_run(report)
    ctx.log(json.dumps(report, ensure_ascii=False, indent=2))
    if not apply:
        ctx.log("Applicera med --apply --expected " + report["fingerprint"])
    return 0
