"""Tester för den stegvisa grafgranskningen utan Neo4j eller levande jobb."""
from contextlib import closing

import pytest
from streamlit.testing.v1 import AppTest

import db
from graph import review_ui
from graph.review import audit_entries
from graph.review_service import fingerprint


def _app(tmp_path):
    return AppTest.from_string(
        "from pathlib import Path\nfrom graph.review_ui import render_review\n"
        f"render_review(Path({str(tmp_path)!r}))\n"
    ).run(timeout=20)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        db.record_doc_entities(
            conn, pdf_stem="test", page_num=1,
            payload={"entiteter": [{"namn": "J", "typ": "person"}], "relationer": []},
            model="test",
        )
        db.record_page(
            conn, pdf_stem="test", page_num=1, engine="tesseract",
            text="Inledning. Vittnet Johan Andersson omnämns på sidan. Avslutning.", score=80,
        )
    return _app(tmp_path)


def _suggestion():
    with closing(db.connect()) as conn:
        item = audit_entries(*db.read_graph_review_snapshot(conn))[0]
        suggestion = {
            "item_key": item["item_key"], "source_hash": item["source_hash"],
            "action": "replace",
            "target": {"namn": "Johan Andersson", "typ": "person"},
            "note": "Fullständigt namn framgår av sidan",
            "evidence": "Vittnet Johan Andersson omnämns på sidan",
        }
        db.save_graph_review_suggestions(conn, [suggestion], "Testprofil", "test-model")
        return db.list_graph_review_suggestions(conn)[0]


def test_source_excerpt_centers_evidence_and_handles_empty_text():
    source = "A" * 250 + "Johan Andersson" + "B" * 250
    excerpt = review_ui.source_excerpt(source, "Johan Andersson", {"namn": "J"}, radius=40)
    assert excerpt == "…" + "A" * 40 + "Johan Andersson" + "B" * 40 + "…"
    assert review_ui.source_excerpt("", "Johan", {"namn": "J"}) == "Källtext saknas."


def test_preview_summary_translates_diff_and_legacy_edges():
    report = {"diff": {
        "mentions_add": 3, "mentions_remove": 1, "relations_add": 2,
        "relations_remove": 4, "legacy_edges": 7,
    }}
    assert review_ui.preview_summary(report) == [
        "Omnämnanden: 3 läggs till och 1 tas bort.",
        "Relationer: 2 läggs till och 4 tas bort.",
        "Engångsförberedelsen omfattar även 7 äldre importerade kanter.",
    ]


def test_default_view_has_three_steps_and_no_internal_identifiers(tmp_path, monkeypatch):
    app = _setup(tmp_path, monkeypatch)
    assert not app.exception
    assert [v.value for v in app.subheader] == ["1. Analysera", "2. Granska", "3. Uppdatera"]
    assert any(v.label == "Analysera med LLM" for v in app.button)
    assert any(v.label == "Förbered uppdatering" for v in app.button)
    assert any("1 post behöver granskas" in v.value for v in app.caption)
    assert not app.json
    assert "fingerprint" not in " ".join(v.value for v in app.markdown)


def test_analyze_uses_default_profile_and_all_items(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        review_ui.config, "load_all",
        lambda: {"profiles": {"Standard": {}, "Alternativ": {}}, "default": "Standard"},
    )
    monkeypatch.setattr(
        review_ui.job_service, "start_job",
        lambda operation, params: calls.append((operation, params)) or {"id": "hemligt-id"},
    )
    app = _setup(tmp_path, monkeypatch)
    next(v for v in app.button if v.label == "Analysera med LLM").click().run()
    assert not app.exception
    assert calls == [("graph-review-llm", {"profile": "Standard", "limit": 20})]
    assert not any("hemligt-id" in v.value for v in app.success)


def test_advanced_analysis_can_use_profile_limit_and_rules(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        review_ui.config, "load_all",
        lambda: {"profiles": {"Standard": {}, "Alternativ": {}}, "default": "Standard"},
    )
    monkeypatch.setattr(
        review_ui.job_service, "start_job",
        lambda operation, params: calls.append((operation, params)) or {"id": "id"},
    )
    app = _setup(tmp_path, monkeypatch)
    next(v for v in app.selectbox if v.label == "Alternativ LLM-profil").set_value("Alternativ")
    next(v for v in app.number_input if v.label == "Max antal källsidor").set_value(8)
    next(v for v in app.button if v.label == "Analysera valda med LLM").click().run()
    next(v for v in app.button if v.label == "Analysera enbart med regler").click().run()
    assert calls == [
        ("graph-review-llm", {"profile": "Alternativ", "limit": 8}),
        ("graph-review", {}),
    ]


def test_rule_item_is_reviewed_with_direct_keep_action(tmp_path, monkeypatch):
    app = _setup(tmp_path, monkeypatch)
    assert any("J · Person" in v.value for v in app.markdown)
    next(v for v in app.button if v.label == "Behåll").click().run()
    assert not app.exception
    with closing(db.connect()) as conn:
        decisions = db.list_graph_review_decisions(conn)
    assert decisions[0]["action"] == "keep"


