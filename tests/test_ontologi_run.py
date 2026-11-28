"""Tester för operationen extract-mordnatten (``ontologi.run``).

LLM, sökning och Neo4j ersätts alltid med fakes — inget test når nätet.
"""
from __future__ import annotations

import dataclasses
import json
from contextlib import closing
from pathlib import Path

import pytest
from neo4j.exceptions import ServiceUnavailable

import db
from ontologi import run as orun
from ontologi.config import Urval, load_config
from ontologi.llm import LlmFatal
from operations.context import OperationContext, ProgressSink
from operations.exceptions import OperationCancelled, OperationFailed

STAM = "Dok 1 — förhör"
_SIDOR = [
    f"Sida {n}. Vittnet berättade att hon gick längs Sveavägen vid Dekorima när "
    f"hon hörde två skott och såg en man springa mot Tunnelgatan. Förhöret nr {n}."
    for n in (1, 2, 3)
]


class RecordingSink(ProgressSink):
    def __init__(self) -> None:
        self.rader: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.rader.append((level, message))

    def write_progress(self, update) -> None:
        pass

    def write_traceback(self, exc: BaseException) -> None:
        pass

    def text(self) -> str:
        return "\n".join(m for _, m in self.rader)


def _ctx(sink: RecordingSink, cancel=lambda: False) -> OperationContext:
    return OperationContext(sink=sink, cancel_requested=cancel, debug=True)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


class _FakeSession:
    def __init__(self, reviewed):
        self.reviewed = reviewed

    def run(self, query, **params):
        return _Result([{"stem": s, "sida": n} for s, n in self.reviewed])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeDriver:
    def __init__(self, reviewed=(), down=False):
        self.reviewed = list(reviewed)
        self.down = down

    def verify_connectivity(self):
        if self.down:
            raise ServiceUnavailable("nere")

    def session(self):
        return _FakeSession(self.reviewed)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Spy:
    def __init__(self, svar=None, fel_vid: int | None = None):
        self.prompter: list[str] = []
        self.svar = svar or '{"observationer": []}'
        self.fel_vid = fel_vid

    async def __call__(self, prompt, system, cfg):
        self.prompter.append(prompt)
        if self.fel_vid is not None and len(self.prompter) == self.fel_vid:
            raise LlmFatal("HTTP 401")
        return self.svar, {"input_tokens": 100, "output_tokens": 20}


@pytest.fixture
def miljo(tmp_path: Path, monkeypatch):
    text_dir = tmp_path / "text"
    text_dir.mkdir()
    (text_dir / f"{STAM}.txt").write_text("\f".join(_SIDOR) + "\f", encoding="utf-8")
    cfg = dataclasses.replace(load_config(), urval=Urval((), (), (STAM,), (), 100))
    profil = {"backend_name": "OpenAI", "model": "gpt-test",
              "api_key_env": "PALME_TEST_ONTOLOGI_KEY",
              "input_price_usd": 1.0, "output_price_usd": 4.0}
    monkeypatch.setattr(orun._llm_config, "load_profile", lambda name: dict(profil))
    monkeypatch.setenv("PALME_TEST_ONTOLOGI_KEY", "hemlig-nyckel")
    laddat: list[list[dict]] = []

    def load(session, extractions, cfg, *, ctx):
        laddat.append(list(extractions))
        return {"sidor": len(extractions), "observationer": 0}

    spy = _Spy()
    deps = orun.Deps(
        llm=spy, search=lambda fragor, top_k: [],
        driver_factory=lambda uri, auth: _FakeDriver(),
        load=load, password=lambda: "pw", load_config=lambda: cfg,
    )
    return {"text_dir": text_dir, "deps": deps, "spy": spy, "laddat": laddat,
            "profil": profil}


def _kor(m, sink=None, cancel=lambda: False, **kw):
    sink = sink or RecordingSink()
    args = dict(profile="Test", max_pages=100, dry_run=False, force=False,
                load_only=False, jobs=2, uri="bolt://test:1", user="neo4j",
                text_dir=m["text_dir"])
    args.update(kw)
    rc = orun.run_extract_mordnatten(**args, context=_ctx(sink, cancel), deps=m["deps"])
    return rc, sink


