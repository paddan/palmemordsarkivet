"""Kunskapsgraf: doc_entities, globala namnregler och grafgranskning (beslut,
körningar och LLM-förslag)."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Collection, Iterator
from contextlib import contextmanager

from ._schema import now

# --- doc_entities (kunskapsgraf) --------------------------------------

def record_doc_entities(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, payload: dict, model: str,
) -> None:
    """UPSERT av extraherade entiteter/relationer för en sida. payload som JSON."""
    conn.execute(
        """INSERT INTO doc_entities(pdf_stem, page_num, payload, model, extracted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
               payload=excluded.payload, model=excluded.model,
               extracted_at=excluded.extracted_at""",
        (pdf_stem, page_num, json.dumps(payload, ensure_ascii=False), model, now()),
    )
    conn.commit()


def doc_entities_extracted(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om sidan redan entitetsextraherats."""
    return conn.execute(
        "SELECT 1 FROM doc_entities WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def iter_doc_entities(conn: sqlite3.Connection) -> list[dict]:
    """Alla extraktioner som dictar med pdf_stem, page_num, payload (parsad JSON)."""
    return [
        {"pdf_stem": row["pdf_stem"], "page_num": row["page_num"],
         "payload": json.loads(row["payload"])}
        for row in conn.execute(
            "SELECT pdf_stem, page_num, payload FROM doc_entities "
            "ORDER BY pdf_stem, page_num"
        )
    ]


# --- grafgranskning ---------------------------------------------------

_GRAPH_ENTITY_TYPES = {"person", "plats", "organisation"}


def _graph_name_rule_value(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} måste vara text")
    cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
    if not cleaned:
        raise ValueError(f"{field} får inte vara tomt")
    return cleaned


def _graph_name_rule_norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def save_graph_name_rule(
    conn: sqlite3.Connection, *, typ: str, source: str, target: str,
) -> None:
    """Spara en typbunden global namnregel utan att ändra originalextraktion."""
    normalized_type = str(typ).strip().casefold()
    if normalized_type not in _GRAPH_ENTITY_TYPES:
        raise ValueError("Okänd entitetstyp för namnregeln")
    clean_source = _graph_name_rule_value(source, "Varianten")
    clean_target = _graph_name_rule_value(target, "Det kanoniska namnet")
    source_norm = _graph_name_rule_norm(clean_source)
    if source_norm == _graph_name_rule_norm(clean_target):
        raise ValueError("Varianten och det kanoniska namnet skiljer sig inte")
    stamp = now()
    with graph_review_write_transaction(conn):
        conn.execute(
            """INSERT INTO graph_name_rules
               (typ,source,source_norm,target,created_at,updated_at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(typ,source_norm) DO UPDATE SET source=excluded.source,
               target=excluded.target,updated_at=excluded.updated_at""",
            (normalized_type, clean_source, source_norm, clean_target, stamp, stamp),
        )


def list_graph_name_rules(conn: sqlite3.Connection) -> list[dict]:
    """Lista globala namnregler i stabil ordning för projektion och UI."""
    return [
        {"typ": row["typ"], "source": row["source"], "target": row["target"]}
        for row in conn.execute(
            "SELECT typ,source,target FROM graph_name_rules ORDER BY typ,source_norm"
        )
    ]


def delete_graph_name_rule(conn: sqlite3.Connection, *, typ: str, source: str) -> bool:
    """Ta bort en exakt typbunden regel, oberoende av versaler och blanksteg."""
    normalized_type = str(typ).strip().casefold()
    if normalized_type not in _GRAPH_ENTITY_TYPES:
        raise ValueError("Okänd entitetstyp för namnregeln")
    source_norm = _graph_name_rule_norm(_graph_name_rule_value(source, "Varianten"))
    with graph_review_write_transaction(conn):
        cursor = conn.execute(
            "DELETE FROM graph_name_rules WHERE typ=? AND source_norm=?",
            (normalized_type, source_norm),
        )
    return cursor.rowcount == 1

def list_graph_review_decisions(conn: sqlite3.Connection) -> list[dict]:
    """Aktiva beslut; källans fingerprint valideras av granskningsmotorn."""
    return [
        {"item_key": row["item_key"], "source_hash": row["source_hash"],
         "action": row["action"], "target": json.loads(row["target_json"]),
         "note": row["note"]}
        for row in conn.execute("SELECT * FROM graph_review_decisions ORDER BY item_key")
    ]


def reset_orphan_graph_review_decisions(
    conn: sqlite3.Connection, valid_item_keys: Collection[str], *, note: str,
) -> int:
    """Återställ beslut vars objekt inte längre finns i projektionen.

    Ett sådant beslut kan aldrig appliceras igen (objektet finns inte kvar),
    men blockerade tidigare all applicering eftersom det räknades som
    inaktuellt. Historiken bevaras — återställningen loggas som ``reset``.
    Returnerar antalet återställda beslut.
    """
    orphans = [
        decision for decision in list_graph_review_decisions(conn)
        if decision["item_key"] not in valid_item_keys
    ]
    if not orphans:
        return 0
    with graph_review_write_transaction(conn):
        for decision in orphans:
            save_graph_review_decision(
                conn,
                item_key=decision["item_key"],
                source_hash=decision["source_hash"],
                action="reset",
                target={},
                note=note,
            )
    return len(orphans)


def save_graph_review_decision(
    conn: sqlite3.Connection, *, item_key: str, source_hash: str,
    action: str, target: dict, note: str,
) -> None:
    """Spara beslut och historik atomiskt; reset tar bort det aktiva beslutet."""
    if action not in {"keep", "exclude", "replace", "reset"}:
        raise ValueError("Okänd granskningsåtgärd")
    if not isinstance(target, dict):
        raise ValueError("Målet måste vara ett objekt")
    if not all(isinstance(value, str) and value.strip()
               for value in (item_key, source_hash, note)):
        raise ValueError("Objektnyckel, källhash och motivering krävs")
    values = (item_key, source_hash, action,
              json.dumps(target, ensure_ascii=False, allow_nan=False), note.strip(), now())
    # En savepoint bevarar även en eventuell yttre transaktion vid fel.
    conn.execute("SAVEPOINT graph_review_decision")
    try:
        if action == "reset":
            conn.execute("DELETE FROM graph_review_decisions WHERE item_key=?", (item_key,))
        else:
            conn.execute(
                """INSERT INTO graph_review_decisions
                   (item_key,source_hash,action,target_json,note,created_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(item_key) DO UPDATE SET source_hash=excluded.source_hash,
                   action=excluded.action,target_json=excluded.target_json,
                   note=excluded.note,created_at=excluded.created_at""", values,
            )
        conn.execute(
            """INSERT INTO graph_review_history
               (item_key,source_hash,action,target_json,note,created_at) VALUES(?,?,?,?,?,?)""",
            values,
        )
    except Exception:
        conn.execute("ROLLBACK TO graph_review_decision")
        conn.execute("RELEASE graph_review_decision")
        raise
    conn.execute("RELEASE graph_review_decision")


def record_graph_review_run(conn: sqlite3.Connection, *, report: dict) -> int:
    """Spara kontrollrapporten med dess fingerprint och fynd som JSON."""
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False)
    cursor = conn.execute(
        "INSERT INTO graph_review_runs(report_json,created_at) VALUES(?,?)", (payload, now()),
    )
    conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def list_graph_review_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Senaste kontrollerna först, med avkodad rapport."""
    return [
        {"id": row["id"], "created_at": row["created_at"],
         "report": json.loads(row["report_json"])}
        for row in conn.execute(
            "SELECT * FROM graph_review_runs ORDER BY id DESC LIMIT ?", (max(0, limit),),
        )
    ]


def get_graph_review_page(conn: sqlite3.Connection, stem: str, page_num: int) -> str:
    """Hämta sidans aktuella text, inklusive eventuell LLM-korrigering."""
    row = conn.execute(
        "SELECT text FROM pdf_pages WHERE pdf_stem=? AND page_num=?", (stem, page_num),
    ).fetchone()
    return (row["text"] or "") if row else ""


def read_graph_review_snapshot(conn: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    """Läs extraktioner och beslut ur samma snapshot, även i en yttre transaktion."""
    conn.execute("SAVEPOINT graph_review_snapshot")
    try:
        entries = iter_doc_entities(conn)
        decisions = list_graph_review_decisions(conn)
    except Exception:
        conn.execute("ROLLBACK TO graph_review_snapshot")
        conn.execute("RELEASE graph_review_snapshot")
        raise
    conn.execute("RELEASE graph_review_snapshot")
    return entries, decisions


def get_graph_review_decision_history(conn: sqlite3.Connection, item_key: str) -> list[dict]:
    """Beslutshistorik för ett objekt, äldsta först och inklusive återställningar."""
    return [
        {"id": row["id"], "item_key": row["item_key"], "source_hash": row["source_hash"],
         "action": row["action"], "target": json.loads(row["target_json"]),
         "note": row["note"], "created_at": row["created_at"]}
        for row in conn.execute(
            "SELECT * FROM graph_review_history WHERE item_key=? ORDER BY id", (item_key,),
        )
    ]


@contextmanager
def graph_review_write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Lås före granskning och spara atomiskt; yttre transaktion måste redan ha skrivlås."""
    nested = conn.in_transaction
    conn.execute("SAVEPOINT graph_review_write" if nested else "BEGIN IMMEDIATE")
    try:
        yield conn
        if nested:
            conn.execute("RELEASE graph_review_write")
        else:
            conn.commit()
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO graph_review_write")
            conn.execute("RELEASE graph_review_write")
        else:
            conn.rollback()
        raise



def save_graph_review_suggestions(
    conn: sqlite3.Connection, suggestions: list[dict], profile: str, model: str,
) -> None:
    """Spara LLM-förslag atomiskt som väntande granskningsmaterial, aldrig beslut."""
    if not all(isinstance(v, str) and v.strip() for v in (profile, model)):
        raise ValueError("Profil och modell krävs")
    values = []
    for item in suggestions:
        if not isinstance(item, dict):
            raise ValueError("Förslaget måste vara ett objekt")
        if item.get("action") not in {"keep", "exclude", "replace"}:
            raise ValueError("Okänd förslagsåtgärd")
        if not isinstance(item.get("target"), dict):
            raise ValueError("Förslagets mål måste vara ett objekt")
        if not all(isinstance(item.get(k), str) and item[k].strip()
                   for k in ("item_key", "source_hash", "note", "evidence")):
            raise ValueError("Objektnyckel, källhash, motivering och källbelägg krävs")
        values.append((item["item_key"], item["source_hash"], item["action"],
                       json.dumps(item["target"], ensure_ascii=False, allow_nan=False),
                       item["note"].strip(), item["evidence"].strip(), profile, model, now()))
    with graph_review_write_transaction(conn):
        conn.executemany(
            """INSERT INTO graph_review_suggestions
               (item_key,source_hash,action,target_json,note,evidence,profile,model,created_at,status)
               VALUES(?,?,?,?,?,?,?,?,?,'pending')
               ON CONFLICT(item_key,source_hash) DO UPDATE SET action=excluded.action,
               target_json=excluded.target_json,note=excluded.note,evidence=excluded.evidence,
               profile=excluded.profile,model=excluded.model,created_at=excluded.created_at,
               status='pending'""", values,
        )


def list_graph_review_suggestions(conn: sqlite3.Connection) -> list[dict]:
    """Alla LLM-förslag inklusive status och ursprung, med avkodat mål."""
    result = []
    for row in conn.execute("SELECT * FROM graph_review_suggestions ORDER BY item_key,source_hash"):
        item = dict(row)
        item["target"] = json.loads(item.pop("target_json"))
        result.append(item)
    return result


def set_graph_review_suggestion_status(
    conn: sqlite3.Connection, item_key: str, source_hash: str, status: str,
) -> None:
    """Ändra förslagets status utan att själv skapa granskningsbeslut."""
    if status not in {"pending", "accepted", "rejected"}:
        raise ValueError("Okänd förslagsstatus")
    with graph_review_write_transaction(conn):
        cursor = conn.execute(
            "UPDATE graph_review_suggestions SET status=? WHERE item_key=? AND source_hash=?",
            (status, item_key, source_hash),
        )
        if cursor.rowcount != 1:
            raise ValueError("Granskningsförslaget finns inte")
