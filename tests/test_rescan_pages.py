"""Tester för urvalet i operationen ``rescan-pages`` (massomskanning)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402
from operations.context import OperationContext  # noqa: E402
from operations.exceptions import OperationFailed  # noqa: E402


class _Sink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def write_log(self, message: str, level: str = "info") -> None:
        self.lines.append((level, message))

    def write_progress(self, update: object) -> None:
        return None

    def write_traceback(self, exc: BaseException) -> None:
        return None


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    (tmp_path / "downloaded" / "files").mkdir(parents=True)
    (tmp_path / "generated" / "text").mkdir(parents=True)
    return tmp_path


def _doc(root: Path, stem: str, scores: dict[int, float], *, pdf: bool = True,
         text: bool = True, source: str = "files") -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.mark_tesseract_done(conn, stem, pdf_path=f"{stem}.pdf", source=source)
    for page, score in scores.items():
        state_db.record_quality_page(conn, pdf_stem=stem, page_num=page, score=score)
    conn.close()
    if pdf:
        (root / "downloaded" / "files" / f"{stem}.pdf").write_bytes(b"%PDF")
    if text:
        (root / "generated" / "text" / f"{stem}.txt").write_text("x", encoding="utf-8")


def _read(stem: str, page: int, engine: str, identity: str | None = None) -> None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_page(conn, pdf_stem=stem, page_num=page, engine=engine, text="t",
                         score=20.0, engine_identity=identity)
    conn.close()


def _select(root: Path, *, engine: str = "surya", identity: str | None = None,
            threshold: float = 70, source: str = "any", max_pages: int = 0):
    from operations.rescan_pages import select_pages

    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: False, debug=True)
    result = select_pages(root, engine=engine, identity=identity, threshold=threshold,
                          source=source, max_pages=max_pages, ctx=ctx)
    return result, sink


def test_select_skips_duplicates_missing_pdfs_and_already_read(root) -> None:
    _doc(root, "vanlig", {1: 30})
    _doc(root, "dubblett", {1: 30}, text=False)
    _doc(root, "utan_pdf", {1: 30}, pdf=False)
    _doc(root, "last", {1: 30})
    _read("last", 1, "surya")

    result, sink = _select(root, engine="surya")

    assert result == {"vanlig": [1]}
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "dubblett" in debug and "utan_pdf" in debug and "last" in debug


def test_select_same_engine_twice_selects_nothing(root) -> None:
    _doc(root, "doc", {1: 20})
    assert _select(root, engine="surya-layout")[0] == {"doc": [1]}
    _read("doc", 1, "surya-layout")
    assert _select(root, engine="surya-layout")[0] == {}


def test_select_llm_vision_other_model_is_reselected(root) -> None:
    _doc(root, "doc", {1: 20})
    conn = state_db.connect()
    state_db.init_schema(conn)
    conn.execute(
        "INSERT INTO pdf_page_readings(pdf_stem, page_num, engine, engine_identity, "
        "text, reason, recorded_at) VALUES('doc',1,'llm-vision','llm-vision:A:m1','t',"
        "'rescan','2026-01-01')"
    )
    conn.commit()
    conn.close()

    assert _select(root, engine="llm-vision", identity="llm-vision:A:m1")[0] == {}
    assert _select(root, engine="llm-vision", identity="llm-vision:A:m2")[0] == {"doc": [1]}


def test_select_max_pages_takes_worst_first(root) -> None:
    _doc(root, "doc", {1: 60, 2: 20, 3: 40})
    assert _select(root, max_pages=2)[0] == {"doc": [2, 3]}


# --- Körningen (run_rescan_pages) --------------------------------------------

GOOD = "Protokoll fört vid förhör"
BAD = "x#&¤"


class _FakeOcr:
    """Ersätter ``ctx.run_process``: skriver sidornas nya läsning som ocr_pages gör.

    ``texts[stem][page]`` är den nya texten (saknas sidan skrivs ingen rad, som
    när ocr_pages misslyckas med den); ``rcs[stem]`` är exitkoden.
    """

    def __init__(self, texts: dict[str, dict[int, str]], rcs: dict[str, int] | None = None,
                 after=None) -> None:
        self.texts = texts
        self.rcs = rcs or {}
        self.after = after
        self.calls: list[list[str]] = []
        self.ctx: OperationContext | None = None

    def __call__(self, argv, *, cwd, env=None) -> int:
        argv = [str(a) for a in argv]
        if self.ctx is not None:
            self.ctx.check_cancelled()  # som ctx.run_process
        self.calls.append(argv)
        stem = Path(argv[argv.index("--in") + 1]).stem
        engine = argv[argv.index("--engine") + 1]
        pages = [int(p) for p in argv[argv.index("--pages") + 1].split(",")]
        assert "--force" in argv
        conn = state_db.connect()
        state_db.init_schema(conn)
        written = False
        for page in pages:
            text = self.texts.get(stem, {}).get(page)
            if text is None:
                continue
            written = True
            state_db.record_page(conn, pdf_stem=stem, page_num=page, engine=engine,
                                 text=text, score=50.0, markdown=None, layout_json=None,
                                 engine_identity=f"{engine}-test")
        conn.close()
        if self.after is not None:
            self.after(stem)
        return self.rcs.get(stem, 0 if written else 1)


def _pdf_doc(root: Path, stem: str, pages: dict[int, str], *, score: float = 30,
             rows: dict[int, str] | None = None) -> None:
    """Ett dokument med en riktig PDF, textfil, kvalitetsrader och valfria pdf_pages-rader."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in pages:
        doc.new_page(200, 300)
    doc.save(str(root / "downloaded" / "files" / f"{stem}.pdf"))
    (root / "generated" / "text" / f"{stem}.txt").write_text(
        "\f".join(pages[p] for p in sorted(pages)), encoding="utf-8")
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.mark_tesseract_done(conn, stem, pdf_path=f"{stem}.pdf", source="files")
    for page in pages:
        state_db.record_quality_page(conn, pdf_stem=stem, page_num=page, score=score)
    for page, text in (rows or {}).items():
        state_db.record_page(conn, pdf_stem=stem, page_num=page, engine="tesseract",
                             text=text, score=20.0)
    conn.close()


