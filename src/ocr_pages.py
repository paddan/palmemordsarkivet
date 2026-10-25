#!/usr/bin/env python3
"""Per-sida OCR-pipeline.

Renderar en PDF sida för sida och OCR:ar varje sida individuellt så att
enskilda dåliga sidor kan om-OCR:as med en annan motor utan att hela filen
måste köras om.

Output i ``<out_dir>/<stem>/``:
    page-NNN.png   — render för Tesseract/Vision

OCR-text och metadata lagras i ``state.db``-tabellen ``pdf_pages``.

Idempotens och metadata (engine, score) skrivs till ``state.db`` via
``db.record_page`` — ingen .json-markör skrivs längre.

Sammansatt ``<out_dir>/<stem>.txt`` skrivs på slutet, sidor separerade med ``\f``.

Engines:
    tesseract  — subprocess till `tesseract`
    vision     — subprocess till `ocrit` (macOS Vision Framework)
    surya      — Surya-modeller (radbaserad, 0.17) via helpers i den här modulen
    surya-layout — Surya 2 i en egen venv (``surya_client.SuryaWorker``): layoutblock
                 → ren text, Markdown och blocklayout (``surya_layout.parse_page``)

Kör:
    python ocr_pages.py --in path/to/foo.pdf --out-dir text_pages
    python ocr_pages.py --in foo.pdf --out-dir out --engine surya
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import db as state_db  # noqa: E402 — kräver ROOT på sys.path ovan

try:
    from quality import score_text
except Exception:  # pragma: no cover
    def score_text(text: str, use_hunspell: bool = False) -> dict:
        return {"chars": len(text), "score": 0.0}

from surya_client import (  # noqa: E402 — ren klient, ingen Surya-import
    EXIT_SURYA_UNAVAILABLE,
    UNAVAILABLE_CODES,
    SuryaPageError,
    SuryaUnavailable,
)

# Så många dödsfall (WORKER_DIED/PROTOCOL_MISMATCH) i rad tål en fil innan den
# avbryts med EXIT_SURYA_UNAVAILABLE; däremellan startas workern om per sida.
MAX_CONSECUTIVE_WORKER_DEATHS = 3

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass


def render_pages(pdf_path: Path, dpi: int):
    import pypdfium2 as pdfium  # local import — Surya/Vision-pad kanske saknar
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        for i in range(len(pdf)):
            try:
                page = pdf[i]
                yield i + 1, page.render(scale=dpi / 72).to_pil()
            except pdfium.PdfiumError as exc:
                print(f"  [render_pages] sida {i + 1} kunde inte laddas: {exc}", file=sys.stderr)
    finally:
        pdf.close()


def ocr_tesseract(png_path: Path, langs: str = "swe") -> str:
    if not shutil.which("tesseract"):
        raise RuntimeError("tesseract saknas i PATH")
    cmd = ["tesseract", str(png_path), "-", "-l", langs]
    out = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:400])
    return out.stdout.decode("utf-8", errors="replace")


def ocr_vision(png_path: Path) -> str:
    if not shutil.which("ocrit"):
        raise RuntimeError("ocrit saknas — installera: brew install insidegui/tap/ocrit")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        cmd = ["ocrit", str(png_path), "-o", str(tmpdir)]
        out = subprocess.run(cmd, capture_output=True, timeout=600, check=False)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", errors="replace")[:400])
        # ocrit skriver <stem>.txt till -o-katalogen
        candidate = tmpdir / (png_path.stem + ".txt")
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
        # fallback: första .txt:n
        for f in tmpdir.glob("*.txt"):
            return f.read_text(encoding="utf-8", errors="replace")
    return ""


@lru_cache(maxsize=1)
def _surya_load():
    """Laddar Surya-modellerna en gång per process (cachas via lru_cache)."""
    from surya.detection import DetectionPredictor
    from surya.foundation import FoundationPredictor
    from surya.recognition import RecognitionPredictor
    foundation = FoundationPredictor()
    rec = RecognitionPredictor(foundation)
    det = DetectionPredictor()
    return rec, det


def ocr_surya(image) -> str:
    rec, det = _surya_load()
    preds = rec([image], det_predictor=det)
    if not preds:
        return ""
    lines = [ln.text for ln in preds[0].text_lines]
    return "\n".join(lines)


def ocr_surya_lines(image) -> tuple[str, list[dict]]:
    """Som ocr_surya men returnerar även bboxes per rad i bildkoordinater."""
    rec, det = _surya_load()
    preds = rec([image], det_predictor=det)
    if not preds:
        return "", []
    lines: list[dict] = []
    for ln in preds[0].text_lines:
        if not ln.text.strip():
            continue
        x0, y0, x1, y1 = ln.bbox
        lines.append({"text": ln.text, "bbox": [x0, y0, x1, y1]})
    return "\n".join(line["text"] for line in lines), lines


def ocr_surya_layout(
    worker,
    image,
    work_dir: Path,
    pdf_stem: str,
    page: int,
    redactions: list[tuple[int, int]],
    trace: dict[str, Any] | None = None,
) -> tuple[str, str | None, str, list[dict]]:
    """Läs en sida med Surya 2-workern och tolka blocken.

    Sidbilden skrivs till en tillfällig PNG i ``work_dir``, skickas som absolut
    sökväg och tas bort direkt efter läsningen, även vid fel — en befintlig
    ``page-NNN.png`` (kanske i annan dpi) återanvänds aldrig. Returnerar ``(text, markdown, layout_json, pdf_lines)``:
    ``markdown`` är ``None`` när sidan saknar Markdown-innehåll (tom sida, bara
    figur eller bara sidfot), och ``pdf_lines`` har ett ``{"text", "bbox"}`` per
    block med en verklig ruta och text, för ``update_pdf_text_layer``.
    ``[MASKAD]``-block som pixeldetektorn lagt in saknar bredd och tas inte med i
    textlagret. ``trace`` fylls av ``parse_page`` med en textfri sammanfattning
    (bl.a. ``degraded``). Höjer ``SuryaUnavailable``/``SuryaPageError`` orörda.
    """
    import surya_layout  # lokalt: ren modul, men behövs bara för den här motorn

    work_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".layout-{page:03d}-", suffix=".png", dir=work_dir)
    os.close(fd)
    png_path = Path(name)
    try:
        image.save(str(png_path), format="PNG")
        raw = worker.read_page(pdf_stem, page, png_path.resolve())
    finally:
        png_path.unlink(missing_ok=True)
    blocks, _degraded = surya_layout.parse_page(
        raw.blocks, image=image, page_height=image.height,
        redactions=redactions, trace=trace,
    )
    text = surya_layout.page_text(blocks)
    markdown = surya_layout.page_markdown(blocks) if text else ""
    pdf_lines: list[dict] = []
    for block in blocks:
        block_text = surya_layout.page_text([block])
        if block.bbox is None or not block_text.strip():
            continue
        x0, y0, x1, y1 = block.bbox
        if x1 <= x0 or y1 <= y0:
            continue
        pdf_lines.append({"text": block_text, "bbox": [x0, y0, x1, y1]})
    return (
        text,
        markdown if markdown.strip() else None,
        surya_layout.blocks_to_json(blocks),
        pdf_lines,
    )


def update_pdf_text_layer(
    src_pdf: Path,
    dst_pdf: Path,
    page_lines: dict[int, list[dict]],
    dpi: int,
    on_failure: Callable[[int, int], None] | None = None,
) -> int:
    """Skriv ny PDF där angivna sidor får Surya-rader som osynligt textlager.

    src_pdf läses, befintligt textlager på de berörda sidorna redaktas bort,
    och Surya-textraderna skrivs in via insert_textbox med render_mode=3
    (osynlig). Övriga sidor lämnas orörda. Skrivs till dst_pdf (kan vara
    samma som src_pdf — vi använder en tempfil).

    En post kan vara flerradig (ett layoutblock): teckenstorleken räknas då per
    rad i rutan. Ryms en post inte ens efter fem halveringar avbryts hela
    patchen, utom när ``on_failure`` ges — då anropas den med ``(sida, index)``
    och resten av posterna skrivs ändå (layoutvägen).

    Returnerar antal sidor som faktiskt patchades.
    """
    import pymupdf  # local import — pymupdf är optional

    scale = 72.0 / dpi
    doc = pymupdf.open(str(src_pdf))
    patched = 0
    try:
        for page_num, lines in page_lines.items():
            if page_num < 1 or page_num > len(doc) or not lines:
                continue
            page = doc[page_num - 1]
            # Rensa befintligt textlager på sidan
            for block in page.get_text("dict")["blocks"]:
                if block.get("type") == 0:  # text-block
                    page.add_redact_annot(block["bbox"])
            page.apply_redactions(images=0, graphics=0)

            # Lägg in Surya-text osynligt
            for index, line in enumerate(lines):
                x0, y0, x1, y1 = line["bbox"]
                rect = pymupdf.Rect(
                    x0 * scale, y0 * scale, x1 * scale, y1 * scale
                )
                n_lines = line["text"].count("\n") + 1
                fontsize = max(1.0, (y1 - y0) * scale * 0.85 / n_lines)
                for _ in range(5):
                    rc = page.insert_textbox(
                        rect, line["text"],
                        fontsize=fontsize, fontname="helv",
                        render_mode=3, align=0,
                    )
                    if rc >= 0:
                        break
                    fontsize = max(1.0, fontsize * 0.5)
                if rc < 0 and on_failure is not None:
                    on_failure(page_num, index)
                elif rc < 0:
                    raise RuntimeError(
                        f"kunde inte infoga textrad på sida {page_num}: {line['text'][:80]!r}"
                    )
            patched += 1

        # Skriv via tempfil om src == dst
        if dst_pdf.resolve() == src_pdf.resolve():
            tmp = dst_pdf.with_suffix(dst_pdf.suffix + ".tmp")
            tmp.unlink(missing_ok=True)  # rensa ev. kvarleva från krasch
            doc.save(str(tmp), garbage=4, deflate=True)
            doc.close()
            tmp.replace(dst_pdf)
        else:
            doc.save(str(dst_pdf), garbage=4, deflate=True)
            doc.close()
    except Exception:
        doc.close()
        raise
    return patched


def detect_redactions_image(
    image,
    darkness: int = 40,
    min_width_frac: float = 0.10,
    min_height: int = 8,
) -> list[tuple[int, int]]:
    """Hitta svarta maskeringsblock i PIL-bild via pixelanalys.

    Söker rader med ett sammanhängande mörkt span >= min_width_frac × sidbredd.
    Returnerar lista av (y0, y1) i bildpixlar, en tuple per funnet block.
    """
    gray = image.convert("L")
    w, h = gray.size
    data = gray.tobytes()
    min_span = int(w * min_width_frac)

    dark_rows: list[int] = []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        max_span = span = 0
        for px in row:
            if px < darkness:
                span += 1
                if span > max_span:
                    max_span = span
            else:
                span = 0
        if max_span >= min_span:
            dark_rows.append(y)

    if not dark_rows:
        return []

    blocks: list[tuple[int, int]] = []
    y0 = dark_rows[0]
    yp = dark_rows[0]
    for y in dark_rows[1:]:
        if y > yp + 3:
            if yp - y0 + 1 >= min_height:
                blocks.append((y0, yp))
            y0 = y
        yp = y
    if yp - y0 + 1 >= min_height:
        blocks.append((y0, yp))
    return blocks


def _merge_redaction_markers(
    text: str,
    blocks: list[tuple[int, int]],
    image_height: int,
    line_bboxes: list[dict] | None = None,
) -> str:
    """Infoga [MASKAD]-markeringar i text vid detekterade maskeringsblock.

    Med line_bboxes (surya-format {text, bbox: [x0, y0, x1, y1]}) används
    exakta y-koordinater. Utan dem approximeras positionen via andel av sidans
    höjd multiplicerat med antal rader.
    """
    if not blocks:
        return text

    if line_bboxes is not None:
        items: list[tuple[float, str]] = [
            ((ln["bbox"][1] + ln["bbox"][3]) / 2, ln["text"])
            for ln in line_bboxes
        ]
        for b0, b1 in blocks:
            items.append(((b0 + b1) / 2, "[MASKAD]"))
        items.sort(key=lambda x: x[0])
        return "\n".join(t for _, t in items)

    lines = text.split("\n")
    n = len(lines)
    insertions: list[tuple[int, str]] = []
    for b0, b1 in blocks:
        mid_y = (b0 + b1) / 2
        idx = max(0, min(n, round(mid_y / image_height * n)))
        insertions.append((idx, "[MASKAD]"))
    for idx, marker in sorted(insertions, key=lambda x: x[0], reverse=True):
        lines.insert(idx, marker)
    return "\n".join(lines)


def detect_redactions_file(
    pdf: Path,
    txt_file: Path,
    marker: Path,
    dpi: int = 72,
    ocr_pdf: Path | None = None,
) -> int:
    """Infoga [MASKAD] i befintlig txt-fil baserat på bildanalys av pdf.

    Renderar sidor vid låg DPI (behövs bara för att hitta stora svarta block).
    Om ocr_pdf anges används dess textlager (via PyMuPDF) för att positionera
    [MASKAD] exakt efter hur textrader faktiskt fördelar sig på sidan, istället
    för att approximera med jämn fördelning.
    Idempotent via marker-fil. Returnerar antal infogade block.
    """
    if marker.exists():
        # Markörfilen finns — synka DB om redaction_checked_at saknas (t.ex. efter
        # migrering eller om DB-uppdateringen missades vid föregående körning).
        try:
            conn = state_db.connect()
            state_db.init_schema(conn)
            row = state_db.get_pdf_file(conn, pdf.stem)
            if row is None or row["redaction_checked_at"] is None:
                source = state_db.source_for_path(pdf)
                state_db.upsert_pdf_file(
                    conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
                )
                has_red = (
                    txt_file.exists()
                    and "[MASKAD]" in txt_file.read_text(encoding="utf-8", errors="replace")
                )
                state_db.mark_redaction_checked(conn, pdf.stem, has_redactions=has_red)
        except Exception as exc:
            # Markören tas bort ändå (kontrollflödet oförändrat), men felet loggas
            # så att det går att se varför filen annars kontrolleras om varje körning.
            log_error(
                "ocr_pages.detect_redactions",
                pdf.name,
                f"kunde inte synka redaction_checked_at trots markörfil: {type(exc).__name__}: {exc}",
            )
        marker.unlink(missing_ok=True)
        return 0
    if not txt_file.exists():
        return 0

    full_text = txt_file.read_text(encoding="utf-8", errors="replace")
    pages_text = full_text.split("\f")

    # Öppna OCR-PDF en gång för alla sidor (om tillgänglig).
    ocr_doc = None
    if ocr_pdf is not None and ocr_pdf.exists():
        try:
            import pymupdf
            ocr_doc = pymupdf.open(str(ocr_pdf))
        except Exception as exc:
            # Fortsätter utan textlager: [MASKAD] placeras då med jämn fördelning.
            log_error(
                "ocr_pages.detect_redactions",
                ocr_pdf.name,
                f"kunde inte öppna OCR-PDF för textlagret, fortsätter utan: {type(exc).__name__}: {exc}",
            )

    pt_per_px = 72.0 / dpi  # bildpixlar → PDF-punkter (vid dpi=72 är faktorn 1.0)

    try:
        n_blocks = 0
        updated = False
        for page_num, image in render_pages(pdf, dpi):
            page_idx = page_num - 1
            if page_idx >= len(pages_text):
                continue
            page_text = pages_text[page_idx]
            if "[MASKAD]" in page_text:
                continue
            blocks = detect_redactions_image(image)
            if not blocks:
                continue

            # Hämta y-mittpunkter för textrader från OCR-PDF:ens textlager.
            y_centers: list[float] = []
            if ocr_doc is not None and page_idx < len(ocr_doc):
                try:
                    for blk in ocr_doc[page_idx].get_text("dict")["blocks"]:
                        if blk.get("type") != 0:
                            continue
                        for ln in blk.get("lines", []):
                            y0, y1 = ln["bbox"][1], ln["bbox"][3]
                            y_centers.append((y0 + y1) / 2)
                    y_centers.sort()
                except Exception:
                    y_centers = []

            lines = page_text.split("\n")
            n = len(lines)
            insertions: list[tuple[int, str]] = []
            for b0, b1 in blocks:
                if y_centers:
                    # Exakt: räkna PDF-rader ovanför blockets mittpunkt.
                    mid_pt = (b0 + b1) / 2 * pt_per_px
                    pdf_idx = sum(1 for y in y_centers if y < mid_pt)
                    idx = max(0, min(n, round(pdf_idx / len(y_centers) * n)))
                else:
                    # Fallback: proportionell mot bildhöjd (jämn fördelning antas).
                    idx = max(0, min(n, round((b0 + b1) / 2 / image.height * n)))
                insertions.append((idx, "[MASKAD]"))
            for idx, m in sorted(insertions, key=lambda x: x[0], reverse=True):
                lines.insert(idx, m)
            pages_text[page_idx] = "\n".join(lines)
            n_blocks += len(blocks)
            updated = True
    finally:
        if ocr_doc is not None:
            ocr_doc.close()

    if updated:
        txt_file.write_text("\f".join(pages_text), encoding="utf-8")

    # Skriv till state.db — autoritativ källa (inga .redact-filer skapas längre).
    try:
        conn = state_db.connect()
        state_db.init_schema(conn)
        source = state_db.source_for_path(pdf)
        state_db.upsert_pdf_file(
            conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
        )
        state_db.mark_redaction_checked(
            conn, pdf.stem, has_redactions=(n_blocks > 0),
        )
    except Exception as e:  # pragma: no cover
        log_error("ocr_pages.detect_redactions", pdf.name, f"db: {e}")

    return n_blocks


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_ocr_pages(
    *,
    inp: Path,
    out_dir: str | None,
    engine: str,
    langs: str,
    dpi: int,
    pages: str | None,
    ocr_dir: str | None,
    no_update_pdf: bool,
    no_detect_redactions: bool,
    txt_dir: str | None,
    context=None,
    surya_worker=None,
) -> int:
    """OCR:a en PDF per sida. Returnerar exitkod.

    ``surya_worker`` ersätter ``SuryaWorker`` för motorn ``surya-layout`` (tester);
    ``None`` skapar en riktig worker, en gång per anrop och bara om någon sida
    faktiskt ska OCR:as. Saknas Surya-runtimen (``UNAVAILABLE_CODES``), eller dör
    workern ``MAX_CONSECUTIVE_WORKER_DEATHS`` gånger i rad, avbryts resten av filen
    och exitkoden blir ``EXIT_SURYA_UNAVAILABLE`` (3) — ett miljöfel som anroparen
    inte ska bokföra på filen. Ett enstaka dödsfall hoppar bara sidan; klienten
    startar workern igen vid nästa sida.
    """
    ctx = _ctx(context)
    pdf = Path(inp)
    if not pdf.exists():
        ctx.log(f"Saknar {pdf}", level="error")
        return 1

    if engine == "detect-only":
        tdir = Path(txt_dir) if txt_dir else ROOT / "generated" / "text"
        txt_file = tdir / f"{pdf.stem}.txt"
        marker = tdir / f"{pdf.stem}.redact"
        ocr_pdf = Path(ocr_dir) / f"{pdf.stem}.pdf" if ocr_dir else None
        n = detect_redactions_file(pdf, txt_file, marker, dpi, ocr_pdf=ocr_pdf)
        ctx.log("inga" if n == 0 else f"{n} block")
        return 0

    if not out_dir:
        ctx.log("--out-dir krävs för alla motorer utom detect-only", level="error")
        return 1

    out_path = Path(out_dir)
    stem_dir = out_path / pdf.stem
    stem_dir.mkdir(parents=True, exist_ok=True)

    only_pages: set[int] | None = None
    if pages:
        try:
            only_pages = {int(x) for x in pages.split(",") if x.strip()}
        except ValueError:
            ctx.log("--pages ska vara kommaseparerade heltal", level="error")
            return 1

    page_lines: dict[int, list[dict]] = {}
    want_pdf_patch = engine in ("surya", "surya-layout") and bool(ocr_dir) and not no_update_pdf
    n_total = 0
    n_skipped = 0
    n_done = 0
    skipped_pages: list[int] = []
    aborted = False
    worker_deaths = 0
    conn = state_db.connect()
    state_db.init_schema(conn)
    source = state_db.source_for_path(pdf)
    state_db.upsert_pdf_file(
        conn, pdf_stem=pdf.stem, source=source, pdf_path=str(pdf),
    )
    # surya-layout: workern (modell + llama-server) startas först när en sida
    # verkligen ska OCR:as, och stängs (hela processgruppen) när loopen lämnas.
    layout_worker = None
    identity: str | None = None
    with contextlib.ExitStack() as stack:
        for page_num, image in render_pages(pdf, dpi):
            ctx.check_cancelled()
            n_total += 1
            if only_pages is not None and page_num not in only_pages:
                continue

            png_path = stem_dir / f"page-{page_num:03d}.png"

            if state_db.page_exists(conn, pdf.stem, page_num):
                n_skipped += 1
                skipped_pages.append(page_num)
                continue

            redaction_blocks: list[tuple[int, int]] = []
            if not no_detect_redactions:
                redaction_blocks = detect_redactions_image(image)

            markdown: str | None = None
            layout_json: str | None = None
            try:
                if engine == "surya-layout":
                    if layout_worker is None:
                        layout_worker = _open_layout_worker(stack, surya_worker)
                        identity = layout_worker.identity()
                        ctx.log(
                            f"  [{pdf.stem}] surya-layout: motoridentitet "
                            f"{identity or 'okänd (--identity svarade inte)'}",
                            level="debug",
                        )
                    trace: dict[str, Any] = {}
                    text, markdown, layout_json, lines = ocr_surya_layout(
                        layout_worker, image, stem_dir, pdf.stem, page_num,
                        redaction_blocks, trace=trace,
                    )
                    worker_deaths = 0
                    _log_layout_trace(ctx, pdf.stem, page_num, trace, markdown)
                    if want_pdf_patch:
                        page_lines[page_num] = lines
                elif engine == "surya":
                    if want_pdf_patch:
                        text, lines = ocr_surya_lines(image)
                        page_lines[page_num] = lines
                        if redaction_blocks:
                            text = _merge_redaction_markers(
                                text, redaction_blocks, image.height, line_bboxes=lines
                            )
                    else:
                        text = ocr_surya(image)
                        if redaction_blocks:
                            text = _merge_redaction_markers(
                                text, redaction_blocks, image.height
                            )
                else:
                    if not png_path.exists():
                        image.save(str(png_path))
                    if engine == "vision":
                        text = ocr_vision(png_path)
                    else:
                        text = ocr_tesseract(png_path, langs)
                    if redaction_blocks:
                        text = _merge_redaction_markers(
                            text, redaction_blocks, image.height
                        )
            except SuryaUnavailable as e:
                log_error("ocr_pages", f"{pdf.name}#p{page_num}", e.code)
                if e.code not in UNAVAILABLE_CODES:
                    worker_deaths += 1
                    if worker_deaths < MAX_CONSECUTIVE_WORKER_DEATHS:
                        ctx.log(
                            f"  [{pdf.stem} p{page_num}] Surya-workern dog ({e.code}, "
                            f"{worker_deaths} i rad) — sidan hoppas, workern startas om "
                            f"vid nästa sida: {e}",
                            level="error",
                        )
                        continue
                ctx.log(
                    f"  [{pdf.stem} p{page_num}] Surya-workern går inte att använda "
                    f"({e.code}"
                    + (f", {worker_deaths} dödsfall i rad" if worker_deaths else "")
                    + f") — resten av filen hoppas (exitkod {EXIT_SURYA_UNAVAILABLE}): {e}",
                    level="error",
                )
                aborted = True
                break
            except SuryaPageError as e:
                worker_deaths = 0
                ctx.log(f"  [{pdf.stem} p{page_num}] Surya kunde inte läsa sidan ({e.code}): {e}",
                        level="error")
                log_error("ocr_pages", f"{pdf.name}#p{page_num}", e.code)
                continue
            except Exception as e:  # noqa: BLE001
                ctx.log(f"  [{pdf.stem} p{page_num}] FEL: {e}", level="error")
                log_error("ocr_pages", f"{pdf.name}#p{page_num}", str(e))
                continue

            try:
                scored = score_text(text, use_hunspell=False)
            except Exception:
                scored = {"chars": len(text), "score": 0.0}

            redact_suffix = f" [{len(redaction_blocks)} mask]" if redaction_blocks else ""
            state_db.record_page(
                conn, pdf_stem=pdf.stem, page_num=page_num,
                engine=engine, text=text, score=scored.get("score"),
                markdown=markdown, layout_json=layout_json,
                engine_identity=identity if engine == "surya-layout" else None,
            )
            n_done += 1
            ctx.log(f"  [{pdf.stem} p{page_num:03d}] {len(text):5d} tecken "
                    f"score={scored.get('score', 0)}{redact_suffix}")

    if skipped_pages:
        ctx.log(
            f"  [{pdf.stem}] {len(skipped_pages)} sidor hoppade — redan i pdf_pages "
            f"(t.ex. {skipped_pages[:10]})",
            level="debug",
        )
    ctx.log(f"Klart {pdf.stem}: {n_done} OCR:ade, {n_skipped} hoppade, "
            f"{n_total} sidor totalt.")

    # Vid avbrott är n_total ofullständigt — markera då inga sidor som "saknade".
    if only_pages and n_done == 0 and n_skipped == 0 and not aborted:
        missing = only_pages - set(range(1, n_total + 1))
        for p in missing:
            if not state_db.page_exists(conn, pdf.stem, p):
                state_db.record_page(
                    conn, pdf_stem=pdf.stem, page_num=p,
                    engine=engine, text="", score=0.0,
                )
        if missing:
            ctx.log(f"  [varning] {len(missing)} begärda sidor saknas i PDF:en "
                    f"({min(missing)}–{max(missing)}), markerade som försökta.")

    if want_pdf_patch and page_lines:
        # want_pdf_patch kräver en icke-tom OCR-katalog enligt definitionen ovan.
        assert ocr_dir is not None
        ocr_pdf = Path(ocr_dir) / f"{pdf.stem}.pdf"
        if not ocr_pdf.exists():
            ocr_pdf.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pdf, ocr_pdf)
            ctx.log(f"  [pdf-patch] kopierade {pdf.name} → {ocr_pdf.parent.name}/")
        if ocr_pdf.exists():
            on_failure = None
            if engine == "surya-layout":
                def on_failure(page: int, index: int) -> None:
                    # Ett block som inte ryms får inte stoppa resten av patchen.
                    ctx.log(f"  [pdf-patch] {pdf.stem} p{page}: block {index} rymdes inte "
                            "i sin ruta och saknas i textlagret", level="warning")
                    log_error("ocr_pages.pdf_patch", f"{pdf.name}#p{page}",
                              f"block {index} rymdes inte i textlagret")
            try:
                n = update_pdf_text_layer(ocr_pdf, ocr_pdf, page_lines, dpi,
                                          on_failure=on_failure)
                ctx.log(f"  [pdf-patch] {ocr_pdf.name}: patchade {n} sidor")
            except Exception as e:  # noqa: BLE001
                ctx.log(f"  [pdf-patch] FEL: {e}", level="error")
                log_error("ocr_pages.pdf_patch", pdf.name, str(e))
    return EXIT_SURYA_UNAVAILABLE if aborted else 0


def _open_layout_worker(stack: contextlib.ExitStack, injected):
    """Den injicerade workern (tester) eller en ny ``SuryaWorker`` som stängs av ``stack``."""
    if injected is not None:
        return injected
    from surya_client import SuryaWorker

    return stack.enter_context(SuryaWorker())


def _log_layout_trace(ctx, stem: str, page_num: int, trace: dict[str, Any],
                      markdown: str | None) -> None:
    """Logga layouttolkningens textfria spår (debug) och degraderad struktur (varning)."""
    summary = json.dumps(trace, ensure_ascii=False, sort_keys=True, default=str)
    ctx.log(f"  [{stem} p{page_num}] layout-spår: {summary}", level="debug")
    if markdown is None:
        ctx.log(f"  [{stem} p{page_num}] ingen Markdown: sidan saknar innehåll "
                "utöver sidfot/figur", level="debug")
    if trace.get("degraded"):
        ctx.log(f"  [{stem} p{page_num}] degraderad struktur: minst ett block "
                "kunde inte tolkas enligt sin etikett och sparades som ren text",
                level="warning")


def _raise_system_exit(signum: int, frame: object) -> None:
    """SIGTERM → ``SystemExit`` så att ``ExitStack`` stänger Surya-workerns grupp."""
    raise SystemExit(128 + signum)


def _install_sigterm_handler() -> None:
    """Ett avbrutet jobb skickar SIGTERM; utan hanterare dör processen utan att
    köra sina kontexthanterare och workern (egen session) och llama-server blir
    kvar. Återställs inte — det här är en CLI-process."""
    signal.signal(signal.SIGTERM, _raise_system_exit)


def main() -> int:
    _install_sigterm_handler()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp",
                    default=os.environ.get("IN"),
                    required=not os.environ.get("IN"),
                    help="PDF-fil (env: IN)")
    ap.add_argument("--out-dir",
                    default=os.environ.get("OUT_DIR"),
                    help="output-katalog (env: OUT_DIR); krävs ej för --engine detect-only")
    ap.add_argument("--engine",
                    default=os.environ.get("ENGINE", "tesseract"),
                    choices=["tesseract", "vision", "surya", "surya-layout", "detect-only"],
                    help="OCR-motor (default: tesseract). surya-layout: Surya 2-layout med Markdown "
                         "(kräver separat venv, PALME_SURYA_PYTHON). detect-only: bara "
                         "redaktionsdetektering, ingen om-OCR")
    ap.add_argument("--langs",
                    default=os.environ.get("LANGS", "swe"),
                    help="tesseract-språk (default: swe)")
    ap.add_argument("--dpi", type=int,
                    default=int(os.environ.get("DPI", "300")),
                    help="render-DPI (default: 300)")
    ap.add_argument("--pages",
                    default=os.environ.get("PAGES"),
                    help="kommaseparerade sidnummer (1-baserade); default alla")
    ap.add_argument("--ocr-dir",
                    default=os.environ.get("OCR_DIR"),
                    help="katalog med OCR-ade PDF:er (generated/ocr)")
    ap.add_argument("--no-update-pdf", action="store_true",
                    help="stäng av PDF-textlager-patchen även om --ocr-dir är satt")
    ap.add_argument("--no-detect-redactions", action="store_true",
                    help="stäng av automatisk detektering av maskeringsblock")
    ap.add_argument("--txt-dir",
                    default=os.environ.get("TXT_DIR"),
                    help="text-katalog för detect-only (default: <root>/generated/text)")
    args = ap.parse_args()

    return run_ocr_pages(
        inp=Path(args.inp),
        out_dir=args.out_dir,
        engine=args.engine,
        langs=args.langs,
        dpi=args.dpi,
        pages=args.pages,
        ocr_dir=args.ocr_dir,
        no_update_pdf=args.no_update_pdf,
        no_detect_redactions=args.no_detect_redactions,
        txt_dir=args.txt_dir,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
