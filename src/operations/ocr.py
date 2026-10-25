"""Migrerad logik från ``ocr.sh``.

Orkestrerar full OCR: Tesseract → Surya-fallback → WPU-Tesseract → Surya-
fallback → merge_wpu → redactions → normalize → quality → Surya-redo → quality.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import db as state_db
import merge_pages
import normalize_text
from surya_client import (
    EXIT_SURYA_UNAVAILABLE,
    SURYA_PYTHON_ENV,
    surya_layout_unavailable_reason,
    surya_python,
)

from .context import OperationContext, ensure_terminal_context
from .detect_redactions import RedactionsOptions, run_detect_redactions
from .tesseract import TesseractOptions, run_tesseract

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class OcrOptions:
    root: Path = ROOT
    inp: Path = ROOT / "downloaded" / "files"
    ocr: Path = ROOT / "generated" / "ocr"
    txt: Path = ROOT / "generated" / "text"
    pages_out: Path = ROOT / "generated" / "text_pages"
    threshold: float = 50.0
    skip_redo: bool = False
    fallback_only: bool = False
    redo_only: bool = False
    no_update_pdf: bool = False
    mode: str = "pages"
    source: str = "any"
    jobs: int = 4
    per_file_jobs: int = 2
    files_from: Path | None = None
    retry_failed: bool = False
    # "lines" = radbaserad Surya 0.17 (motorn surya), "layout" = Surya 2-layout
    # med Markdown i egen venv (motorn surya-layout). Standarden byts efter piloten.
    surya_engine: str = "lines"

    def __post_init__(self) -> None:
        for name in ("inp", "ocr", "txt", "pages_out", "files_from"):
            value = getattr(self, name)
            if value is not None and value.is_relative_to(ROOT):
                setattr(self, name, self.root / value.relative_to(ROOT))


def _surya_available() -> bool:
    try:
        import surya  # noqa: F401

        return True
    except Exception:
        return False


def _surya_engine_arg(options: OcrOptions) -> str:
    """Värdet till ``ocr_pages --engine`` för vald Surya-motor."""
    return "surya-layout" if options.surya_engine == "layout" else "surya"


def _surya_ready(options: OcrOptions, ctx: OperationContext) -> bool:
    """Om vald Surya-motor går att köra; en saknad runtime loggas med orsak.

    ``layout`` kontrolleras med ``surya_layout_unavailable_reason`` (tolken finns och
    workern svarar på ``--identity``), ``lines`` med att ``surya`` går att importera.
    """
    if options.surya_engine == "layout":
        python = surya_python()
        reason = surya_layout_unavailable_reason(python)
        if reason is None:
            ctx.log(f"[surya] motor surya-layout via {python}", level="debug")
            return True
        orsak = reason
        ctx.log(
            f"Surya 2-layout är inte tillgänglig: {orsak}. Installera worker-venv:en "
            f"eller peka {SURYA_PYTHON_ENV} på en Python med surya-ocr==0.22.1.",
            level="error",
        )
        return False
    if _surya_available():
        ctx.log("[surya] motor surya (radbaserad)", level="debug")
        return True
    return False


def _txt_has_content(path: Path) -> bool:
    """Returnera True när filen har icke-whitespace-innehåll (som gamla ocr.sh)."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    return bool(path.read_text(encoding="utf-8", errors="replace").strip())


def _page_count(stem: str) -> int:
    """Antal sidor för ``stem`` i pdf_pages (jämförs före/efter en ocr_pages-körning)."""
    conn = state_db.connect()
    state_db.init_schema(conn)
    try:
        return int(state_db.count_pages_for_stem(conn, stem))
    finally:
        conn.close()


def _pdf_page_count(pdf: Path, ctx: OperationContext) -> int | None:
    """Antal sidor i PDF:en via pypdfium2, eller ``None`` om den inte går att läsa."""
    try:
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(pdf))
    except Exception as exc:  # noqa: BLE001 - oläsbar PDF ska aldrig stoppa urvalet
        ctx.log(f"[surya] {pdf.name}: sidantalet gick inte att läsa ({exc})", level="debug")
        return None
    try:
        return len(doc)
    finally:
        doc.close()