def _rader() -> list[dict]:
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        return db.list_ontology_extractions(conn)


def test_missing_api_key_fails_before_any_call(miljo, monkeypatch) -> None:
    monkeypatch.delenv("PALME_TEST_ONTOLOGI_KEY")
    with pytest.raises(OperationFailed, match="PALME_TEST_ONTOLOGI_KEY"):
        _kor(miljo)
    assert miljo["spy"].prompter == []


def test_unknown_profile_fails_before_any_call(miljo, monkeypatch) -> None:
    def saknas(name):
        raise KeyError(f"Okänd profil {name!r}")

    monkeypatch.setattr(orun._llm_config, "load_profile", saknas)
    with pytest.raises(OperationFailed):
        _kor(miljo)
    assert miljo["spy"].prompter == []


def test_dry_run_writes_nothing_and_logs_cost(miljo) -> None:
    rc, sink = _kor(miljo, dry_run=True)
    assert rc == 0
    assert miljo["spy"].prompter == []
    assert _rader() == []
    assert miljo["laddat"] == []
    text = sink.text()
    assert "3 sidor" in text
    assert "uttrycklig" in text
    assert "kostnad" in text.lower()
    assert "$" in text


def test_dry_run_without_prices_says_cost_unknown(miljo) -> None:
    for nyckel in ("input_price_usd", "output_price_usd"):
        miljo["profil"].pop(nyckel)
    rc, sink = _kor(miljo, dry_run=True)
    assert rc == 0
    assert "kostnad okänd" in sink.text()


def test_each_answer_is_saved_before_load(miljo) -> None:
    rc, sink = _kor(miljo)
    assert rc == 0
    rader = _rader()
    assert [(r["pdf_stem"], r["sida"]) for r in rader] == [(STAM, 1), (STAM, 2), (STAM, 3)]
    assert all(r["profile"] == "Test" and r["model"] == "gpt-test" for r in rader)
    assert len(miljo["laddat"]) == 1 and len(miljo["laddat"][0]) == 3
    assert "Mordnatten: 3 sidor extraherade (300 token in, 60 ut)" in sink.text()
    assert "hemlig-nyckel" not in sink.text()
    assert _SIDOR[0] not in sink.text()


def test_unchanged_pages_are_not_reextracted(miljo) -> None:
    _kor(miljo)
    miljo["spy"].prompter.clear()
    rc, sink = _kor(miljo)
    assert rc == 0
    assert miljo["spy"].prompter == []


def test_fatal_llm_error_stops_and_keeps_saved(miljo) -> None:
    miljo["deps"].llm = _Spy(fel_vid=2)
    with pytest.raises(OperationFailed):
        _kor(miljo, jobs=1)
    rader = _rader()
    assert [(r["pdf_stem"], r["sida"]) for r in rader] == [(STAM, 1)]
    assert len(miljo["deps"].llm.prompter) == 2
    # Det som hann sparas läses ändå in i grafen.
    assert len(miljo["laddat"]) == 1 and len(miljo["laddat"][0]) == 1


def test_neo4j_down_keeps_raw_and_returns_zero(miljo) -> None:
    def load(session, extractions, cfg, *, ctx):
        raise ServiceUnavailable("nere")

    miljo["deps"].load = load
    rc, sink = _kor(miljo)
    assert rc == 0
    assert len(_rader()) == 3
    varningar = [m for lvl, m in sink.rader if lvl == "warning"]
    assert any("--load-only" in m for m in varningar)


def test_reviewed_unreachable_neo4j_is_a_warning(miljo) -> None:
    miljo["deps"].driver_factory = lambda uri, auth: _FakeDriver(down=True)
    rc, sink = _kor(miljo, dry_run=True)
    assert rc == 0
    assert any(lvl == "warning" for lvl, _ in sink.rader)


def test_load_only_skips_llm(miljo, monkeypatch) -> None:
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        db.upsert_ontology_extraction(conn, {
            "pdf_stem": STAM, "sida": 1, "text_hash": "h", "schemaversion": 1,
            "profile": "Test", "model": "m", "raw": "{}",
            "parsed": {"observationer": []}, "n_observationer": 0,
        })

    def ingen_profil(name):
        raise AssertionError("profilen ska inte lösas vid --load-only")

    monkeypatch.setattr(orun._llm_config, "load_profile", ingen_profil)
    rc, _ = _kor(miljo, load_only=True)
    assert rc == 0
    assert miljo["spy"].prompter == []
    assert len(miljo["laddat"]) == 1 and len(miljo["laddat"][0]) == 1


