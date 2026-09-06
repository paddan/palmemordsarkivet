"""Bestående granskningsbeslut, migration och atomisk historik."""
import sqlite3

import pytest

import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / 'review.db')
    db.init_schema(connection)
    yield connection
    connection.close()


def save(conn, action='replace', **kwargs):
    values = dict(item_key='doc:1:entity:0', source_hash='hash', action=action,
                  target={'namn': 'Åke', 'typ': 'Person'}, note='Kontrollerat på sidan')
    values.update(kwargs)
    db.save_graph_review_decision(conn, **values)


def test_migrate_v7_preserves_original_extraction(tmp_path):
    conn = db.connect(tmp_path / 'legacy.db')
    conn.executescript('''CREATE TABLE schema_version(version INTEGER PRIMARY KEY, applied_at TEXT);
        INSERT INTO schema_version VALUES(7, 'then');
        CREATE TABLE doc_entities(pdf_stem TEXT, page_num INTEGER, payload TEXT,
        model TEXT, extracted_at TEXT, PRIMARY KEY(pdf_stem,page_num));
        INSERT INTO doc_entities VALUES('original',1,'{"entiteter":[]}','model','then');''')
    db.init_schema(conn)
    db.init_schema(conn)
    assert db.schema_version(conn) == 9
    assert conn.execute('PRAGMA user_version').fetchone()[0] == 9
    assert db.iter_doc_entities(conn)[0]['payload'] == {'entiteter': []}
    save(conn)
    assert db.list_graph_review_decisions(conn)[0]['target']['namn'] == 'Åke'
    conn.close()


def test_upsert_and_reset_preserve_history(conn):
    save(conn)
    save(conn, 'keep', target={})
    assert db.list_graph_review_decisions(conn) == [dict(
        item_key='doc:1:entity:0', source_hash='hash', action='keep', target={},
        note='Kontrollerat på sidan')]
    save(conn, 'reset', target={})
    assert db.list_graph_review_decisions(conn) == []
    assert [r[0] for r in conn.execute(
        'SELECT action FROM graph_review_history ORDER BY id')] == ['replace', 'keep', 'reset']


@pytest.mark.parametrize('kwargs', [dict(note='  '), dict(action='unknown'),
                                     dict(target=[]), dict(item_key=''), dict(source_hash='')])
def test_invalid_decision_changes_nothing(conn, kwargs):
    with pytest.raises(ValueError):
        save(conn, **kwargs)
    assert db.list_graph_review_decisions(conn) == []