def _fallback_reason(
    conn: sqlite3.Connection, stem: str, pdf: Path, txt: Path, ctx: OperationContext,
) -> str | None:
    """Varför ``stem`` ska köras i Surya-fallbacken, eller ``None`` om den inte ska.

    En fil utan text är kandidat som förut. En fil med text är kandidat när PDF:en
    har fler sidor än pdf_pages-raderna — en körning som stoppades på exitkod 3
    lämnar annars en tyst ofullständig fil (Ruling AC). ocr_pages hoppar redan
    sparade sidor, så omkörningen läser bara de som saknas. Utan några
    pdf_pages-rader kommer texten från Tesseract (ocrmypdf), inte från en avbruten
    Surya-läsning, och filen räknas inte som ofullständig.
    """
    if not pdf.is_file():
        return None
    if not _txt_has_content(txt):
        return "Tesseract gav ingen text — kör Surya på hela filen"
    pages = _pdf_page_count(pdf, ctx)
    if pages is None:
        return None
    rows = int(state_db.count_pages_for_stem(conn, stem))
    if rows and rows < pages:
        return (
            f"ofullständig: {rows} av {pages} sidor i pdf_pages — kör Surya på "
            "de sidor som saknas"
        )
    ctx.log(
        f"[surya] {stem}: har text, inte ofullständig — "
        + (f"{rows} pdf_pages-rader av {pages} PDF-sidor" if rows
           else f"0 rader av {pages} PDF-sidor = Tesseract-text"),
        level="debug",
    )
    return None


def _merge_and_normalize(stem: str, options: OcrOptions) -> bool:
    """Slå ihop sidtexten till ``text/<stem>.txt`` och normalisera; ``True`` om text finns."""
    merge_pages.merge_one(stem, options.txt, create_missing=True)
    txt_file = options.txt / f"{stem}.txt"
    if not _txt_has_content(txt_file):
        return False
    normalize_text.process_file(txt_file)
    conn = state_db.connect()
    state_db.init_schema(conn)
    try:
        state_db.mark_normalized(conn, stem, text_mtime=txt_file.stat().st_mtime)
    finally:
        conn.close()
    return True


def _log_surya_unavailable(step: str, stem: str, ctx: OperationContext) -> None:
    """En rad (error) när ocr_pages gav exitkod 3: miljöfel, steget stoppas utan markering."""
    ctx.log(
        f"{step}: Surya-runtimen gick inte att använda för {stem} (exitkod "
        f"{EXIT_SURYA_UNAVAILABLE}; felkoden står i ocr_pages-raden ovan) — steget "
        "stoppas och inga filer markeras som misslyckade.",
        level="error",
    )


def run_surya_fallback(label: str, pdf_dir: Path, options: OcrOptions, context: OperationContext) -> int:
    """Surya-fallback för filer där Tesseract misslyckats och text saknas.

    Kandidater är också ofullständiga filer: text finns, men PDF:en har fler sidor
    än pdf_pages-rader (en tidigare körning stoppades på exitkod 3).
    """
    conn = state_db.connect()
    state_db.init_schema(conn)
    candidates: list[tuple[str, Path]] = []
    reasons: dict[str, str] = {}
    for stem in state_db.list_surya_fallback_candidates(conn):
        pdf = pdf_dir / f"{stem}.pdf"
        txt = options.txt / f"{stem}.txt"
        reason = _fallback_reason(conn, stem, pdf, txt, context)
        if reason is not None:
            candidates.append((stem, pdf))
            reasons[stem] = reason
        else:
            context.log(
                f"[surya] {label}/{stem}: ingen kandidat "
                f"(pdf finns: {pdf.is_file()}, text finns: {_txt_has_content(txt)})",
                level="debug",
            )
    conn.close()

    if not candidates:
        context.log(f"Surya-fallback för Tesseract-fel ({label}): inga filer.")
        return 0

    context.log(f"Surya-fallback för Tesseract-fel ({label}): {len(candidates)} filer")
    for stem, _pdf in candidates:
        context.log(f"[surya] {label}/{stem}: {reasons[stem]}", level="debug")
    failed = 0
    for i, (stem, pdf) in enumerate(candidates, 1):
        context.check_cancelled()
        context.progress(i, len(candidates), stem)
        argv = [
            sys.executable, str(ROOT / "src" / "ocr_pages.py"),
            "--in", str(pdf), "--out-dir", str(options.pages_out),
            "--engine", _surya_engine_arg(options), "--ocr-dir", str(options.ocr),
        ]
        if options.no_update_pdf:
            argv.append("--no-update-pdf")
        before = _page_count(stem)
        rc = context.run_process(argv, cwd=options.root)
        new_pages = _page_count(stem) - before
        context.log(
            f"[surya] {label}/{stem}: ocr_pages rc={rc}, {new_pages} nya sidor i pdf_pages",
            level="debug",
        )
        if rc == EXIT_SURYA_UNAVAILABLE:
            # Miljöfel: sidor som hann läsas sparas, men filen markeras inte —
            # nästa körning (med fungerande runtime) försöker igen.
            if new_pages > 0:
                _merge_and_normalize(stem, options)
            _log_surya_unavailable(f"Surya-fallback ({label})", stem, context)
            _log_fallback_failures(label, failed, context)
            # Exitkod 3 vidare så att run_ocr hoppar resten av Surya-stegen (Ruling AD).
            return int(EXIT_SURYA_UNAVAILABLE)  # konstanten är Any för mypy (ej på mypy-sökvägen)
        if rc == 0 or new_pages > 0:
            has_text = _merge_and_normalize(stem, options)
        else:
            merge_pages.merge_one(stem, options.txt, create_missing=True)
            has_text = False
        conn = state_db.connect()
        state_db.init_schema(conn)
        if rc == 0 and has_text:
            state_db.clear_ocr_failures(conn, stem)
        else:
            state_db.mark_surya_failed(conn, stem)
            state_db.mark_tesseract_blacklisted(conn, stem)
            failed += 1
        conn.close()
    _log_fallback_failures(label, failed, context)
    # Per-fil-fel ska inte stoppa pipelinen — sammanfattningen ovan är resultatet
    # (gammal shell-semantik: kända misslyckanden hoppas över, steg exit 0).
    return 0