def _readings(stem: str | None = None) -> list[dict]:
    conn = state_db.connect()
    state_db.init_schema(conn)
    sql = "SELECT * FROM pdf_page_readings"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return [r for r in rows if stem is None or r["pdf_stem"] == stem]


def _row(stem: str, page: int) -> dict | None:
    conn = state_db.connect()
    state_db.init_schema(conn)
    row = state_db.get_page_row(conn, stem, page)
    conn.close()
    return dict(row) if row else None


def _file_page(root: Path, stem: str, page: int) -> str:
    text = (root / "generated" / "text" / f"{stem}.txt").read_text(encoding="utf-8")
    return text.split("\f")[page - 1]


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []


@pytest.fixture
def quality_calls(monkeypatch):
    """Ersätter quality.run_quality; sparar stammarna i files_from."""
    import quality

    rec = _Recorder()

    def fake(**kwargs):
        listed = Path(kwargs["files_from"]).read_text(encoding="utf-8").split()
        rec.calls.append({**kwargs, "listed": {n.removesuffix(".txt") for n in listed}})
        return 0

    monkeypatch.setattr(quality, "run_quality", fake)
    return rec


@pytest.fixture
def ingest_calls(monkeypatch):
    import operations.adapters as adapters

    rec = _Recorder()
    monkeypatch.setattr(adapters, "ingest_adapter",
                        lambda ctx, params: rec.calls.append(dict(params)))
    return rec


@pytest.fixture
def vision_profile(monkeypatch):
    import llm_vision

    profile = llm_vision.VisionProfile(name="Bild", model="m1", base_url="https://api.example.org/v1",
                                       api_key="hemlig", external=True)
    monkeypatch.setattr(llm_vision, "load_profile", lambda name: profile)
    monkeypatch.setattr(llm_vision, "check_profile", lambda p, client=None: None)
    return profile


def _scan(root: Path, fake: _FakeOcr, *, cancelled=lambda: False, **kwargs):
    from operations.rescan_pages import rescan_pages

    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=cancelled, debug=True)
    ctx.run_process = fake  # type: ignore[method-assign]
    fake.ctx = ctx
    params = {"root": root, "engine": "surya-layout", "profile": "", "threshold": 70,
              "source": "any", "max_pages": 0, "dry_run": False, "reindex": False,
              "jobs": 1, "dpi": 300}
    params.update(kwargs)
    summary = rescan_pages(**params, context=ctx)
    return summary, sink