def test_history_failure_rolls_back_active_change(conn):
    save(conn, 'keep', target={})
    conn.execute('''CREATE TRIGGER reject_history BEFORE INSERT ON graph_review_history
        BEGIN SELECT RAISE(ABORT, 'simulerat fel'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        save(conn, 'exclude')
    assert db.list_graph_review_decisions(conn)[0]['action'] == 'keep'
    assert conn.execute('SELECT COUNT(*) FROM graph_review_history').fetchone()[0] == 1


def test_runs_json_roundtrip_and_newest_first(conn):
    first = db.record_graph_review_run(conn, report={'count': 2, 'issues': ['Åke']})
    second = db.record_graph_review_run(conn, report={'count': 0})
    rows = db.list_graph_review_runs(conn)
    assert [r['id'] for r in rows] == [second, first]
    assert rows[1]['report'] == {'count': 2, 'issues': ['Åke']}
    assert rows[0]['created_at']
    assert len(db.list_graph_review_runs(conn, limit=1)) == 1


def test_page_text_is_current_page_and_document_specific(conn):
    conn.executemany('INSERT INTO pdf_pages VALUES(?,?,?,?,?,?)', [
        ('doc', 1, 'llm', 'Korrigerad text', 95, 'now'),
        ('other', 1, 'tesseract', 'Annan text', 80, 'now')])
    assert db.get_graph_review_page(conn, 'doc', 1) == 'Korrigerad text'
    assert db.get_graph_review_page(conn, 'doc', 2) == ''


def test_snapshot_preserves_outer_transaction(conn):
    conn.execute('BEGIN')
    conn.execute("INSERT INTO doc_entities VALUES('doc',1,'{}','model','now')")
    save(conn, 'keep', target={})
    entries, decisions = db.read_graph_review_snapshot(conn)
    assert entries == [{'pdf_stem': 'doc', 'page_num': 1, 'payload': {}}]
    assert decisions[0]['action'] == 'keep'
    assert conn.in_transaction
    conn.rollback()
    assert db.read_graph_review_snapshot(conn) == ([], [])


def test_decision_history_decodes_targets_and_filters_item(conn):
    save(conn)
    save(conn, 'reset', target={})
    save(conn, item_key='other')
    history = db.get_graph_review_decision_history(conn, 'doc:1:entity:0')
    assert [row['action'] for row in history] == ['replace', 'reset']
    assert history[0]['target'] == {'namn': 'Åke', 'typ': 'Person'}
    assert history[1]['target'] == {}
    assert all(row['created_at'] and row['source_hash'] == 'hash' for row in history)


def test_write_transaction_locks_before_reads_and_rolls_back(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    other = sqlite3.connect(path, timeout=0)
    try:
        with (pytest.raises(RuntimeError, match='avbrutet'),
              db.graph_review_write_transaction(conn)):
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute("INSERT INTO graph_review_runs VALUES(1,'{}','now')")
            save(conn)
            raise RuntimeError('avbrutet')
        assert db.list_graph_review_decisions(conn) == []
        assert db.get_graph_review_decision_history(conn, 'doc:1:entity:0') == []
        with db.graph_review_write_transaction(conn):
            save(conn)
        assert other.execute('SELECT COUNT(*) FROM graph_review_decisions').fetchone()[0] == 1
    finally:
        other.close()


def test_nested_write_transaction_preserves_outer_changes(conn):
    conn.execute('BEGIN IMMEDIATE')
    save(conn, 'keep', target={})
    with pytest.raises(RuntimeError), db.graph_review_write_transaction(conn):
        save(conn, 'exclude')
        raise RuntimeError('avbrutet')
    assert conn.in_transaction
    assert db.list_graph_review_decisions(conn)[0]['action'] == 'keep'
    conn.rollback()
    assert db.list_graph_review_decisions(conn) == []


def test_suggestions_roundtrip_status_and_reproposal(conn):
    proposal = dict(item_key='doc:1:entity:0', source_hash='hash', action='replace',
                    target={'namn': 'Åke', 'typ': 'person'}, note='Fullständigt namn',
                    evidence='Åke uppgav')
    db.save_graph_review_suggestions(conn, [proposal], profile='lokal', model='qwen')
    row = db.list_graph_review_suggestions(conn)[0]
    assert row['target'] == {'namn': 'Åke', 'typ': 'person'}
    assert row['status'] == 'pending'
    assert row['evidence'] == 'Åke uppgav'
    assert (row['profile'], row['model']) == ('lokal', 'qwen')
    db.set_graph_review_suggestion_status(conn, proposal['item_key'], 'hash', 'accepted')
    assert db.list_graph_review_suggestions(conn)[0]['status'] == 'accepted'
    db.save_graph_review_suggestions(conn, [proposal], profile='lokal', model='qwen2')
    assert db.list_graph_review_suggestions(conn)[0]['status'] == 'pending'
    assert db.list_graph_review_decisions(conn) == []


def test_global_name_rule_roundtrip_and_delete(conn):
    db.save_graph_name_rule(
        conn, typ="organisation", source="  RKA2  ", target="Rikskriminalen A2",
    )
    assert db.list_graph_name_rules(conn) == [{
        "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
    }]
    assert db.delete_graph_name_rule(conn, typ="organisation", source="rka2")
    assert db.list_graph_name_rules(conn) == []


def test_suggestion_batch_invalid_rolls_back(conn):
    good = dict(item_key='one', source_hash='hash', action='keep', target={},
                note='Kontrollerat', evidence='Källtext')
    bad = dict(good, item_key='two', action='reset')
    with pytest.raises(ValueError):
        db.save_graph_review_suggestions(conn, [good, bad], profile='lokal', model='qwen')
    assert db.list_graph_review_suggestions(conn) == []
    with pytest.raises(ValueError):
        db.set_graph_review_suggestion_status(conn, 'one', 'hash', 'invalid')
