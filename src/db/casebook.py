"""Utredningspärm: sparade svar, källbokmärken och utredarens marginalanteckningar."""

from __future__ import annotations

import json
import sqlite3

from ._schema import now

# --- casebook / bokmärken --------------------------------------------

def _json_list(value: list[dict] | None) -> str:
    """Serialisera listdata kompakt men läsbart i state-db."""
    return json.dumps(value or [], ensure_ascii=False, default=str)


def _parse_json_list(raw: str | None) -> list:
    """Tolka JSON-listor från state-db. Trasigt innehåll blir tom lista."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def record_casebook_entry(
    conn: sqlite3.Connection,
    *,
    question: str,
    answer: str,
    mode: str,
    backend: str | None = None,
    model: str | None = None,
    sources: list[dict] | None = None,
    entities: list[dict] | None = None,
    note: str | None = None,
) -> int:
    """Spara ett fråga/svar-spår i utredningspärmen och returnera rad-id."""
    if not question.strip():
        raise ValueError("question får inte vara tom")
    if not answer.strip():
        raise ValueError("answer får inte vara tom")
    cur = conn.execute(
        """
        INSERT INTO casebook_entries(
            question, answer, mode, backend, model, sources_json,
            entities_json, note, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question.strip(),
            answer,
            mode.strip() or "okänt",
            backend,
            model,
            _json_list(sources),
            _json_list(entities),
            note.strip() if note and note.strip() else None,
            now(),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def list_casebook_entries(
    conn: sqlite3.Connection, *, limit: int = 20
) -> list[dict]:
    """Lista sparade fråga/svar-spår, nyast först."""
    rows = conn.execute(
        """
        SELECT id, question, answer, mode, backend, model, sources_json,
               entities_json, note, created_at
        FROM casebook_entries
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "question": row["question"],
            "answer": row["answer"],
            "mode": row["mode"],
            "backend": row["backend"],
            "model": row["model"],
            "sources": _parse_json_list(row["sources_json"]),
            "entities": _parse_json_list(row["entities_json"]),
            "note": row["note"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def delete_casebook_entry(conn: sqlite3.Connection, entry_id: int) -> bool:
    """Radera ett sparat fråga/svar-spår. Returnerar True om något togs bort."""
    cur = conn.execute("DELETE FROM casebook_entries WHERE id=?", (entry_id,))
    conn.commit()
    return cur.rowcount > 0


def _stored_bookmark_page(page: int | None) -> int:
    """Spara okänd sida som 0 så UNIQUE(source, page) fungerar även utan sida."""
    return int(page) if page is not None else 0


def _public_bookmark_page(page: int) -> int | None:
    return page if page > 0 else None


def record_source_bookmark(
    conn: sqlite3.Connection,
    *,
    source: str,
    page: int | None = None,
    nr: str | None = None,
    title: str | None = None,
    note: str | None = None,
) -> int:
    """Spara eller uppdatera ett källbokmärke och returnera rad-id."""
    if not source.strip():
        raise ValueError("source får inte vara tom")
    stored_page = _stored_bookmark_page(page)
    stamp = now()
    conn.execute(
        """
        INSERT INTO source_bookmarks(
            source, page, nr, title, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source, page) DO UPDATE SET
            nr         = excluded.nr,
            title      = excluded.title,
            note       = excluded.note,
            updated_at = excluded.updated_at
        """,
        (
            source.strip(),
            stored_page,
            nr,
            title,
            note.strip() if note and note.strip() else None,
            stamp,
            stamp,
        ),
    )
    row = conn.execute(
        "SELECT id FROM source_bookmarks WHERE source=? AND page=?",
        (source.strip(), stored_page),
    ).fetchone()
    conn.commit()
    return int(row["id"])


def list_source_bookmarks(
    conn: sqlite3.Connection, *, limit: int = 50
) -> list[dict]:
    """Lista källbokmärken, senast uppdaterade först."""
    rows = conn.execute(
        """
        SELECT id, source, page, nr, title, note, created_at, updated_at
        FROM source_bookmarks
        ORDER BY updated_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "page": _public_bookmark_page(row["page"]),
            "nr": row["nr"],
            "title": row["title"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def delete_source_bookmark(conn: sqlite3.Connection, bookmark_id: int) -> bool:
    """Radera ett källbokmärke. Returnerar True om något togs bort."""
    cur = conn.execute("DELETE FROM source_bookmarks WHERE id=?", (bookmark_id,))
    conn.commit()
    return cur.rowcount > 0


# --- source_annotations (utredarens marginalanteckningar) -------------

def record_source_annotation(
    conn: sqlite3.Connection,
    *,
    source: str,
    note: str,
    page: int | None = None,
    nr: str | None = None,
    title: str | None = None,
    quote: str | None = None,
) -> int:
    """Spara en ny anteckning knuten till en källa/sida och returnera rad-id.

    Flera anteckningar per källa/sida tillåts (till skillnad från bokmärken)."""
    if not source.strip():
        raise ValueError("source får inte vara tom")
    if not note.strip():
        raise ValueError("note får inte vara tom")
    stamp = now()
    cur = conn.execute(
        """
        INSERT INTO source_annotations(
            source, page, nr, title, quote, note, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source.strip(),
            _stored_bookmark_page(page),
            nr,
            title,
            quote.strip() if quote and quote.strip() else None,
            note.strip(),
            stamp,
            stamp,
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    assert row_id is not None  # INSERT med autoincrement ger alltid ett rad-id
    return row_id


def update_source_annotation(
    conn: sqlite3.Connection, annotation_id: int, *, note: str
) -> bool:
    """Uppdatera anteckningstexten. Returnerar True om raden fanns."""
    if not note.strip():
        raise ValueError("note får inte vara tom")
    cur = conn.execute(
        "UPDATE source_annotations SET note=?, updated_at=? WHERE id=?",
        (note.strip(), now(), annotation_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_source_annotations(
    conn: sqlite3.Connection, *, source: str | None = None, limit: int = 200
) -> list[dict]:
    """Lista anteckningar, senast uppdaterade först.

    Filtrera på ``source`` för en specifik källa (alla sidor)."""
    sql = (
        "SELECT id, source, page, nr, title, quote, note, created_at, updated_at "
        "FROM source_annotations"
    )
    params: list = []
    if source is not None:
        sql += " WHERE source=?"
        params.append(source.strip())
    sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [
        {
            "id": row["id"],
            "source": row["source"],
            "page": _public_bookmark_page(row["page"]),
            "nr": row["nr"],
            "title": row["title"],
            "quote": row["quote"],
            "note": row["note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in conn.execute(sql, params)
    ]


def delete_source_annotation(conn: sqlite3.Connection, annotation_id: int) -> bool:
    """Radera en anteckning. Returnerar True om något togs bort."""
    cur = conn.execute(
        "DELETE FROM source_annotations WHERE id=?", (annotation_id,)
    )
    conn.commit()
    return cur.rowcount > 0