def test_better_reading_is_kept(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD}, rows={1: BAD})
    fake = _FakeOcr({"doc": {1: GOOD}})

    summary, _ = _scan(root, fake)

    assert summary.improved == 1 and summary.kept_old == 0 and summary.failed == 0
    assert summary.exit_code == 0
    assert _row("doc", 1)["text"] == GOOD
    assert _file_page(root, "doc", 1) == GOOD
    assert [(r["reason"], r["text"]) for r in _readings()] == [("rescan", BAD)]


def test_worse_reading_is_rejected_and_restored(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: GOOD}, rows={1: GOOD})
    fake = _FakeOcr({"doc": {1: BAD}})
    before = (root / "generated" / "text" / "doc.txt").read_bytes()

    summary, sink = _scan(root, fake)

    assert summary.kept_old == 1 and summary.improved == 0
    row = _row("doc", 1)
    assert row["text"] == GOOD and row["engine"] == "tesseract"
    assert sorted((r["reason"], r["text"]) for r in _readings()) == [
        ("rejected", BAD), ("rescan", GOOD)]
    assert (root / "generated" / "text" / "doc.txt").read_bytes() == before
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "poäng 0.0 mot gammal tesseract poäng 91.8" in debug


def test_tie_keeps_old_reading(root, quality_calls) -> None:
    """Regression: lika jämförelsepoäng (t.ex. 0 mot 0 för en kodlista) räknades
    som förbättring och den nya läsningen ersatte den gamla utan belägg."""
    other_bad = "q@%§"
    from operations.page_readings import comparison_score

    assert comparison_score(other_bad) == comparison_score(BAD)
    _pdf_doc(root, "doc", {1: BAD}, rows={1: BAD})
    fake = _FakeOcr({"doc": {1: other_bad}})

    summary, sink = _scan(root, fake)

    assert summary.kept_old == 1 and summary.improved == 0
    assert _row("doc", 1)["text"] == BAD
    assert _file_page(root, "doc", 1) == BAD
    assert sorted((r["reason"], r["text"]) for r in _readings()) == [
        ("rejected", other_bad), ("rescan", BAD)]
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "lika poäng" in debug


def test_force_keeps_worse_new_reading(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: GOOD}, rows={1: GOOD})
    fake = _FakeOcr({"doc": {1: BAD}})

    summary, sink = _scan(root, fake, force=True)

    assert summary.improved == 1 and summary.kept_old == 0
    assert _row("doc", 1)["text"] == BAD
    assert _file_page(root, "doc", 1) == BAD
    # den gamla läsningen finns kvar i historiken och kan återställas
    assert [(r["reason"], r["text"]) for r in _readings()] == [("rescan", GOOD)]
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "force" in debug and "behåller ny" in debug


def test_force_still_skips_pages_read_with_the_engine(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD}, rows={1: BAD, 2: BAD})
    _read("doc", 1, "surya-layout")
    fake = _FakeOcr({"doc": {2: GOOD}})

    _scan(root, fake, force=True)

    assert _pages_scanned(fake) == [2]


def test_failed_page_keeps_old_and_continues(root, quality_calls) -> None:
    from operations.rescan_pages import run_rescan_pages

    _pdf_doc(root, "a", {1: BAD}, score=10, rows={1: BAD})
    _pdf_doc(root, "b", {1: BAD}, score=20, rows={1: BAD})
    fake = _FakeOcr({"b": {1: GOOD}})

    summary, _ = _scan(root, fake)

    assert len(fake.calls) == 2
    assert summary.failed == 1 and summary.improved == 1 and summary.exit_code != 0
    assert _readings("a") == []
    assert _row("a", 1)["text"] == BAD
    assert _row("b", 1)["text"] == GOOD

    # Wrappern ger samma exitkod (nu finns inget kvar att välja för b).
    _pdf_doc(root, "c", {1: BAD}, rows={1: BAD})
    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: False)
    ctx.run_process = _FakeOcr({})  # type: ignore[method-assign]
    rc = run_rescan_pages(root=root, engine="surya-layout", profile="", threshold=70,
                          source="any", max_pages=0, dry_run=False, reindex=False,
                          jobs=1, dpi=300, context=ctx)
    assert rc != 0


