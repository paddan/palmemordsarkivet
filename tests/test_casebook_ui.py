"""Tester för delad utredningspärm-logik."""

from __future__ import annotations

import base64

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


def _pdf_token(path) -> str:
    return base64.urlsafe_b64encode(str(path).encode()).decode().rstrip("=")


def test_resolve_pdf_query_path_accepts_pdf_under_root(tmp_path) -> None:
    pdf = tmp_path / "downloaded" / "files" / "281.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")

    assert casebook_ui.resolve_pdf_query_path(tmp_path, _pdf_token(pdf)) == pdf.resolve()


def test_resolve_pdf_query_path_rejects_bad_or_unsafe_paths(tmp_path) -> None:
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"%PDF")
    txt = tmp_path / "generated" / "text" / "281.txt"
    txt.parent.mkdir(parents=True)
    txt.write_text("text", encoding="utf-8")

    assert casebook_ui.resolve_pdf_query_path(tmp_path, "inte-base64") is None
    assert casebook_ui.resolve_pdf_query_path(tmp_path, _pdf_token(outside)) is None
    assert casebook_ui.resolve_pdf_query_path(tmp_path, _pdf_token(txt)) is None


def test_resolve_pdf_query_page_accepts_positive_int_only() -> None:
    assert casebook_ui.resolve_pdf_query_page("12") == 12
    assert casebook_ui.resolve_pdf_query_page(["7"]) == 7
    assert casebook_ui.resolve_pdf_query_page("") is None
    assert casebook_ui.resolve_pdf_query_page("0") is None
    assert casebook_ui.resolve_pdf_query_page("-1") is None
    assert casebook_ui.resolve_pdf_query_page("sju") is None


def test_pdf_open_target_uses_browser_file_url_with_optional_page(tmp_path) -> None:
    pdf = tmp_path / "281.pdf"
    pdf.write_bytes(b"%PDF")

    assert casebook_ui.pdf_open_target(pdf) == pdf.resolve().as_uri()
    assert casebook_ui.pdf_open_target(pdf, page=12) == f"{pdf.resolve().as_uri()}#page=12"


def test_open_pdf_in_browser_uses_new_browser_tab(tmp_path, monkeypatch) -> None:
    pdf = tmp_path / "281.pdf"
    pdf.write_bytes(b"%PDF")
    calls = []

    assert hasattr(casebook_ui, "open_pdf_in_browser")
    monkeypatch.setattr(
        casebook_ui.webbrowser,
        "open",
        lambda url, new=0: calls.append((url, new)) or True,
    )

    casebook_ui.open_pdf_in_browser(pdf, page=12)

    assert calls == [(f"{pdf.resolve().as_uri()}#page=12", 2)]
