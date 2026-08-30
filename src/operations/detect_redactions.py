"""Migrerad logik från ``detect_redactions.sh``.

Detekterar maskeringsblock i PDF:er och infogar ``[MASKAD]`` i OCR-text.
Idempotens spåras i ``pdf_files.redaction_checked_at``; kandidater förfiltreras
från state.db så redan kontrollerade filer inte spawnar processer.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import db as state_db
import ocr_pages

from .context import OperationContext, ensure_terminal_context


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
    done = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        futures = {
            pool.submit(
                _detect_one, stem, pdf, txt, options.dpi, wpu_dir, options.root
            ): stem
            for stem, pdf in todo
        }
        for fut in as_completed(futures):
            ctx.check_cancelled()
            done += 1
            try:
                stem = fut.result()
            except Exception as exc:  # noqa: BLE001 - per-fil-fel får inte döda batchen
                failed += 1
                stem = futures[fut]
                ctx.log(
                    f"Maskeringsdetektering misslyckades för {stem}: {exc}",
                    level="error",
                )
            ctx.progress(done, total, stem)

    if failed:
        ctx.log(f"Klart med {failed} filfel — se loggen.", level="error")
    else:
        ctx.log("Klart.")
    return 0


def _detect_one(stem: str, pdf: Path, txt_dir: Path, dpi: int, wpu_dir: Path, root: Path) -> str:
    txt_file = txt_dir / f"{stem}.txt"
    marker = txt_dir / f"{stem}.redact"
    ocr_pdf = root / "generated" / "ocr" / f"{stem}.pdf"
    ocr_pages.detect_redactions_file(
        pdf, txt_file, marker, dpi, ocr_pdf=ocr_pdf if ocr_pdf.exists() else None
    )
    return stem
