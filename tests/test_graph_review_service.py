"""Verifiera att grafuppdatering är explicit, avgränsad och atomisk."""
from unittest.mock import MagicMock

import pytest

from graph import review_service as service
from operations.exceptions import OperationCancelled, OperationFailed


def test_replace_rolls_back_when_cancelled():
    driver = MagicMock()
    tx = driver.session.return_value.__enter__.return_value.begin_transaction.return_value.__enter__.return_value
    tx.run.return_value.data.return_value = [{"ids": ["old-node"]}]
    ctx = MagicMock()
    ctx.check_cancelled.side_effect = [None, OperationCancelled("stopp")]
    with pytest.raises(OperationCancelled):
        service.replace_graph(driver, [{"label": "Person", "norm": "x"}], [], ctx)
    tx.commit.assert_not_called()
    manager = driver.session.return_value.__enter__.return_value.begin_transaction.return_value
    assert manager.__exit__.call_args.args[0] is OperationCancelled


def test_replace_cleans_only_managed_edges_and_old_orphans():
    driver = MagicMock()
    tx = driver.session.return_value.__enter__.return_value.begin_transaction.return_value.__enter__.return_value
    tx.run.return_value.data.return_value = [{"ids": ["old-node"]}]
    service.replace_graph(driver, [], [], MagicMock())
    queries = [c.args[0] for c in tx.run.call_args_list]
    assert not any("DETACH DELETE" in q for q in queries)
    assert any("NÄMNER" in q and "RELATERAR" in q for q in queries)
    assert any("r.graph_owner = $owner" in q for q in queries)
    assert any("elementId(n) IN $ids" in q and "NOT (n)--()" in q for q in queries)
    tx.commit.assert_called_once()


def test_apply_requires_matching_preview_before_connection(monkeypatch):
    monkeypatch.setattr(service, "read_projection", lambda: ({"fingerprint": "new"}, [], []))
    connect = MagicMock()
    monkeypatch.setattr(service, "connect_graph", connect)
    with pytest.raises(OperationFailed, match="förhandsvisning"):
        service.run_sync(apply=True, expected="old", context=MagicMock())
    connect.assert_not_called()


def test_preview_never_opens_write_transaction(monkeypatch):
    report = {"fingerprint": "new", "nodes": 0, "mentions": 0, "relations": 0}
    monkeypatch.setattr(service, "read_projection", lambda: (report, [], []))
    driver = MagicMock()
    connected = driver.__enter__.return_value
    session = connected.session.return_value.__enter__.return_value
    session.run.return_value.data.return_value = []
    session.run.return_value.single.return_value = {"count": 0}
    monkeypatch.setattr(service, "connect_graph", lambda **kw: driver)
    monkeypatch.setattr(service, "save_run", lambda report: None)
    service.run_sync(context=MagicMock())
    connected.session.return_value.__enter__.return_value.begin_transaction.assert_not_called()


def test_apply_is_bound_to_preview_target(monkeypatch):
    monkeypatch.setattr(
        service,
        "read_projection",
        lambda: ({"fingerprint": "source", "stale": 0}, [], []),
    )
    monkeypatch.setattr(service, "require_preview", MagicMock(side_effect=OperationFailed("fel mål")))
    connect = MagicMock()
    monkeypatch.setattr(service, "connect_graph", connect)

    with pytest.raises(OperationFailed, match="fel mål"):
        service.run_sync(
            uri="bolt://annan:7687", apply=True, expected="source", context=MagicMock()
        )
    connect.assert_not_called()


def test_apply_rejects_graph_changed_since_preview(monkeypatch):
    monkeypatch.setattr(
        service,
        "read_projection",
        lambda: ({"fingerprint": "source", "stale": 0}, [], []),
    )
    monkeypatch.setattr(
        service,
        "require_preview",
        lambda expected, uri, user, adopt_legacy: {"graph_fingerprint": "old"},
    )
    monkeypatch.setattr(
        service,
        "compare_graph",
        lambda driver, mentions, relations, **kwargs: {
            "graph_fingerprint": "new",
            "mentions_add": 0,
            "mentions_remove": 1,
            "relations_add": 0,
            "relations_remove": 0,
            "duplicate_edges": 0,
            "legacy_edges": 0,
        },
    )
    driver = MagicMock()
    monkeypatch.setattr(service, "connect_graph", lambda **kwargs: driver)
    replace = MagicMock()
    monkeypatch.setattr(service, "replace_graph", replace)

    with pytest.raises(OperationFailed, match="Neo4j har ändrats"):
        service.run_sync(apply=True, expected="source", context=MagicMock())
    replace.assert_not_called()


def test_replace_does_not_delete_isolated_documents():
    driver = MagicMock()
    tx = driver.session.return_value.__enter__.return_value.begin_transaction.return_value.__enter__.return_value
    tx.run.return_value.data.return_value = [{"ids": ["document", "entity"]}]

    service.replace_graph(driver, [], [], MagicMock())

    cleanup = next(call.args[0] for call in tx.run.call_args_list
                   if "elementId(n) IN $ids" in call.args[0])
    assert "n:Person OR n:Plats OR n:Organisation" in cleanup


def test_apply_requires_explicit_legacy_adoption(monkeypatch):
    monkeypatch.setattr(
        service, "read_projection",
        lambda: ({"fingerprint": "source", "stale": 0}, [], []),
    )
    monkeypatch.setattr(
        service, "require_preview",
        lambda expected, uri, user, adopt_legacy: {"graph_fingerprint": "same"},
    )
    monkeypatch.setattr(
        service, "compare_graph",
        lambda driver, mentions, relations, **kwargs: {
            "graph_fingerprint": "same", "legacy_edges": 3,
            "mentions_add": 0, "mentions_remove": 0,
            "relations_add": 0, "relations_remove": 0, "duplicate_edges": 0,
        },
    )
    monkeypatch.setattr(service, "connect_graph", lambda **kwargs: MagicMock())
    replace = MagicMock()
    monkeypatch.setattr(service, "replace_graph", replace)

    with pytest.raises(OperationFailed, match="--adopt-legacy"):
        service.run_sync(apply=True, expected="source", context=MagicMock())
    replace.assert_not_called()


def test_projection_fingerprint_changes_when_global_name_rules_change():
    assert service.fingerprint([], [], []) != service.fingerprint([], [], [{
        "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
    }])
