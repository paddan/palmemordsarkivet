"""Tester för konversationens spårlista (src/utredningsspar.py)."""

from __future__ import annotations

import utredningsspar as us

SPAR = {"title": "Vittnet vid Tunnelgatan", "why": "skäl", "question": "Q?",
        "sources": [{"source": "a.txt", "page": 2}, {"source": "påhittad.txt", "page": 1}]}


def test_belagg_som_inte_lasts_tas_bort() -> None:
    store: list[dict] = []
    ids = us.register_leads(store, [SPAR], turn=1, known_sources={"a"})
    assert ids == [1]
    assert store[0]["sources"] == [{"source": "a.txt", "page": 2}]
    assert store[0]["status"] == us.STATUS_FORESLAGEN and store[0]["turn"] == 1


def test_spar_utan_kvarvarande_belagg_visas_inte() -> None:
    store: list[dict] = []
    assert us.register_leads(store, [SPAR], turn=1, known_sources=set()) == []
    assert store == []


def test_samma_rubrik_ger_samma_spar() -> None:
    store: list[dict] = []
    us.register_leads(store, [SPAR], 1, {"a.txt"})
    us.mark_dug(store, 1)
    igen = dict(SPAR, title="  vittnet vid  TUNNELGATAN. ")
    assert us.register_leads(store, [igen], 3, {"a.txt"}) == [1]
    assert len(store) == 1 and store[0]["status"] == us.STATUS_GRAVD


def test_summary() -> None:
    store: list[dict] = []
    assert us.summary(store) == ""
    us.register_leads(store, [SPAR, dict(SPAR, title="Bilen i tre tips")], 1, {"a.txt"})
    us.mark_dug(store, 1)
    assert us.summary(store) == (
        "[Spår hittills i konversationen]\n"
        "1. (grävd) Vittnet vid Tunnelgatan\n"
        "2. (föreslagen) Bilen i tre tips"
    )
