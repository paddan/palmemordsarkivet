"""Migrerad logik från ``detect_redactions.sh``.

Detekterar maskeringsblock i PDF:er och infogar ``[MASKAD]`` i OCR-text.
Idempotens spåras i ``pdf_files.redaction_checked_at``; kandidater förfiltreras
från state.db så redan kontrollerade filer inte spawnar processer.
"""

from __future__ import annotations

from concurrent.futures import Executor, ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

import db as state_db
import ocr_pages

from .context import OperationContext, ensure_terminal_context


def _make_pool(jobs: int) -> Executor:
    """Pool för parallella dokument — processer, aldrig trådar.

    PDFium (pypdfium2, som ``ocr_pages.render_pages`` använder) är inte
    trådsäkert: samtidiga dokument i trådar gav ``Failed to load document
    (PDFium: Data format error)`` på nästan varje fil och kunde krascha hela
    processen. Rendering och bildanalys är dessutom CPU-bundna, så processer
    ger verklig parallellitet förbi GIL.
    """
    return ProcessPoolExecutor(max_workers=jobs)


@dataclass
class RedactionsOptions:
    root: Path = Path(__file__).resolve().parents[2]
    inp: Path | None = None
    txt: Path | None = None
    jobs: int = 4
    dpi: int = 72
    rebuild: bool = False
    rebuild_text: bool = False
    files_from: Path | None = None

    def __post_init__(self) -> None:
        if self.inp is None:
            self.inp = self.root / "downloaded" / "files"
        if self.txt is None:
            self.txt = self.root / "generated" / "text"


def run_detect_redactions(options: RedactionsOptions, context: OperationContext | None = None) -> int:
    """Kör redaktionsdetektering för väntande filer. Returnerar exitkod."""
    ctx = ensure_terminal_context(context)
    wpu_dir = options.root / "downloaded" / "wpu_files"
    # __post_init__ garanterar att dessa är ifyllda; lokala variabler gör det
    # synligt för typkontrollen.
    inp = options.inp
    txt = options.txt
    if inp is None or txt is None:  # pragma: no cover — ska inte hända
        raise ValueError("options.inp/txt saknas")

    if options.rebuild_text:
        ocr_dir = options.root / "generated" / "ocr"
        ctx.log(f"Regenererar .txt-filer från {ocr_dir}/*.pdf ...")
        for pdf in sorted(ocr_dir.glob("*.pdf")):
            stem = pdf.stem
            ctx.run_process(["pdftotext", "-layout", str(pdf), str(txt / f"{stem}.txt")], cwd=options.root)

    if options.rebuild:
        conn = state_db.connect()
        state_db.init_schema(conn)
        n = state_db.reset_redaction_state(conn)
        conn.close()
        ctx.log(f"Nollställde redaction-flaggor för {n} filer i state.db.")

    conn = state_db.connect()
    state_db.init_schema(conn)
    files_from: set[str] | None = None
    if options.files_from is not None:
        files_from = {
            line.strip().removesuffix(".txt")
            for line in options.files_from.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    pending = state_db.list_pending_redaction_stems(conn, files_from=files_from)
    conn.close()

    todo: list[tuple[str, Path]] = []
    for stem in pending:
        txt_file = txt / f"{stem}.txt"
        if not txt_file.is_file():
            continue
        pdf = inp / f"{stem}.pdf"
        if not pdf.is_file() and wpu_dir.is_dir():
            pdf = wpu_dir / f"{stem}.pdf"
        if pdf.is_file():
            todo.append((stem, pdf))

    total = len(todo)
    if total == 0:
        ctx.log("Redaktionsdetektering klar — inga nya filer.")
        return 0

    ctx.log(f"Kontrollerar maskeringar i {total} filer (DPI={options.dpi}, jobs={options.jobs})...")
    progress = {"done": 0, "failed": 0}
    ctx.log(f"maskeringar: {options.jobs} arbetsprocesser för {total} filer", level="debug")
    args = {stem: (pdf, txt, options.dpi, wpu_dir, options.root) for stem, pdf in todo}
    broken = _run_batch(ctx, options.jobs, list(args), args, progress, total)
    if broken:
        # En arbetsprocess dog hårt (t.ex. krasch i PDFium) och tog poolen med sig;
        # det går inte att se vilken fil som orsakade det. Kör de drabbade filerna
        # om en i taget i en pool med en arbetare: filerna körs i ordning, så när
        # poolen bryts igen är den första drabbade filen den skyldiga.
        ctx.log(
            f"en arbetsprocess dog — kör {len(broken)} filer om en i taget "
            f"(första: {', '.join(broken[:3])})",
            level="warning",
        )
        remaining = [stem for stem in args if stem in set(broken)]
        while remaining:
            hit = set(_run_batch(ctx, 1, remaining, args, progress, total))
            if not hit:
                break
            culprit_idx = next(i for i, stem in enumerate(remaining) if stem in hit)
            culprit = remaining[culprit_idx]
            progress["failed"] += 1
            progress["done"] += 1
            ctx.log(
                f"Maskeringsdetektering misslyckades för {culprit}: arbetsprocessen dog",
                level="error",
            )
            ctx.progress(progress["done"], total, culprit)
            remaining = [stem for stem in remaining[culprit_idx + 1:] if stem in hit]
    failed = progress["failed"]

    if failed:
        ctx.log(f"Klart med {failed} filfel — se loggen.", level="error")
    else:
        ctx.log("Klart.")
    return 0


def _run_batch(
    ctx: OperationContext,
    jobs: int,
    stems: list[str],
    args: dict[str, tuple],
    progress: dict[str, int],
    total: int,
) -> list[str]:
    """Kör ``stems`` i en ny pool; returnerar filerna som drabbades av en trasig pool.

    Vanliga undantag är filfel och loggas per fil. ``BrokenProcessPool`` betyder
    att en arbetsprocess dog; de filerna räknas inte än utan returneras, och
    anroparen avgör vilken som var skyldig.
    """
    broken: list[str] = []
    pool = _make_pool(jobs)
    try:
        futures = {pool.submit(_detect_one, stem, *args[stem]): stem for stem in stems}
        for fut in as_completed(futures):
            ctx.check_cancelled()
            stem = futures[fut]
            try:
                fut.result()
            except BrokenProcessPool:
                broken.append(stem)
                continue
            except Exception as exc:  # noqa: BLE001 - per-fil-fel får inte döda batchen
                progress["failed"] += 1
                ctx.log(
                    f"Maskeringsdetektering misslyckades för {stem}: "
                    f"{type(exc).__name__}: {exc}",
                    level="error",
                )
            progress["done"] += 1
            ctx.progress(progress["done"], total, stem)
    except BaseException:
        # Avbrott/fel: släpp köade filer i stället för att vänta in hela batchen.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return broken


def _detect_one(stem: str, pdf: Path, txt_dir: Path, dpi: int, wpu_dir: Path, root: Path) -> str:
    txt_file = txt_dir / f"{stem}.txt"
    marker = txt_dir / f"{stem}.redact"
    ocr_pdf = root / "generated" / "ocr" / f"{stem}.pdf"
    ocr_pages.detect_redactions_file(
        pdf, txt_file, marker, dpi, ocr_pdf=ocr_pdf if ocr_pdf.exists() else None
    )
    return stem