def _log_fallback_failures(label: str, failed: int, context: OperationContext) -> None:
    if failed:
        context.log(
            f"Surya-fallback ({label}): {failed} fil(er) misslyckades och "
            "blacklistades för vidare försök.",
            level="error",
        )


def run_ocr(options: OcrOptions, context: OperationContext | None = None) -> int:
    """Kör full OCR-pipeline. Returnerar exitkod."""
    ctx = ensure_terminal_context(context)

    if options.fallback_only:
        if not _surya_ready(options, ctx):
            if options.surya_engine != "layout":
                ctx.log("Surya är inte installerat.", level="error")
            return 1
        return run_surya_fallback("palmemordsarkivet", options.inp, options, ctx)

    if options.redo_only:
        # Surya-sidoredo med layoutmotorn kontrolleras en gång, som i hela kedjan
        # (filredo kör ocrmypdf, och --from-list kontrollerar själv).
        needs_layout = options.surya_engine == "layout" and options.mode == "pages"
        if needs_layout and not _surya_ready(options, ctx):
            return 1
        return _run_redo(options, ctx)

    rc = 0
    # Tillgängligheten kontrolleras en gång: för layout startar kontrollen en
    # kort worker-process (--identity), och svaret gäller hela körningen.
    surya_ok = not options.skip_redo and _surya_ready(options, ctx)
    tesseract_opts = TesseractOptions(
        root=options.root, inp=options.inp, ocr=options.ocr, txt=options.txt,
        jobs=options.jobs, per_file_jobs=options.per_file_jobs,
        retry_failed=options.retry_failed, files_from=options.files_from,
    )
    rc |= run_tesseract(tesseract_opts, ctx)

    # Ruling AD: stoppar ett Surya-steg på exitkod 3 hoppas resten av körningens
    # Surya-steg (wpu-fallback, redo) — en trasig runtime kostar ett försök per körning.
    surya_stopped: str | None = None

    def _surya_step(step: str, step_rc: int) -> int:
        nonlocal surya_stopped
        if step_rc == EXIT_SURYA_UNAVAILABLE and surya_stopped is None:
            surya_stopped = step
            ctx.log(
                f"{step} stoppade med exitkod {EXIT_SURYA_UNAVAILABLE} (Surya-runtimen "
                "otillgänglig) — resterande Surya-steg i körningen hoppas.",
                level="warning",
            )
        return step_rc

    if surya_ok:
        rc |= _surya_step(
            "Surya-fallback (palmemordsarkivet)",
            run_surya_fallback("palmemordsarkivet", options.inp, options, ctx),
        )

    wpu_dir = options.root / "downloaded" / "wpu_files"
    if wpu_dir.is_dir():
        wpu_opts = TesseractOptions(
            root=options.root, inp=wpu_dir, ocr=options.ocr, txt=options.txt,
            jobs=options.jobs, per_file_jobs=options.per_file_jobs,
            retry_failed=options.retry_failed,
        )
        rc |= run_tesseract(wpu_opts, ctx)
        if surya_ok and surya_stopped is None:
            rc |= _surya_step(
                "Surya-fallback (wpu.nu)",
                run_surya_fallback("wpu.nu", wpu_dir, options, ctx),
            )
        import merge_wpu

        rc |= merge_wpu.run_merge_wpu(
            dry_run=False, rebuild=False, margin=merge_wpu.DEFAULT_MARGIN,
            wpu_dir=wpu_dir, text_dir=options.txt, ocr_dir=options.ocr,
            jobs=options.jobs, context=ctx,
        )

    red_opts = RedactionsOptions(
        root=options.root, inp=options.inp, txt=options.txt, jobs=options.jobs,
        files_from=options.files_from,
    )
    rc |= run_detect_redactions(red_opts, ctx)

    # run_normalize returnerar antal ändrade filer — inte en exitkod.
    normalize_text.run_normalize(
        root=options.root, txt_dir=options.txt, dry_run=False, stats=False,
        rebuild=False, files_from=options.files_from, context=ctx,
    )

    import quality

    rc |= quality.run_quality(
        top=None, limit=None, per_page=True, text_dir=options.txt,
        files_dir=options.inp, rebuild=False, files_from=options.files_from,
        context=ctx,
    )

    if surya_ok and surya_stopped is None:
        rc |= _run_redo(options, ctx)
        rc |= quality.run_quality(
            top=None, limit=None, per_page=True, text_dir=options.txt,
            files_dir=options.inp, rebuild=False, files_from=options.files_from,
            context=ctx,
        )
    elif surya_stopped is None:
        # Ett stopp på exitkod 3 har redan loggats en gång av _surya_step.
        orsak = (
            "--skip-redo" if options.skip_redo
            else f"Surya-motorn {options.surya_engine!r} är inte tillgänglig"
        )
        ctx.log(f"[redo] Surya-omkörning hoppas över: {orsak}", level="debug")

    ctx.log("OCR-pipeline klar.")
    return 1 if rc else 0


