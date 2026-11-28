"""Dokumentrader ur document_meta: sidor, uppslag, hänvisningar och delar."""
from graph.document_rows import build_document_rows, count_pages, nr_key


def _meta(stem, nr=None, arkiv="palme", uppslag=(), hanv=()):
    return {"pdf_stem": stem, "arkiv": arkiv, "nr": nr, "titel": "T", "bestallt": None,
            "upplagt": None, "anmarkning": None, "antal_sidor": None, "dokumentdatum": None,
            "uppslag": list(uppslag), "hanvisningar": list(hanv), "avkortat": False}


def test_count_pages_matches_ingest_split():
    from rag.ingest import split_pages
    for text in ("", "a", "a\fb", "a\f\fc\f"):
        pages = split_pages(text)
        tomt_slut = len(pages) > 1 and not pages[-1].strip()
        assert count_pages(text) == len(pages) - (1 if tomt_slut else 0)


def test_count_pages_ignores_empty_trailing_segment():
    assert count_pages("a\fb\f") == 2
    assert count_pages("a\f") == 1
    assert count_pages("") == 1
    assert count_pages("a\f\fc\f") == 3


def test_nr_key():
    assert nr_key(" 281,10 ") == "281.10"


def test_pages_and_document_rows():
    rows = build_document_rows([_meta("A", "1")], {"A": 2})
    assert rows.documents[0]["sidor"] == 2
    assert rows.documents[0]["stem"] == "A"
    assert rows.pages == [{"id": "A#1", "stem": "A", "sida": 1}, {"id": "A#2", "stem": "A", "sida": 2}]


def test_reference_to_number_with_two_documents_links_both():
    rows = build_document_rows([_meta("A", "1", hanv=["5"]), _meta("B", "5"), _meta("C", "5")], {})
    assert sorted(r["target_stem"] for r in rows.references) == ["B", "C"]


def test_unknown_and_self_references():
    rows = build_document_rows([_meta("A", "1", hanv=["1", "999"])], {})
    assert rows.references == [] and rows.unresolved_references == 1


def test_comma_and_dot_numbers_match():
    rows = build_document_rows([_meta("A", "1", hanv=["281,10"]), _meta("B", "281.10")], {})
    assert rows.references == [{"stem": "A", "target_stem": "B"}]


def test_part_links_to_nearest_existing_parent():
    rows = build_document_rows([_meta("P", "1295"), _meta("C", "1295.24.3")], {})
    assert rows.parts == [{"stem": "C", "parent_stem": "P"}]


def test_wpu_documents_get_no_references_or_parts():
    rows = build_document_rows([_meta("W", None, arkiv="wpu", uppslag=["E9979-00-A"])], {})
    assert rows.references == [] and rows.parts == [] and rows.uppslag == [{"stem": "W", "kod": "E9979-00-A"}]