def test_surya_unavailable_aborts(root, quality_calls) -> None:
    from operations.exceptions import OperationFailed

    _pdf_doc(root, "a", {1: BAD}, score=10, rows={1: BAD})
    _pdf_doc(root, "b", {1: BAD}, score=20, rows={1: BAD})
    fake = _FakeOcr({}, rcs={"a": 3, "b": 3})

    with pytest.raises(OperationFailed, match="Surya"):
        _scan(root, fake)

    assert len(fake.calls) == 1
    assert _readings() == []


def test_five_failed_documents_in_a_row_abort(root, quality_calls) -> None:
    from operations.exceptions import OperationFailed

    for i in range(6):
        _pdf_doc(root, f"d{i}", {1: BAD}, score=10 + i, rows={1: BAD})
    fake = _FakeOcr({})

    with pytest.raises(OperationFailed, match="5"):
        _scan(root, fake, engine="vision")

    assert len(fake.calls) == 5
    assert _readings() == []


def test_llm_fatal_exit_aborts(root, quality_calls, vision_profile) -> None:
    from operations.exceptions import OperationFailed

    _pdf_doc(root, "a", {1: BAD}, rows={1: BAD})
    fake = _FakeOcr({}, rcs={"a": 4})

    with pytest.raises(OperationFailed, match="LLM"):
        _scan(root, fake, engine="llm-vision", profile="Bild")

    assert "--profile" in fake.calls[0]
    assert _readings() == []


def test_llm_fatal_exit_mid_document_counts_written_pages(
        root, quality_calls, ingest_calls, vision_profile) -> None:
    from operations.exceptions import OperationFailed
    from operations.rescan_pages import rescan_pages

    _pdf_doc(root, "a", {1: BAD, 2: BAD}, score=10, rows={1: BAD, 2: BAD})
    _pdf_doc(root, "b", {1: BAD}, score=20, rows={1: BAD})
    # Sidan 1 hinner läsas innan kvoten tar slut (rc 4); sidan 2 läses aldrig.
    fake = _FakeOcr({"a": {1: GOOD}}, rcs={"a": 4})
    sink = _Sink()
    ctx = OperationContext(sink=sink, cancel_requested=lambda: False, debug=True)
    ctx.run_process = fake  # type: ignore[method-assign]

    with pytest.raises(OperationFailed, match="LLM"):
        rescan_pages(root=root, engine="llm-vision", profile="Bild", threshold=70,
                     source="any", max_pages=0, dry_run=False, reindex=True, jobs=1,
                     dpi=300, context=ctx)

    assert len(fake.calls) == 1  # b skannas inte efter avbrottet
    assert _row("a", 1)["text"] == GOOD and _file_page(root, "a", 1) == GOOD
    assert _row("a", 2)["text"] == BAD
    assert [(r["page_num"], r["text"]) for r in _readings("a")] == [(1, BAD)]
    assert quality_calls.calls[-1]["listed"] == {"a"}
    assert ingest_calls.calls[-1]["only_stems"] == {"a"}
    info = "\n".join(m for lvl, m in sink.lines if lvl == "info")
    assert "Avbruten: Skanna om sidor: 3 valda, 2 skannade, 1 förbättrade" in info
    assert "1 misslyckades" in info


def test_llm_vision_logs_destination(root, quality_calls, vision_profile) -> None:
    _pdf_doc(root, "a", {1: BAD}, rows={1: BAD})
    fake = _FakeOcr({"a": {1: GOOD}})

    summary, sink = _scan(root, fake, engine="llm-vision", profile="Bild")

    assert summary.improved == 1
    info = "\n".join(m for lvl, m in sink.lines if lvl == "info")
    assert "sidbilder skickas till api.example.org (extern)" in info
    assert "hemlig" not in "\n".join(m for _, m in sink.lines)


def test_dry_run_writes_nothing(root, quality_calls, ingest_calls) -> None:
    _pdf_doc(root, "a", {1: BAD, 2: BAD}, rows={1: BAD})
    _pdf_doc(root, "b", {1: BAD})
    fake = _FakeOcr({"a": {1: GOOD, 2: GOOD}, "b": {1: GOOD}})

    summary, sink = _scan(root, fake, dry_run=True, reindex=True)

    assert fake.calls == []
    assert _readings() == []
    assert quality_calls.calls == [] and ingest_calls.calls == []
    assert summary.selected == 3
    info = "\n".join(m for lvl, m in sink.lines if lvl == "info")
    assert "3 sidor" in info and "2 dokument" in info


