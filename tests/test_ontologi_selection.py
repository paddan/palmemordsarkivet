"""Urval av sidor för ontologi-extraktionen (ren funktion, ingen I/O)."""
from ontologi.config import Urval
from ontologi.extraction import text_hash
from ontologi.selection import PageRef, select_pages, series_of

LONG = "Vittnet såg en man vid Tunnelgatan klockan 23.30 och han var lång. " * 3


def _urval(**over):
    base = {"serier": ("EAE",), "sokfragor": (), "stammar": (), "sidor": (), "max_sidor": 100}
    base.update(over)
    return Urval(**base)


def _meta(stem, *uppslag):
    return {"pdf_stem": stem, "uppslag": list(uppslag)}


def _select(metas, texts, urval=None, **over):
    kw = {
        "extractions": {}, "schemaversion": 1, "reviewed": set(),
        "search_hits": [], "max_pages": 100, "force": False,
    }
    kw.update(over)
    return select_pages(metas, texts, urval or _urval(), **kw)


def test_series_of():
    assert series_of("EAE621-00") == "EAE"
    assert series_of("EA9982-00-D") == "EA"
    assert series_of("D4991-00") == "D"


def test_series_selects_only_exact_series():
    metas = [_meta("a", "EAE621-00"), _meta("b", "D4991-00"), _meta("c", "EAEX1-00")]
    texts = {"a": [LONG], "b": [LONG], "c": [LONG]}
    refs, stats = _select(metas, texts)
    assert refs == [PageRef("a", 1, "serie")]
    assert stats["serie"] == 1


def test_missing_text_is_skipped():
    refs, _ = _select([_meta("a", "EAE621-00")], {})
    assert refs == []


def test_short_pages_are_skipped():
    short = "a" * 50 + " ,.- " * 40
    refs, stats = _select([_meta("a", "EAE1-00")], {"a": [short, LONG]})
    assert refs == [PageRef("a", 2, "serie")]
    assert stats["kort"] == 1


def test_same_text_and_version_is_not_reselected():
    ex = {("a", 1): {"text_hash": text_hash(LONG), "schemaversion": 1}}
    refs, stats = _select([_meta("a", "EAE1-00")], {"a": [LONG]}, extractions=ex)
    assert refs == []
    assert stats["oförändrad"] == 1


def test_changed_text_is_reselected_even_if_reviewed():
    ex = {("a", 1): {"text_hash": "gammal", "schemaversion": 1}}
    refs, _ = _select(
        [_meta("a", "EAE1-00")], {"a": [LONG]}, extractions=ex, reviewed={("a", 1)}
    )
    assert refs == [PageRef("a", 1, "serie")]


def test_new_schema_version_skips_reviewed_unless_force():
    ex = {("a", 1): {"text_hash": text_hash(LONG), "schemaversion": 1}}
    kw = {"extractions": ex, "reviewed": {("a", 1)}, "schemaversion": 2}
    metas, texts = [_meta("a", "EAE1-00")], {"a": [LONG]}
    refs, stats = _select(metas, texts, **kw)
    assert refs == [] and stats["granskad"] == 1
    refs, _ = _select(metas, texts, force=True, **kw)
    assert refs == [PageRef("a", 1, "serie")]
    # Ogranskad sida med ny schemaversion extraheras om.
    refs, _ = _select(metas, texts, extractions=ex, schemaversion=2)
    assert refs == [PageRef("a", 1, "serie")]


def test_order_and_max_pages():
    metas = [_meta("a", "EAE1-00"), _meta("b", "EAE2-00")]
    texts = {"a": [LONG, LONG], "b": [LONG, LONG], "c": [LONG]}
    urval = _urval(sidor=(("c", 1),))
    kw = {"search_hits": [("b", 2), ("a", 2)]}
    refs, stats = _select(metas, texts, urval, **kw)
    assert refs == [
        PageRef("c", 1, "uttrycklig"),
        PageRef("a", 2, "sok"), PageRef("b", 2, "sok"),
        PageRef("a", 1, "serie"), PageRef("b", 1, "serie"),
    ]
    refs, stats = _select(metas, texts, urval, max_pages=2, **kw)
    assert len(refs) == 2
    assert stats["avkortade"] == 3


def test_explicit_stems_and_out_of_range():
    texts = {"a": [LONG, LONG]}
    urval = _urval(serier=(), stammar=("a",), sidor=(("a", 9), ("zzz", 1)))
    refs, stats = _select([], texts, urval, search_hits=[("a", 0)])
    assert refs == [PageRef("a", 1, "uttrycklig"), PageRef("a", 2, "uttrycklig")]
    assert stats["utanför"] == 3