def _run_redo(options: OcrOptions, ctx: OperationContext) -> int:
    """Surya-redo på dåliga sidor (mode pages) eller hela filer (mode files)."""
    if options.mode == "pages":
        conn = state_db.connect()
        state_db.init_schema(conn)
        wpu_dir = options.root / "downloaded" / "wpu_files"
        raw: dict[str, list[int]] = {}
        for row in state_db.list_redo_pages(conn, threshold=options.threshold):
            stem = row["pdf_stem"]
            if not state_db.page_exists(conn, stem, row["page_num"]):
                raw.setdefault(stem, []).append(row["page_num"])
        conn.close()

        if not raw:
            ctx.log(f"Inga nya dåliga sidor (THRESHOLD={options.threshold}).")
            return 0

        failed = 0
        for stem, pages in raw.items():
            ctx.check_cancelled()
            ctx.log(
                f"[redo] {stem}: {len(pages)} sidor under tröskel "
                f"{options.threshold}: {sorted(set(pages))[:20]}",
                level="debug",
            )
            pdf = options.inp / f"{stem}.pdf"
            if not pdf.exists():
                pdf = wpu_dir / f"{stem}.pdf"
            if not pdf.exists():
                ctx.log(f"[redo] {stem}: hoppar — PDF saknas i {options.inp} och {wpu_dir}",
                        level="debug")
                continue
            pages_arg = ",".join(str(p) for p in sorted(set(pages)))
            argv = [
                sys.executable, str(ROOT / "src" / "ocr_pages.py"),
                "--in", str(pdf), "--out-dir", str(options.pages_out),
                "--engine", _surya_engine_arg(options), "--pages", pages_arg,
                "--ocr-dir", str(options.ocr),
            ]
            if options.no_update_pdf:
                argv.append("--no-update-pdf")
            before = _page_count(stem)
            rc = ctx.run_process(argv, cwd=options.root)
            new_pages = _page_count(stem) - before
            ctx.log(f"[redo] {stem}: ocr_pages rc={rc}, {new_pages} nya sidor i pdf_pages",
                    level="debug")
            # Sidor som hann läsas före ett fel ska nå text/ och indexet ändå.
            if rc == 0 or new_pages > 0:
                _merge_and_normalize(stem, options)
            if rc == EXIT_SURYA_UNAVAILABLE:
                _log_surya_unavailable("Surya-sidoredo", stem, ctx)
                if failed:
                    ctx.log(f"Surya-sidoredo: {failed} fil(er) misslyckades.", level="error")
                return int(EXIT_SURYA_UNAVAILABLE)  # konstanten är Any för mypy (ej på mypy-sökvägen)
            if rc != 0:
                failed += 1
        if failed:
            ctx.log(f"Surya-sidoredo: {failed} fil(er) misslyckades.", level="error")
        return 0

    # mode files: --from-list nollställer state för listade stems och kör hela
    # kedjan; annars ocrmypdf --redo-ocr på hela filer från quality-tabellen.
    if options.files_from is not None:
        return _run_from_list_chain(options, ctx)

    conn = state_db.connect()
    state_db.init_schema(conn)
    targets = state_db.list_low_quality_stems(
        conn,
        threshold=options.threshold,
        source_type=None if options.source == "any" else options.source,
    )
    conn.close()

    failed = 0
    for base in targets:
        ctx.check_cancelled()
        pdf = options.inp / f"{base}.pdf"
        if not pdf.is_file():
            continue
        out_pdf = options.ocr / f"{base}.pdf"
        out_txt = options.txt / f"{base}.txt"
        out_pdf.unlink(missing_ok=True)
        out_txt.unlink(missing_ok=True)
        argv = [
            "ocrmypdf", "-l", "swe", "--redo-ocr", "--rotate-pages", "--clean",
            "--jobs", str(options.per_file_jobs), "--quiet", str(pdf), str(out_pdf),
        ]
        if ctx.run_process(argv, cwd=options.root) == 0:
            ctx.run_process(["pdftotext", "-layout", str(out_pdf), str(out_txt)], cwd=options.root)
            normalize_text.process_file(out_txt)
            conn = state_db.connect()
            state_db.init_schema(conn)
            state_db.touch_text_mtime(conn, base, text_mtime=out_txt.stat().st_mtime)
            conn.close()
        else:
            failed += 1
    if failed:
        ctx.log(f"Filredoing: {failed} fil(er) misslyckades.", level="error")
    return 0