def test_manual_save_rejects_decision_changed_in_another_tab(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    with closing(db.connect()) as conn:
        stale_item = audit_entries(*db.read_graph_review_snapshot(conn))[0]
    review_ui._save(stale_item, "keep", {}, "Första granskningen")
    with pytest.raises(ValueError, match="beslutet har ändrats"):
        review_ui._save(stale_item, "exclude", {}, "Gammal webbläsarflik")
    with closing(db.connect()) as conn:
        decisions = db.list_graph_review_decisions(conn)
    assert decisions[0]["action"] == "keep"


def test_global_name_rule_is_previewed_saved_and_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        db.record_doc_entities(
            conn, pdf_stem="test", page_num=1,
            payload={
                "entiteter": [
                    {"namn": "RKA2", "typ": "organisation"},
                    {"namn": "Olof Palme", "typ": "person"},
                ],
                "relationer": [{"fran": "RKA2", "typ": "utredde", "till": "Olof Palme"}],
            },
            model="test",
        )
    app = _app(tmp_path)
    next(v for v in app.selectbox if v.label == "Entitetstyp").set_value("Organisation")
    next(v for v in app.text_input if v.label == "Variant").set_value("RKA2")
    next(v for v in app.text_input if v.label == "Kanoniskt namn").set_value("Rikskriminalen A2")
    next(v for v in app.button if v.label == "Visa träffar").click().run()
    assert any(
        "1 omnämnande" in v.value and "1 relationsförekomst" in v.value
        for v in app.info
    )
    next(v for v in app.button if v.label == "Spara namnregel").click().run()
    with closing(db.connect()) as conn:
        assert db.list_graph_name_rules(conn) == [{
            "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
        }]
    assert any("RKA2" in v.value and "Rikskriminalen A2" in v.value for v in app.markdown)


def test_llm_suggestion_is_first_and_requires_explicit_acceptance(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    _suggestion()
    app = _app(tmp_path)
    assert any("Johan Andersson · Person" in v.value for v in app.markdown)
    assert any("Vittnet Johan Andersson" in v.value for v in app.markdown)
    next(v for v in app.button if v.label == "Godkänn").click().run()
    assert not app.exception
    with closing(db.connect()) as conn:
        assert db.list_graph_review_suggestions(conn)[0]["status"] == "accepted"
        assert db.list_graph_review_decisions(conn)[0]["target"]["namn"] == "Johan Andersson"


def test_stale_llm_suggestion_can_only_be_rejected(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    suggestion = _suggestion()
    with closing(db.connect()) as conn:
        db.record_doc_entities(conn, pdf_stem="test", page_num=1,
                               payload={"entiteter": []}, model="new")
    with pytest.raises(ValueError, match="Källan har ändrats"):
        review_ui._resolve_suggestion(suggestion, True)
    review_ui._resolve_suggestion(suggestion, False)
    with closing(db.connect()) as conn:
        assert db.list_graph_review_decisions(conn) == []
        assert db.list_graph_review_suggestions(conn)[0]["status"] == "rejected"


def test_llm_acceptance_rechecks_page_and_manual_decision(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    suggestion = _suggestion()
    with closing(db.connect()) as conn:
        db.record_page(conn, pdf_stem="test", page_num=1, engine="tesseract",
                       text="Ändrad text", score=80)
    with pytest.raises(ValueError, match="källbelägg"):
        review_ui._resolve_suggestion(suggestion, True)
    with closing(db.connect()) as conn:
        item = audit_entries(*db.read_graph_review_snapshot(conn))[0]
        db.save_graph_review_decision(
            conn, item_key=item["item_key"], source_hash=item["source_hash"],
            action="keep", target={}, note="Nyare manuell kontroll",
        )
    with pytest.raises(ValueError, match="beslutet har ändrats"):
        review_ui._resolve_suggestion(suggestion, True, expected_decision=None)


def test_preview_and_apply_hide_control_code_and_adopt_legacy(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        review_ui.job_service, "start_job",
        lambda operation, params: calls.append((operation, params)) or {"id": "jobb"},
    )
    _setup(tmp_path, monkeypatch)
    with closing(db.connect()) as conn:
        entries, decisions = db.read_graph_review_snapshot(conn)
        current = fingerprint(entries, decisions)
        db.record_graph_review_run(conn, report={
            "kind": "preview", "fingerprint": current, "adopt_legacy": True,
            "diff": {
                "mentions_add": 1, "mentions_remove": 0, "relations_add": 0,
                "relations_remove": 0, "legacy_edges": 2,
            },
        })
    app = _app(tmp_path)
    assert any("1 läggs till" in v.value for v in app.markdown)
    assert not any(current in v.value for v in app.markdown)
    next(v for v in app.button if v.label == "Uppdatera och verifiera grafen").click().run()
    assert calls == [
        ("graph-sync", {"apply": True, "expected": current, "adopt_legacy": True})
    ]


def test_prepare_update_always_adopts_legacy_edges(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        review_ui.job_service, "start_job",
        lambda operation, params: calls.append((operation, params)) or {"id": "jobb"},
    )
    app = _setup(tmp_path, monkeypatch)
    next(v for v in app.button if v.label == "Förbered uppdatering").click().run()
    assert calls == [("graph-sync", {"adopt_legacy": True})]