def test_cancellation_loads_saved_then_reraises(miljo) -> None:
    spy = miljo["spy"]
    with pytest.raises(OperationCancelled):
        _kor(miljo, jobs=1, cancel=lambda: len(spy.prompter) >= 1)
    assert len(_rader()) == 1
    assert len(miljo["laddat"]) == 1 and len(miljo["laddat"][0]) == 1


def test_search_hits_are_selected(miljo) -> None:
    cfg = miljo["deps"].load_config()
    cfg = dataclasses.replace(cfg, urval=Urval((), ("skotten",), (), (), 100))
    miljo["deps"].load_config = lambda: cfg
    fragor: list = []

    def search(q, top_k):
        fragor.append((tuple(q), top_k))
        return [(STAM, 2)]

    miljo["deps"].search = search
    rc, sink = _kor(miljo, dry_run=True)
    assert rc == 0
    assert fragor == [(("skotten",), 20)]
    assert "1 sidor" in sink.text()


def test_parse_warnings_are_grouped(miljo) -> None:
    obs = {"tid_text": None, "tid_fran": None, "tid_till": None, "plats": None,
           "handelse_id": None, "vittne": {"namn": None, "maskad": False},
           "observerade": [], "utdrag": "detta citat står inte alls på sidan någonstans",
           "osakerhet": None}
    miljo["deps"].llm = _Spy(svar=json.dumps({"observationer": [obs]}))
    rc, sink = _kor(miljo)
    assert rc == 0
    assert "3 varningar" in sink.text()
    debug = [m for lvl, m in sink.rader if lvl == "debug"]
    assert any("×3" in m or "3 st" in m for m in debug)


class _Felande:
    """Svarar normalt utom vid anrop nr ``vid``, där ``fel`` höjs eller ``svar`` ges."""

    def __init__(self, vid: int, fel: BaseException | None = None, svar: str | None = None):
        self.vid, self.fel, self.svar = vid, fel, svar
        self.prompter: list[str] = []

    async def __call__(self, prompt, system, cfg):
        self.prompter.append(prompt)
        if len(self.prompter) == self.vid:
            if self.fel is not None:
                raise self.fel
            return self.svar, {"input_tokens": 1, "output_tokens": 1}
        return '{"observationer": []}', {"input_tokens": 100, "output_tokens": 20}


@pytest.fixture
def fellogg(monkeypatch):
    rader: list[tuple[str, str, str]] = []
    monkeypatch.setattr(orun.errors_log, "log_error",
                        lambda comp, item, msg: rader.append((comp, item, msg)))
    return rader


@pytest.mark.parametrize("svar", ["inget json alls", '{"annat": 1}'])
def test_invalid_answer_is_a_failed_page_not_saved(miljo, fellogg, svar) -> None:
    miljo["deps"].llm = _Felande(vid=2, svar=svar)
    rc, sink = _kor(miljo, jobs=1)
    assert rc == 0
    assert [(r["pdf_stem"], r["sida"]) for r in _rader()] == [(STAM, 1), (STAM, 3)]
    assert any(item == f"{STAM} s2" for _, item, _ in fellogg)
    assert "1 sidor misslyckades" in sink.text()


def test_truncated_answer_is_a_failed_page(miljo, fellogg) -> None:
    from ontologi.llm import LlmPageError

    miljo["deps"].llm = _Felande(vid=1, fel=LlmPageError("svaret avklippt (length)"))
    rc, _ = _kor(miljo, jobs=1)
    assert rc == 0
    assert [(r["pdf_stem"], r["sida"]) for r in _rader()] == [(STAM, 2), (STAM, 3)]
    assert any("length" in msg for _, _, msg in fellogg)


def test_valid_empty_answer_is_saved(miljo) -> None:
    rc, _ = _kor(miljo)
    assert rc == 0
    assert all(r["n_observationer"] == 0 for r in _rader()) and len(_rader()) == 3


