"""Tester för källbundna LLM-förslag vid grafgranskning."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.review import audit_entries  # noqa: E402
from operations.exceptions import OperationFailed  # noqa: E402


def _items() -> list[dict]:
    entries = [{
        "pdf_stem": "A",
        "page_num": 7,
        "payload": {
            "entiteter": [
                {"typ": "person", "namn": "J"},
                {"typ": "person", "namn": "Olof Palme"},
            ],
            "relationer": [
                {"fran": "J", "typ": "såg", "till": "Olof Palme"},
            ],
        },
    }]
    return audit_entries(entries, [])


def _raw(item: dict, **changes) -> str:
    suggestion = {
        "item_key": item["item_key"],
        "action": "replace",
        "target": {"typ": "person", "namn": "Jan Andersson"},
        "evidence": "Jan Andersson såg Olof Palme",
        "motivation": "Förnamn och efternamn framgår uttryckligen.",
    }
    suggestion.update(changes)
    return json.dumps({"suggestions": [suggestion]}, ensure_ascii=False)


def test_parse_suggestions_accepts_source_bound_replacement() -> None:
    from graph.review_llm import parse_suggestions

    items = _items()
    result = parse_suggestions(
        _raw(items[0]),
        items,
        "Förhöret uppgav: Jan Andersson\n såg Olof Palme vid platsen.",
    )

    assert result == [{
        "item_key": items[0]["item_key"],
        "source_hash": items[0]["source_hash"],
        "action": "replace",
        "target": {"typ": "person", "namn": "Jan Andersson"},
        "evidence": "Jan Andersson såg Olof Palme",
        "note": "Förnamn och efternamn framgår uttryckligen.",
    }]


@pytest.mark.parametrize(
    "changes",
    [
        {"item_key": "påhittad-nyckel"},
        {"action": "merge"},
        {"evidence": "Anna Karlsson stod på Sveavägen"},
        {"evidence": "   "},
        {"motivation": ""},
        {"target": {"typ": "okänd", "namn": "Jan Andersson"}},
        {
            "target": {"typ": "person", "namn": "Påhittat Namn"},
            "evidence": "Olof Palme",
        },
        {
            "target": {"typ": "person", "namn": "Jan Andersson"},
            "evidence": "Olof Palme",
        },
    ],
)
def test_parse_suggestions_discards_unverifiable_proposals(changes: dict) -> None:
    from graph.review_llm import parse_suggestions

    items = _items()
    assert parse_suggestions(
        _raw(items[0], **changes),
        items,
        "Jan Andersson såg Olof Palme.",
    ) == []


def test_parse_suggestions_accepts_keep_with_cited_uncertainty() -> None:
    from graph.review_llm import parse_suggestions

    item = _items()[0]
    raw = _raw(
        item,
        action="keep",
        target={"ignoreras": "alltid"},
        evidence="Vittnet J kunde inte identifieras",
        motivation="Källan ger inte stöd för att utveckla initialen.",
    )

    assert parse_suggestions(
        raw, [item], "Vittnet J kunde inte identifieras närmare."
    )[0]["target"] == {}


def test_parse_suggestions_returns_empty_for_malformed_json() -> None:
    from graph.review_llm import parse_suggestions

    assert parse_suggestions("inget JSON", _items(), "sidtext") == []
    assert parse_suggestions('{"suggestions": [trasig', _items(), "sidtext") == []


def test_review_page_sends_full_source_and_identity_guard(monkeypatch) -> None:
    from graph import review_llm

    item = _items()[0]
    source = "UNIK INLEDNING " + ("mycket lång sidtext " * 500) + " UNIK AVSLUTNING"

    async def fake_call(prompt: str, cfg: dict) -> str:
        assert "UNIK INLEDNING" in prompt and "UNIK AVSLUTNING" in prompt
        assert "gissa" in prompt.casefold() and "identitet" in prompt.casefold()
        assert item["item_key"] in prompt
        assert cfg["kind"] == "openai"
        return _raw(
            item,
            action="keep",
            target={},
            evidence="UNIK INLEDNING",
            motivation="Ingen säker identifiering finns i sidan.",
        )

    monkeypatch.setattr(review_llm, "_call_llm", fake_call)
    result = asyncio.run(review_llm.review_page(
        [item], source, {"kind": "openai", "model": "test", "base_url": "", "api_key": ""}
    ))

    assert result[0]["action"] == "keep"


def test_run_llm_review_rejects_unknown_profile_before_database_work(monkeypatch) -> None:
    from graph import review_llm

    def unknown(_name: str) -> dict:
        raise ValueError("Okänd LLM-konfiguration 'Saknas'.")

    def forbidden_connect():
        raise AssertionError("Databasen får inte öppnas före profilvalideringen")

    monkeypatch.setattr(review_llm._llm_config, "load_profile", unknown)
    monkeypatch.setattr(review_llm.state_db, "connect", forbidden_connect)

    with pytest.raises(OperationFailed, match="Okänd LLM-konfiguration"):
        review_llm.run_llm_review(profile="Saknas")


def test_run_llm_review_rejects_missing_cloud_key_before_database_work(monkeypatch) -> None:
    from graph import review_llm

    monkeypatch.setattr(
        review_llm._llm_config,
        "load_profile",
        lambda _name: {
            "backend_name": "OpenAI",
            "model": "gpt-test",
            "api_key_env": "SAKNAD_TESTNYCKEL",
        },
    )
    monkeypatch.delenv("SAKNAD_TESTNYCKEL", raising=False)
    monkeypatch.setattr(
        review_llm.state_db,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("databasen får inte öppnas")),
    )

    with pytest.raises(OperationFailed, match="SAKNAD_TESTNYCKEL"):
        review_llm.run_llm_review(profile="Moln")


def test_run_llm_review_rejects_negative_limit_before_database_work(monkeypatch) -> None:
    from graph import review_llm

    monkeypatch.setattr(review_llm._llm_config, "load_profile", lambda _name: {})
    monkeypatch.setattr(
        review_llm.state_db,
        "connect",
        lambda: (_ for _ in ()).throw(AssertionError("databasen får inte öppnas")),
    )

    with pytest.raises(OperationFailed, match="negativ"):
        review_llm.run_llm_review(limit=-1)


def test_run_llm_review_limits_pages_and_persists_only_suggestions(tmp_path, monkeypatch) -> None:
    import db
    from graph import review_llm

    db_path = tmp_path / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    for stem in ("A", "B"):
        db.record_doc_entities(
            conn,
            pdf_stem=stem,
            page_num=1,
            payload={"entiteter": [{"typ": "person", "namn": "J"}], "relationer": []},
            model="extractor",
        )
        db.record_page(
            conn,
            pdf_stem=stem,
            page_num=1,
            engine="tesseract",
            text=f"I dokument {stem} omnämns vittnet J utan närmare identitet.",
            score=80,
        )
    conn.close()

    real_connect = db.connect
    monkeypatch.setattr(review_llm.state_db, "connect", lambda: real_connect(db_path))
    monkeypatch.setattr(
        review_llm._llm_config,
        "load_profile",
        lambda _name: {"backend_name": "Claude", "model": "test-model"},
    )

    async def fake_review(items: list[dict], source_text: str, cfg: dict) -> list[dict]:
        return [{
            "item_key": items[0]["item_key"],
            "source_hash": items[0]["source_hash"],
            "action": "keep",
            "target": {},
            "evidence": "vittnet J",
            "note": "Sidan ger ingen säker identifiering.",
        }]

    monkeypatch.setattr(review_llm, "review_page", fake_review)

    class Context:
        def __init__(self) -> None:
            self.progress_values = []
            self.logs = []

        def check_cancelled(self) -> None:
            pass

        def progress(self, current: int, total: int, message: str) -> None:
            self.progress_values.append((current, total, message))

        def log(self, message: str, **_kwargs) -> None:
            self.logs.append(message)

    context = Context()
    assert review_llm.run_llm_review(profile="Granskare", limit=1, context=context) == 0

    conn = real_connect(db_path)
    suggestions = db.list_graph_review_suggestions(conn)
    decisions = db.list_graph_review_decisions(conn)
    conn.close()
    assert len(suggestions) == 1
    assert suggestions[0]["profile"] == "Granskare"
    assert suggestions[0]["status"] == "pending"
    assert decisions == []
    assert context.progress_values == [(1, 1, "A sida 1")]
