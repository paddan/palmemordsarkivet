"""Ren granskning utan databas eller ändring av källdata."""
from copy import deepcopy

from graph.review import audit_entries, build_reviewed_rows, preview_name_rule


def entry(stem="A", names=None, relations=None):
    return {"pdf_stem": stem, "page_num": 1, "payload": {
        "entiteter": names if names is not None else [
            {"namn": "J", "typ": "person"}, {"namn": "Olof Palme", "typ": "person"}],
        "relationer": relations if relations is not None else [
            {"fran": "J", "typ": "såg", "till": "Olof Palme"}]}}


def decision(item, action, target=None):
    return {"item_key": item["item_key"], "source_hash": item["source_hash"],
            "action": action, "target": target or {}, "note": "Kontrollerat källan"}


def test_flags_initials_and_retains_originals():
    rows = [entry(names=[{"namn": n, "typ": "person"} for n in ["J", "MJ", "BeBe"]], relations=[])]
    original = deepcopy(rows)
    audit = audit_entries(rows, [])
    assert all(x["issues"] for x in audit)
    mentions, _ = build_reviewed_rows(rows, [])
    assert len(mentions) == 3
    assert rows == original


def test_rename_is_occurrence_scoped_and_moves_relation():
    rows = [entry(), entry("B")]
    d = decision(audit_entries(rows, [])[0], "replace", {"namn": "Jan Andersson", "typ": "person"})
    mentions, relations = build_reviewed_rows(rows, [d])
    assert [r["fran_norm"] for r in relations] == ["jan andersson", "j"]
    assert mentions[0]["namn"] == "Jan Andersson"


def test_stale_page_decision_is_not_applied():
    rows = [entry()]
    item = audit_entries(rows, [])[0]
    d = decision(item, "exclude")
    rows[0]["payload"]["relationer"][0]["typ"] = "mötte"
    updated = audit_entries(rows, [d])[0]
    assert updated["item_key"] == item["item_key"]
    assert updated["stale"] and updated["decision"] == d
    assert len(build_reviewed_rows(rows, [d])[0]) == 2


def test_excluded_entity_cannot_be_recreated_by_relation_replacement():
    rows = [entry()]
    audit = audit_entries(rows, [])
    decisions = [decision(audit[0], "exclude"), decision(audit[2], "replace", audit[2]["original"])]
    mentions, relations = build_reviewed_rows(rows, decisions)
    assert len(mentions) == 1
    assert relations == []


def test_ambiguous_labels_never_choose_endpoint():
    rows = [entry(names=[{"namn": "J", "typ": "person"}, {"namn": "J", "typ": "plats"},
                         {"namn": "Olof Palme", "typ": "person"}])]
    assert audit_entries(rows, [])[-1]["issues"]
    assert build_reviewed_rows(rows, [])[1] == []


def test_detects_bad_entities_relations_and_duplicates():
    rows = [entry(names=[{"namn": "[MASKAD]", "typ": "person"}] * 2,
                  relations=[{"fran": "saknas", "till": "saknas", "typ": ""}] * 2)]
    audit = audit_entries(rows, [])
    assert all(x["issues"] for x in audit)
    assert len(audit[-1]["issues"]) >= 4


def test_relation_replacement_resolves_corrected_entities():
    rows = [entry()]
    audit = audit_entries(rows, [])
    ds = [decision(audit[0], "replace", {"namn": "Jan Andersson", "typ": "person"}),
          decision(audit[2], "replace", {"fran": "Jan Andersson", "typ": "mötte", "till": "Olof Palme"})]
    assert build_reviewed_rows(rows, ds)[1][0]["typ"] == "mötte"


def test_decisions_never_mutate_input_and_do_not_cross_pages():
    first = entry()
    second = entry()
    second["page_num"] = 2
    rows = [first, second]
    original = deepcopy(rows)
    d = decision(audit_entries(rows, [])[0], "exclude")
    mentions, relations = build_reviewed_rows(rows, [d])
    assert rows == original
    assert len(mentions) == 3
    assert [r["sida"] for r in relations] == [2]


