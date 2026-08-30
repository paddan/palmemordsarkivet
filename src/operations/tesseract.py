"""Migrerad logik från ``ocr_tesseract.sh``.

Per-PDF-flödet: kontrollera state.db → extrahera råtext med ``pdftotext`` →
välja språk → kopiera gott textlager eller köra tre OCRmyPDF-försök → skriva
layout-text → markera done/failed + ``text_mtime`` i state.db.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import db as state_db

from .context import OperationContext, ensure_terminal_context

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class TesseractOptions:
    root: Path = ROOT
    inp: Path = ROOT / "downloaded" / "files"
    ocr: Path = ROOT / "generated" / "ocr"
    txt: Path = ROOT / "generated" / "text"
    tessdata: Path = ROOT / "tessdata"
    user_words: Path = ROOT / "tessdata" / "swe.user-words"
    user_words_auto: Path = ROOT / "tessdata" / "swe.user-words.auto"
    tess_config: Path = ROOT / "tessdata" / "tesseract.config"
    psm: int = 6
    langs: str = "swe"
    jobs: int = 4
    per_file_jobs: int = 2
    min_text_chars: int = 200
    image_dpi: int = 300
    errors_log: Path = ROOT / "generated" / "errors.log"
    files_from: Path | None = None
    retry_failed: bool = False
    retry_blacklist: bool = False

    def __post_init__(self) -> None:
        for name in (
            "inp", "ocr", "txt", "tessdata", "user_words", "user_words_auto",
            "tess_config", "errors_log", "files_from",
        ):
            value = getattr(self, name)
            if value is not None and value.is_relative_to(ROOT):
                setattr(self, name, self.root / value.relative_to(ROOT))


@dataclass
class TesseractResult:
    stem: str
    status: str
    error: str | None = None


def detect_language(text: str) -> str:
    """Returnera "eng", "swe" eller "swe+eng" för en text."""
    stripped = text.strip()
    if not stripped:
        return "swe+eng"
    try:
        from langdetect import detect_langs

        langs = {lang.lang: lang.prob for lang in detect_langs(stripped[:8000])}
        return "eng" if langs.get("en", 0) > 0.80 else "swe"
    except Exception:
        return "swe"


def text_quality_ok(text: str) -> bool:
    """Returnera True om ett befintligt textlager bedöms som användbart."""
    chars = len(text)
    if not chars:
        return False
    alnum = sum(1 for c in text if c.isalnum())
    tokens = text.split()
    if not tokens:
        return False
    short = sum(1 for w in tokens if len(w) <= 2)
    digit_in = sum(1 for w in tokens if re.search(r"\d", w) and re.search(r"[^\W\d_]", w))
    ar = alnum / chars
    sr = short / len(tokens)
    dr = digit_in / len(tokens)
    return ar >= 0.55 and sr <= 0.30 and dr <= 0.10


def build_ocr_attempts(mode: str, common: list[str]) -> list[list[str]]:
    """Returnera de tre OCRmyPDF-försöken (redo har bara två)."""
    if mode == "redo":
        return [
            [*common, "--clean", "--redo-ocr"],
            [*common, "--redo-ocr"],
        ]
    return [
        [*common, "--clean", "--skip-text", "--deskew"],
        [*common, "--skip-text", "--deskew"],
        [*common, "--skip-text"],
    ]


def _base_flags(options: TesseractOptions) -> list[str]:
    flags = [
        "--rotate-pages",
        "--tesseract-pagesegmode", str(options.psm),
        "--image-dpi", str(options.image_dpi),
    ]
    if options.user_words.is_file():
        flags += ["--user-words", str(options.user_words)]
    if options.user_words_auto.is_file():
        flags += ["--user-words", str(options.user_words_auto)]
    if options.tess_config.is_file():
        flags += ["--tesseract-config", str(options.tess_config)]
    return flags


def _run_ocrmypdf(
    ctx: OperationContext, options: TesseractOptions, flags: list[str], pdf: Path, out_pdf: Path
) -> bool:
    argv = [
        "ocrmypdf", "-l", options.langs, "--jobs", str(options.per_file_jobs),
        "--optimize", "0", "--quiet", *flags, str(pdf), str(out_pdf),
    ]
    # TESSDATA_PREFIX pekar ut tessdata-katalogen så swe_best.traineddata
    # används (gamla ocr_tesseract.sh exporterade samma variabel).
    return ctx.run_process(
        argv, cwd=options.root, env={"TESSDATA_PREFIX": str(options.tessdata)}
    ) == 0


def process_pdf(
    ctx: OperationContext,
    options: TesseractOptions,
    pdf: Path,
    *,
    conn=None,
) -> TesseractResult:
    """Bearbeta en PDF. Returnerar ett typat resultat."""
    own_conn = conn is None
    if own_conn:
        conn = state_db.connect()
        state_db.init_schema(conn)
    try:
        stem = pdf.stem
        out_pdf = options.ocr / f"{stem}.pdf"
        out_txt = options.txt / f"{stem}.txt"

        row = state_db.get_pdf_file(conn, stem)
        if row is not None and row["tesseract_done_at"] is not None:
            return TesseractResult(stem, "hoppar")
        if out_txt.is_file() and out_txt.stat().st_size > 0:
            state_db.mark_tesseract_done(conn, stem, pdf_path=str(pdf), source=state_db.source_for_path(pdf))
            if out_txt.is_file():
                state_db.touch_text_mtime(conn, stem, text_mtime=out_txt.stat().st_mtime)
            return TesseractResult(stem, "hoppar")
        if state_db.is_tesseract_blacklisted(conn, stem):
            return TesseractResult(stem, "blacklist-skip")
        if row is not None and row["tesseract_failed"]:
            return TesseractResult(stem, "fel-skip")

        raw_text = _pdftotext_raw(ctx, options, pdf)
        existing = len(re.sub(r"\s", "", raw_text))
        has_text = existing > options.min_text_chars
        langs = detect_language(raw_text) if has_text else "swe+eng"
        # Temporärt språkbyte för just denna fil.
        file_options = _with_langs(options, langs)

        status_parts: list[str] = []
        if langs != "swe":
            status_parts.append(f"lang:{langs}")

        if has_text:
            if text_quality_ok(raw_text):
                status_parts.append("text-finns")
                out_pdf.parent.mkdir(parents=True, exist_ok=True)
                out_pdf.write_bytes(pdf.read_bytes())
                _pdftotext_layout(ctx, options, pdf, out_txt)
            else:
                status_parts.append("text-skräp→redo")
                out_pdf.unlink(missing_ok=True)
                out_txt.unlink(missing_ok=True)
                if not _run_ocr(ctx, file_options, "redo", pdf, out_pdf):
                    state_db.mark_tesseract_failed(conn, stem, pdf_path=str(pdf), source=state_db.source_for_path(pdf))
                    return TesseractResult(stem, "fel", "ocrmypdf redo misslyckades")
                _pdftotext_layout(ctx, options, out_pdf, out_txt)
        else:
            status_parts.append("ocr")
            if not _run_ocr(ctx, file_options, "skip", pdf, out_pdf):
                state_db.mark_tesseract_failed(conn, stem, pdf_path=str(pdf), source=state_db.source_for_path(pdf))
                return TesseractResult(stem, "fel", "ocrmypdf misslyckades")
            _pdftotext_layout(ctx, options, out_pdf, out_txt)

        state_db.mark_tesseract_done(conn, stem, pdf_path=str(pdf), source=state_db.source_for_path(pdf))
        if out_txt.is_file():
            state_db.touch_text_mtime(conn, stem, text_mtime=out_txt.stat().st_mtime)
        return TesseractResult(stem, " ".join(status_parts) or "ok")
    finally:
        if own_conn:
            conn.close()


def _with_langs(options: TesseractOptions, langs: str) -> TesseractOptions:
    return TesseractOptions(**{**options.__dict__, "langs": langs})


def _pdftotext_raw(ctx: OperationContext, options: TesseractOptions, pdf: Path) -> str:
    # Läs pdftotext-utdata till en temporär fil via shell-fri subprocess.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        ctx.run_process(["pdftotext", "-q", str(pdf), str(tmp)], cwd=options.root)
        return tmp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        # Rensa tempfilen även vid fel — annars läcker NamedTemporaryFile
        # (delete=False tar inte bort filen automatiskt).
        tmp.unlink(missing_ok=True)


def _pdftotext_layout(ctx: OperationContext, options: TesseractOptions, pdf: Path, out_txt: Path) -> None:
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    ctx.run_process(["pdftotext", "-layout", str(pdf), str(out_txt)], cwd=options.root)


def _run_ocr(ctx: OperationContext, options: TesseractOptions, mode: str, pdf: Path, out_pdf: Path) -> bool:
    attempts = build_ocr_attempts(mode, _base_flags(options))
    return any(_run_ocrmypdf(ctx, options, flags, pdf, out_pdf) for flags in attempts)


def _list_candidates(options: TesseractOptions) -> list[Path]:
    if options.files_from is not None:
        stems: list[str] = []
        for line in options.files_from.read_text(encoding="utf-8").splitlines():
            stem = line.strip().removesuffix(".txt")
            if stem:
                stems.append(stem)
        pdfs = [options.inp / f"{stem}.pdf" for stem in stems]
        return [p for p in pdfs if p.is_file()]

    conn = state_db.connect()
    state_db.init_schema(conn)
    skip = state_db.list_tesseract_skip_stems(conn)
    conn.close()
    return [p for p in sorted(options.inp.glob("*.pdf")) if p.stem not in skip]


def run_tesseract(options: TesseractOptions, context: OperationContext | None = None) -> int:
    """Kör Tesseract-kön parallellt. Returnerar exitkod."""
    ctx = ensure_terminal_context(context)

    if options.retry_failed:
        conn = state_db.connect()
        state_db.init_schema(conn)
        n = state_db.clear_tesseract_failed(conn)
        conn.close()
        ctx.log(f"Nollställde {n} tesseract_failed-flaggor i state.db." if n else "Inga misslyckade OCR-jobb.")
    if options.retry_blacklist:
        conn = state_db.connect()
        state_db.init_schema(conn)
        n = state_db.retry_tesseract_blacklisted(conn)
        conn.close()
        ctx.log(f"Återaktiverade {n} blacklistade OCR-jobb." if n else "Inga blacklistade OCR-jobb.")

    options.ocr.mkdir(parents=True, exist_ok=True)
    options.txt.mkdir(parents=True, exist_ok=True)

    pdfs = _list_candidates(options)
    total = len(pdfs)
    if options.files_from is not None:
        ctx.log(f"Bearbetar {total} filer från {options.files_from}...")
    else:
        ctx.log(f"Hittade {total} PDF:er att bearbeta...")

    results: list[TesseractResult] = []
    done = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        futures = {pool.submit(process_pdf, ctx, options, pdf): pdf for pdf in pdfs}
        for fut in as_completed(futures):
            done += 1
            ctx.check_cancelled()
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001
                result = TesseractResult(futures[fut].stem, "fel", str(exc))
            if result.status == "fel":
                failed += 1
            ctx.progress(done, total, result.stem)
            results.append(result)

    ctx.log(f"Klart: {len(results)} filer bearbetade.")
    if failed:
        # Per-fil-fel får inte stoppa hela steget — gamla ocr_tesseract.sh
        # avslutade 0 med kända misslyckanden "hoppades över".
        ctx.log(f"{failed} fil(er) misslyckades och hoppas över.", level="error")
    return 0