def _run_from_list_chain(options: OcrOptions, ctx: OperationContext) -> int:
    """Nollställ state för stems i ``--from-list`` och kör hela kedjan för dem.

    Motsvarar gamla ``ocr.sh``-grenen med samma flagga: ta bort text/markörer,
    återställ tesseract-/surya-state i state.db och kör tesseract → redactions →
    normalize → quality → (Surya-sidoredo + quality).
    """
    lst = options.files_from
    assert lst is not None
    if not lst.is_file():
        ctx.log(f"Saknar --from-list-fil: {lst}", level="error")
        return 1
    stems = [
        line.strip().removesuffix(".txt")
        for line in lst.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not stems:
        ctx.log(f"Inga stems i --from-list-filen {lst}.", level="error")
        return 1

    conn = state_db.connect()
    state_db.init_schema(conn)
    try:
        for stem in stems:
            # Gamla ocr.sh tog bort .txt också — annars ser process_pdf en
            # befintlig text och hoppar över om-OCR trots reset i state.db.
            (options.txt / f"{stem}.txt").unlink(missing_ok=True)
            (options.txt / f"{stem}.redact").unlink(missing_ok=True)
            state_db.reset_pipeline_state_for_stem(conn, stem)
    finally:
        conn.close()
    ctx.log(f"Nollställde {len(stems)} filer — kör full kedja…")

    rc = 0
    tesseract_opts = TesseractOptions(
        root=options.root, inp=options.inp, ocr=options.ocr, txt=options.txt,
        jobs=options.jobs, per_file_jobs=options.per_file_jobs,
        retry_failed=options.retry_failed, files_from=lst,
    )
    rc |= run_tesseract(tesseract_opts, ctx)

    red_opts = RedactionsOptions(
        root=options.root, inp=options.inp, txt=options.txt,
        jobs=options.jobs, files_from=lst,
    )
    rc |= run_detect_redactions(red_opts, ctx)

    normalize_text.run_normalize(
        root=options.root, txt_dir=options.txt, dry_run=False, stats=False,
        rebuild=False, files_from=lst, context=ctx,
    )

    import quality

    rc |= quality.run_quality(
        top=None, limit=None, per_page=True, text_dir=options.txt,
        files_dir=options.inp, rebuild=False, files_from=lst, context=ctx,
    )

    if not options.skip_redo and _surya_ready(options, ctx):
        rc |= _run_redo(replace(options, mode="pages"), ctx)
        rc |= quality.run_quality(
            top=None, limit=None, per_page=True, text_dir=options.txt,
            files_dir=options.inp, rebuild=False, files_from=lst, context=ctx,
        )
    return 1 if rc else 0
