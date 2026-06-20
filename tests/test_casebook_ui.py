"""Tester för delad utredningspärm-logik."""

from __future__ import annotations

import casebook_ui
import db as _db


def test_annotation_payload_matches_record_kwargs(tmp_path) -> None:
    """source_bookmark_payload ska kunna skickas rakt in i
    record_source_annotation (det render_annotation_widget gör). Låser fast
    att signaturerna inte glider isär."""
    conn = _db.connect(tmp_path / "state.db")
    _db.init_schema(conn)
    payload = casebook_ui.source_bookmark_payload(
        {"source": "100 — Skandia.txt", "page": 28, "nr": "100",
         "titel": "Skandiaförhör", "text": "ignoreras"}
    )
    annotation_id = _db.record_source_annotation(conn, note="Kolla detta", **payload)
    saved = _db.list_source_annotations(conn, source="100 — Skandia.txt")
    assert annotation_id > 0
    assert saved[0]["note"] == "Kolla detta"
    assert saved[0]["page"] == 28
    assert saved[0]["title"] == "Skandiaförhör"


def test_source_bookmark_payload_normalizes_rag_hit() -> None:
    payload = casebook_ui.source_bookmark_payload({
        "source": "100 — Skandia.txt",
        "page": 28,
        "nr": "100",
        "titel": "Skandiaförhör",
        "text": "Det här långa utdraget ska inte sparas i pärmen.",
    })

    assert payload == {
        "source": "100 — Skandia.txt",
        "page": 28,
        "nr": "100",
        "title": "Skandiaförhör",
    }


def test_source_bookmark_payload_falls_back_to_source_stem() -> None:
    payload = casebook_ui.source_bookmark_payload({
        "source": "865 — Brev.txt",
        "page": None,
    })

    assert payload == {
        "source": "865 — Brev.txt",
        "page": None,
        "nr": "865",
        "title": "865 — Brev",
    }


def test_casebook_entry_title_prefers_note_and_shortens_question() -> None:
    assert casebook_ui.casebook_entry_title({
        "note": "Skandiaspåret",
        "question": "Lista alla vittnen som nämner Skandia",
    }) == "Skandiaspåret"

    title = casebook_ui.casebook_entry_title({
        "note": None,
        "question": (
            "lista alla vittnen som var på mordplatsen och ge en kort "
            "beskrivning av vad de såg"
        ),
    }, max_chars=48)

    assert title == "lista alla vittnen som var på mordplatsen..."
    assert len(title) <= 48


def test_source_display_title_uses_saved_title_and_page() -> None:
    assert casebook_ui.source_display_title({
        "source": "100 — Skandia.txt",
        "title": "Skandiaförhör",
        "page": 28,
    }) == "Skandiaförhör, sida 28"

    assert casebook_ui.source_display_title({
        "source": "865 — Brev.txt",
        "page": None,
    }) == "865 — Brev"


def test_graph_centers_query_params_roundtrip_filters_invalid_entities() -> None:
    entities = [
        {"namn": "Skandia", "label": "Organisation", "norm": "skandia"},
        {"namn": "Stig Engström", "label": "Person", "norm": "stig engström"},
        {"namn": "Saknar norm", "label": "Person"},
    ]

    params = casebook_ui.graph_centers_query_params(entities)
    assert set(params) == {"centers"}

    assert casebook_ui.decode_graph_centers_param(params["centers"]) == [
        {"namn": "Skandia", "label": "Organisation", "norm": "skandia"},
        {"namn": "Stig Engström", "label": "Person", "norm": "stig engström"},
    ]


def test_decode_graph_centers_param_returns_empty_list_for_bad_input() -> None:
    assert casebook_ui.decode_graph_centers_param("") == []
    assert casebook_ui.decode_graph_centers_param("inte-base64") == []