def test_quality_and_ingest_only_for_touched_documents(root, quality_calls, ingest_calls) -> None:
    _pdf_doc(root, "battre", {1: BAD}, score=10, rows={1: BAD})
    _pdf_doc(root, "samre", {1: GOOD}, score=20, rows={1: GOOD})
    _pdf_doc(root, "fel", {1: BAD}, score=30, rows={1: BAD})
    fake = _FakeOcr({"battre": {1: GOOD}, "samre": {1: BAD}})

    _scan(root, fake, reindex=False)

    assert len(quality_calls.calls) == 1
    call = quality_calls.calls[0]
    assert call["listed"] == {"battre", "samre"}
    assert call["per_page"] is True and call["rebuild"] is False
    assert ingest_calls.calls == []

    # Andra körningen med indexering: nya sidor att välja i nya dokument.
    _pdf_doc(root, "ny", {1: BAD}, score=5, rows={1: BAD})
    conn = state_db.connect()
    state_db.init_schema(conn)
    state_db.record_ingest(conn, pdf_stem="ny", text_mtime=1.0, chunks=1)
    conn.close()
    _scan(root, _FakeOcr({"ny": {1: GOOD}}), reindex=True)
    conn = state_db.connect()
    assert state_db.get_ingested_mtime(conn, "ny") is None
    conn.close()
    assert quality_calls.calls[-1]["listed"] == {"ny"}
    assert len(ingest_calls.calls) == 1
    assert ingest_calls.calls[0]["only_stems"] == {"ny"}
    assert ingest_calls.calls[0]["rebuild"] is False


def test_whole_file_winner_is_not_redone_by_full_ocr(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: GOOD})  # ingen pdf_pages-rad: hel-fils-OCR
    fake = _FakeOcr({"doc": {1: BAD}})

    summary, _ = _scan(root, fake)

    assert summary.kept_old == 1
    conn = state_db.connect()
    state_db.init_schema(conn)
    assert state_db.page_exists(conn, "doc", 1)
    conn.close()
    row = _row("doc", 1)
    assert row["engine"] == "hel-fils-OCR" and row["text"] == GOOD
    assert _file_page(root, "doc", 1) == GOOD
    # textfilsidan hade ingen egen poäng: jämförelsepoängen som avgjorde valet sparas
    from operations.page_readings import comparison_score

    assert row["score"] == pytest.approx(comparison_score(GOOD))


def test_cancel_during_parallel_llm_run_leaves_no_orphan_readings(
        root, quality_calls, vision_profile) -> None:
    import threading

    from operations.exceptions import OperationCancelled

    for i in range(4):
        _pdf_doc(root, f"d{i}", {1: BAD, 2: BAD}, score=10 + i, rows={1: BAD, 2: BAD})
    flag = threading.Event()
    fake = _FakeOcr({f"d{i}": {1: GOOD, 2: GOOD} for i in range(4)},
                    after=lambda stem: flag.set())

    with pytest.raises(OperationCancelled):
        _scan(root, fake, engine="llm-vision", profile="Bild", jobs=2,
              cancelled=flag.is_set)

    assert 1 <= len(fake.calls) <= 2
    readings = _readings()
    assert readings
    for reading in readings:
        row = _row(reading["pdf_stem"], reading["page_num"])
        assert row is not None and row["engine"] == "llm-vision", reading
    scanned = {Path(c[c.index("--in") + 1]).stem for c in fake.calls}
    assert {r["pdf_stem"] for r in readings} == scanned


def test_cancel_rescores_touched_documents_but_does_not_reindex(
        root, quality_calls, ingest_calls) -> None:
    from operations.exceptions import OperationCancelled

    _pdf_doc(root, "a", {1: BAD}, score=10, rows={1: BAD})
    _pdf_doc(root, "b", {1: BAD}, score=20, rows={1: BAD})
    cancelled = {"now": False}

    def cancel_after_first(stem: str) -> None:
        cancelled["now"] = True

    fake = _FakeOcr({"a": {1: GOOD}, "b": {1: GOOD}}, after=cancel_after_first)

    with pytest.raises(OperationCancelled):
        _scan(root, fake, reindex=True, cancelled=lambda: cancelled["now"])

    # "a" hann skannas och slås in i textfilen; "b" påbörjades aldrig
    assert [Path(c[c.index("--in") + 1]).stem for c in fake.calls] == ["a"]
    assert _file_page(root, "a", 1) == GOOD
    assert len(quality_calls.calls) == 1
    assert quality_calls.calls[0]["listed"] == {"a"}
    # det riktiga kvalitetssteget kontrollerar avbrott per fil: det får inte stoppa här
    quality_calls.calls[0]["context"].check_cancelled()
    assert ingest_calls.calls == []


