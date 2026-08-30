"""Migrerad logik från ``ocr.sh``.

Orkestrerar full OCR: Tesseract → Surya-fallback → WPU-Tesseract → Surya-
fallback → merge_wpu → redactions → normalize → quality → Surya-redo → quality.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path

import db as state_db
import merge_pages
import normalize_text

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


def _txt_has_content(path: Path) -> bool:
    """Returnera True när filen har icke-whitespace-innehåll (som gamla ocr.sh)."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    return bool(path.read_text(encoding="utf-8", errors="replace").strip())


def run_surya_fallback(label: str, pdf_dir: Path, options: OcrOptions, context: OperationContext) -> int:
    """Surya-fallback för filer där Tesseract misslyckats och text saknas."""
    conn = state_db.connect()
    state_db.init_schema(conn)
    candidates: list[tuple[str, Path]] = []
    for stem in state_db.list_surya_fallback_candidates(conn):
        pdf = pdf_dir / f"{stem}.pdf"
        txt = options.txt / f"{stem}.txt"
        if pdf.is_file() and not _txt_has_content(txt):
            candidates.append((stem, pdf))
    conn.close()

    if not candidates:
        context.log(f"Surya-fallback för Tesseract-fel ({label}): inga filer.")
        return 0

    context.log(f"Surya-fallback för Tesseract-fel ({label}): {len(candidates)} filer")
    failed = 0
    for i, (stem, pdf) in enumerate(candidates, 1):
        context.check_cancelled()
        context.progress(i, len(candidates), stem)
        argv = [
            sys.executable, str(ROOT / "src" / "ocr_pages.py"),
            "--in", str(pdf), "--out-dir", str(options.pages_out),
            "--engine", "surya", "--ocr-dir", str(options.ocr),
        ]
        if options.no_update_pdf:
            argv.append("--no-update-pdf")
        rc = context.run_process(argv, cwd=options.root)
        merge_pages.merge_one(stem, options.txt, create_missing=True)
        txt_file = options.txt / f"{stem}.txt"
        conn = state_db.connect()
        state_db.init_schema(conn)
        if rc == 0 and _txt_has_content(txt_file):
            normalize_text.process_file(txt_file)
            state_db.mark_normalized(conn, stem, text_mtime=txt_file.stat().st_mtime)
            state_db.clear_ocr_failures(conn, stem)
        else:
            state_db.mark_surya_failed(conn, stem)
            state_db.mark_tesseract_blacklisted(conn, stem)
            failed += 1
        conn.close()
    if failed:
        context.log(
            f"Surya-fallback ({label}): {failed} fil(er) misslyckades och "
            "blacklistades för vidare försök.",
            level="error",
        )
    # Per-fil-fel ska inte stoppa pipelinen — sammanfattningen ovan är resultatet
    # (gammal shell-semantik: kända misslyckanden hoppas över, steg exit 0).
    return 0


def run_ocr(options: OcrOptions, context: OperationContext | None = None) -> int:
    """Kör full OCR-pipeline. Returnerar exitkod."""
    ctx = ensure_terminal_context(context)

    if options.fallback_only:
        if not _surya_available():
            ctx.log("Surya är inte installerat.", level="error")
            return 1
        return run_surya_fallback("palmemordsarkivet", options.inp, options, ctx)

    if options.redo_only:
        return _run_redo(options, ctx)

    rc = 0
    tesseract_opts = TesseractOptions(
        root=options.root, inp=options.inp, ocr=options.ocr, txt=options.txt,
        jobs=options.jobs, per_file_jobs=options.per_file_jobs,
        retry_failed=options.retry_failed, files_from=options.files_from,
    )
    rc |= run_tesseract(tesseract_opts, ctx)

    if not options.skip_redo and _surya_available():
        rc |= run_surya_fallback("palmemordsarkivet", options.inp, options, ctx)

    wpu_dir = options.root / "downloaded" / "wpu_files"
    if wpu_dir.is_dir():
        wpu_opts = TesseractOptions(
            root=options.root, inp=wpu_dir, ocr=options.ocr, txt=options.txt,
            jobs=options.jobs, per_file_jobs=options.per_file_jobs,
            retry_failed=options.retry_failed,
        )
        rc |= run_tesseract(wpu_opts, ctx)
        if not options.skip_redo and _surya_available():
            rc |= run_surya_fallback("wpu.nu", wpu_dir, options, ctx)
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

    if not options.skip_redo and _surya_available():
        rc |= _run_redo(options, ctx)
        rc |= quality.run_quality(
            top=None, limit=None, per_page=True, text_dir=options.txt,
            files_dir=options.inp, rebuild=False, files_from=options.files_from,
            context=ctx,
        )

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
            pdf = options.inp / f"{stem}.pdf"
            if not pdf.exists():
                pdf = wpu_dir / f"{stem}.pdf"
            if not pdf.exists():
                continue
            pages_arg = ",".join(str(p) for p in sorted(set(pages)))
            argv = [
                sys.executable, str(ROOT / "src" / "ocr_pages.py"),
                "--in", str(pdf), "--out-dir", str(options.pages_out),
                "--engine", "surya", "--pages", pages_arg, "--ocr-dir", str(options.ocr),
            ]
            if options.no_update_pdf:
                argv.append("--no-update-pdf")
            if ctx.run_process(argv, cwd=options.root) == 0:
                merge_pages.merge_one(stem, options.txt)
                txt_file = options.txt / f"{stem}.txt"
                if txt_file.exists():
                    normalize_text.process_file(txt_file)
                    conn = state_db.connect()
                    state_db.init_schema(conn)
                    state_db.mark_normalized(conn, stem, text_mtime=txt_file.stat().st_mtime)
                    conn.close()
            else:
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

    if not options.skip_redo and _surya_available():
        rc |= _run_redo(replace(options, mode="pages"), ctx)
        rc |= quality.run_quality(
            top=None, limit=None, per_page=True, text_dir=options.txt,
            files_dir=options.inp, rebuild=False, files_from=lst, context=ctx,
        )
    return 1 if rc else 0