def test_relation_exclusion_and_keep_are_explicit():
    rows = [entry()]
    audit = audit_entries(rows, [])
    mentions, relations = build_reviewed_rows(rows, [decision(audit[0], "keep"),
                                                  decision(audit[-1], "exclude")])
    assert len(mentions) == 2
    assert relations == []


def test_unreviewed_surnames_resolve_within_document_only():
    rows = [entry(names=[{"namn": "Stig Engström", "typ": "person"}], relations=[]),
            entry(names=[{"namn": "Engström", "typ": "person"}], relations=[]),
            entry("B", names=[{"namn": "Engström", "typ": "person"}], relations=[])]
    rows[1]["page_num"] = 2
    assert [m["norm"] for m in build_reviewed_rows(rows, [])[0]] == [
        "stig engström", "stig engström", "engström"]
    audit = audit_entries(rows, [])
    ds = [decision(audit[0], "exclude")]
    assert [m["norm"] for m in build_reviewed_rows(rows, ds)[0]] == ["engström", "engström"]
    ds = [decision(audit[1], "keep")]
    assert build_reviewed_rows(rows, ds)[0][1]["norm"] == "engström"


def test_decision_validation_rejects_bad_targets_and_blank_reason():
    import pytest

    from graph.review import validate_decision

    item = audit_entries([entry()], [])[0]
    for action, target, note in [("invalid", {}, "x"), ("keep", {}, " "),
                                 ("replace", {"namn": "", "typ": "person"}, "x"),
                                 ("replace", {"namn": "Jan", "typ": "other"}, "x")]:
        with pytest.raises(ValueError):
            validate_decision(item, action, target, note)
    validate_decision(item, "replace", {"namn": "Jan", "typ": "person"}, "Källa")
    validate_decision(item, "reset", {}, "Återställ")
    relation = audit_entries([entry()], [])[-1]
    with pytest.raises(ValueError):
        validate_decision(relation, "replace", {"fran": "J", "typ": "", "till": "Olof Palme"}, "x")


def test_global_name_rule_canonicalizes_entities_and_relation_endpoints():
    rows = [entry(names=[
        {"namn": "RKA2", "typ": "organisation"},
        {"namn": "Olof Palme", "typ": "person"},
    ], relations=[{"fran": "RKA2", "typ": "utredde", "till": "Olof Palme"}])]
    original = deepcopy(rows)

    mentions, relations = build_reviewed_rows(rows, [], [{
        "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
    }])

    assert {row["namn"] for row in mentions} == {"Rikskriminalen A2", "Olof Palme"}
    assert relations[0]["fran_norm"] == "rikskriminalen a2"
    assert rows == original


def test_explicit_per_occurrence_replacement_overrides_global_name_rule():
    rows = [entry(names=[
        {"namn": "RKA2", "typ": "organisation"},
        {"namn": "Olof Palme", "typ": "person"},
    ], relations=[{"fran": "RKA2", "typ": "utredde", "till": "Olof Palme"}])]
    audit = audit_entries(rows, [])
    manual = decision(audit[0], "replace", {"namn": "Säkerhetspolisen", "typ": "organisation"})

    mentions, relations = build_reviewed_rows(rows, [manual], [{
        "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
    }])

    assert {row["namn"] for row in mentions} == {"Säkerhetspolisen", "Olof Palme"}
    assert relations[0]["fran_norm"] == "säkerhetspolisen"


def test_global_name_rule_preview_counts_affected_entities_pages_and_relations():
    rows = [entry(names=[
        {"namn": "RKA2", "typ": "organisation"},
        {"namn": "Olof Palme", "typ": "person"},
    ], relations=[{"fran": "RKA2", "typ": "utredde", "till": "Olof Palme"}])]

    assert preview_name_rule(rows, [], [], {
        "typ": "organisation", "source": "RKA2", "target": "Rikskriminalen A2",
    }) == {"entities": 1, "pages": 1, "relations": 1}