# --- Dokumentläget (--pdf-stem) ----------------------------------------------

def _pages_scanned(fake: _FakeOcr) -> list[int]:
    return [int(p) for argv in fake.calls for p in argv[argv.index("--pages") + 1].split(",")]


def test_document_mode_selects_every_page_regardless_of_score(
        root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD, 3: BAD}, score=90)
    _read("doc", 1, "tesseract")
    _pdf_doc(root, "annat", {1: BAD}, score=5)
    fake = _FakeOcr({"doc": {1: GOOD, 2: GOOD, 3: GOOD}})

    summary, sink = _scan(root, fake, pdf_stem="doc", threshold=10)

    assert _pages_scanned(fake) == [1, 2, 3]
    assert all(Path(a[a.index("--in") + 1]).stem == "doc" for a in fake.calls)
    assert summary.selected == 3 and summary.improved == 3
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "dokumentläge" in debug


def test_document_mode_skips_pages_already_read_with_the_engine(
        root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD, 3: BAD})
    _read("doc", 1, "surya-layout")
    fake = _FakeOcr({"doc": {2: GOOD, 3: GOOD}})

    summary, sink = _scan(root, fake, pdf_stem="doc", max_pages=1)

    assert _pages_scanned(fake) == [2]
    assert summary.selected == 1
    debug = "\n".join(m for lvl, m in sink.lines if lvl == "debug")
    assert "redan läst" in debug and "doc s1" in debug


def test_document_mode_skips_rejected_readings_too(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD})
    fake = _FakeOcr({"doc": {1: BAD}})
    _scan(root, fake, pdf_stem="doc")      # lika poäng → underkänd, i historiken
    assert len(fake.calls) == 1

    summary, sink = _scan(root, fake, pdf_stem="doc")

    assert len(fake.calls) == 1 and summary.selected == 0
    assert any("redan lästs med surya-layout" in m for _lvl, m in sink.lines)


def test_document_mode_rereads_llm_vision_with_another_model(
        root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD})
    _read("doc", 1, "llm-vision", "llm-vision:Bild:gammal-modell")
    _read("doc", 2, "llm-vision", "llm-vision:Bild:ny-modell")
    from operations import rescan_pages as rp

    ctx = OperationContext(sink=_Sink(), cancel_requested=lambda: False, debug=True)
    chosen = rp.select_document_pages(root, "doc", max_pages=0, ctx=ctx,
                                      engine="llm-vision",
                                      identity="llm-vision:Bild:ny-modell")

    assert chosen == {"doc": [1]}


def test_document_mode_respects_max_pages(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD, 3: BAD})
    fake = _FakeOcr({"doc": {1: GOOD, 2: GOOD, 3: GOOD}})

    _scan(root, fake, pdf_stem="doc", max_pages=2)

    assert _pages_scanned(fake) == [1, 2]


def test_document_mode_rejects_removed_duplicate(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD})
    (root / "generated" / "text" / "doc.txt").unlink()
    fake = _FakeOcr({"doc": {1: GOOD}})

    with pytest.raises(OperationFailed, match="dubblett"):
        _scan(root, fake, pdf_stem="doc")

    assert fake.calls == [] and _readings() == []


def test_document_mode_rejects_missing_pdf(root, quality_calls) -> None:
    fake = _FakeOcr({})

    with pytest.raises(OperationFailed, match="PDF"):
        _scan(root, fake, pdf_stem="finns-inte")

    assert fake.calls == []


def test_document_mode_dry_run_writes_nothing(root, quality_calls) -> None:
    _pdf_doc(root, "doc", {1: BAD, 2: BAD, 3: BAD})
    fake = _FakeOcr({"doc": {1: GOOD, 2: GOOD, 3: GOOD}})

    summary, sink = _scan(root, fake, pdf_stem="doc", dry_run=True)

    assert summary.selected == 3 and fake.calls == [] and _readings() == []
    assert any("3 sidor i 1 dokument" in m for _lvl, m in sink.lines)