def test_unexpected_page_error_does_not_stop_the_run(miljo, fellogg) -> None:
    miljo["deps"].llm = _Felande(vid=2, fel=RuntimeError("bugg"))
    rc, sink = _kor(miljo, jobs=1)
    assert rc == 0
    assert [(r["pdf_stem"], r["sida"]) for r in _rader()] == [(STAM, 1), (STAM, 3)]
    assert any(lvl == "error" and "RuntimeError" in m for lvl, m in sink.rader)
    assert any(item == f"{STAM} s2" and "RuntimeError" in msg for _, item, msg in fellogg)
    assert len(miljo["laddat"]) == 1 and len(miljo["laddat"][0]) == 2


def test_fatal_llm_error_is_written_to_errors_log(miljo, fellogg) -> None:
    miljo["deps"].llm = _Spy(fel_vid=1)
    with pytest.raises(OperationFailed):
        _kor(miljo, jobs=1)
    assert any("401" in msg for _, _, msg in fellogg)


def _gammal_rad(sida: int) -> None:
    from ontologi.extraction import text_hash

    with closing(db.connect()) as conn:
        db.init_schema(conn)
        db.upsert_ontology_extraction(conn, {
            "pdf_stem": STAM, "sida": sida, "text_hash": text_hash(_SIDOR[sida - 1]),
            "schemaversion": 0, "profile": "Test", "model": "m", "raw": "{}",
            "parsed": {"observationer": []}, "n_observationer": 0,
        })


@pytest.mark.parametrize("nere", ["driver", "losenord"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_unreadable_graph_protects_extracted_pages(miljo, nere, dry_run) -> None:
    _gammal_rad(1)
    if nere == "driver":
        miljo["deps"].driver_factory = lambda uri, auth: _FakeDriver(down=True)
    else:
        miljo["deps"].password = lambda: None
    rc, sink = _kor(miljo, jobs=1, dry_run=dry_run)
    assert rc == 0
    varningar = [m for lvl, m in sink.rader if lvl == "warning"]
    assert any("behandlas som granskade" in m for m in varningar)
    if dry_run:
        assert "granskad 1" in sink.text()
    else:
        assert len(miljo["spy"].prompter) == 2
        assert all("Sida 1." not in p for p in miljo["spy"].prompter)


def test_draft_events_warn(miljo) -> None:
    for dry in (True, False):
        _, sink = _kor(miljo, dry_run=dry)
        assert any(lvl == "warning" and "Ankarhändelserna är ett utkast" in m
                   for lvl, m in sink.rader)
    cfg = dataclasses.replace(miljo["deps"].load_config(), handelser_utkast=False)
    miljo["deps"].load_config = lambda: cfg
    _, sink = _kor(miljo, dry_run=True)
    assert not any("utkast" in m for _, m in sink.rader)


def test_default_profile_is_named_in_a_warning(miljo, monkeypatch) -> None:
    monkeypatch.setattr(orun._llm_config, "load_all", lambda: {
        "default": "Standardprofil", "profiles": {"Standardprofil": dict(miljo["profil"])}})
    rc, sink = _kor(miljo, profile="")
    assert rc == 0
    assert any(lvl == "warning" and "Standardprofil" in m for lvl, m in sink.rader)
    _, sink = _kor(miljo, profile="Test", force=True)
    assert not any("standardprofil" in m.lower() for lvl, m in sink.rader
                   if lvl == "warning")


def test_rag_dir_goes_first_so_ingest_resolves_to_rag_ingest(monkeypatch):
    """Körd via scripts/ skuggade scripts/ingest.py modulen som rag.ask importerar."""
    import importlib.util
    import sys

    from ontologi import run as run_mod

    scripts_dir = str(run_mod.RAG_DIR.parents[1] / "scripts")
    monkeypatch.setattr(sys, "path", [scripts_dir, *sys.path])
    run_mod._ensure_rag_path()
    assert sys.path[0] == str(run_mod.RAG_DIR)
    spec = importlib.util.find_spec("ingest")
    assert spec is not None and spec.origin == str(run_mod.RAG_DIR / "ingest.py")
    run_mod._ensure_rag_path()  # idempotent
    assert sys.path.count(str(run_mod.RAG_DIR)) >= 1 and sys.path[1] != str(run_mod.RAG_DIR)
