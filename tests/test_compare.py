"""Tester för vittnesjämförelsen (src/compare.py)."""

from __future__ import annotations

from compare import build_compare_prompt, group_hits_by_source


def _hits():
    return [
        {"nr": "100", "titel": "Förhör A", "page": 3, "source": "100 — A.txt",
         "text": "Han sprang norrut."},
        {"nr": "100", "titel": "Förhör A", "page": 4, "source": "100 — A.txt",
         "text": "Klockan var 23.20."},
        {"nr": "205", "titel": "Förhör B", "page": 1, "source": "205 — B.txt",
         "text": "Han sprang söderut."},
    ]


def test_group_hits_by_source_keeps_order_and_pages() -> None:
    groups = group_hits_by_source(_hits())
    assert [g["source"] for g in groups] == ["100 — A.txt", "205 — B.txt"]
    assert groups[0]["nr"] == "100"
    assert len(groups[0]["hits"]) == 2


def test_build_compare_prompt_includes_topic_and_source_labels() -> None:
    prompt = build_compare_prompt("vart sprang gärningsmannen", _hits())
    assert "vart sprang gärningsmannen" in prompt
    assert '[Nr 100, sida 3, "Förhör A"]' in prompt
    assert '[Nr 205, sida 1, "Förhör B"]' in prompt
    assert "Han sprang söderut." in prompt
