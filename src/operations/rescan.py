"""Operationen ``rescan-page``: skanna om en enda sida, eller återställ en äldre läsning.

Flöde vid omskanning: hitta PDF:en → kontrollera sidnumret → spara den gällande
läsningen i historiken (``pdf_page_readings``) → OCR:a sidan med ``ocr_pages.py
--force`` i en underprocess → ``merge_one`` → ``normalize_text.process_file`` →
``mark_normalized`` → (valbart) inkrementell ingest av just det dokumentet.

Vid återställning (``restore_reading``) körs ingen OCR: den historiska läsningen
skrivs tillbaka som gällande rad i ``pdf_pages`` efter att den läsning den
ersätter sparats i historiken, och sedan följer samma väg (merge → normalize →
ingest). Därmed hamnar också återställningen i historiken och kan ångras.
Återställningen kräver inte PDF:en — sidan kontrolleras mot textfilen.

Den gällande läsningen är sidan i ``generated/text/<stem>.txt`` (den som
indexeras och citeras). ``pdf_pages``-raden arkiveras bara när den stämmer med
den sidan; har raden text ``NULL`` (äldre markörer) eller har texten ändrats efter
OCR:en arkiveras textfilens sida, annars hade den gått förlorad vid omskanningen.

OCR:en körs som underprocess (inte i processen) eftersom ``ocr_pages.py`` har
SIGTERM-hanteraren som stänger Surya-workerns processgrupp vid avbrott; workern
sätter bara en avbrottsflagga i sin egen process.
"""

from __future__ import annotations

from pathlib import Path

import db as state_db
from llm_vision import EXIT_LLM_FATAL
from page_review import (
    WHOLE_FILE_ENGINE,
    find_pdf,
    is_removed_duplicate,
    page_count,
    text_page_count,
)
from surya_client import EXIT_SURYA_UNAVAILABLE

from .context import OperationContext, ensure_terminal_context
from .exceptions import OperationFailed
from .page_readings import (
    ENGINES,
    VISION_ENGINE,
    archive_current,
    check_vision_profile,
    forget_failed_attempt,
    load_vision_profile,
    ocr_argv,
    reindex_stems,
    rescore_documents,
    row_snapshot,
)
from .page_readings import (
    connect as _connect,
)
from .page_readings import (
    restore_reading as restore_reading_row,
)


def _validate_page(root: Path, pdf_stem: str, page: int, ctx: OperationContext) -> Path:
    """PDF:en för stammen; ``OperationFailed`` om den saknas eller sidan inte finns."""
    if not pdf_stem:
        raise OperationFailed("Ange ett dokument (--pdf-stem).")
    with _connect() as conn:
        row = state_db.get_pdf_file(conn, pdf_stem)
    if row is not None and is_removed_duplicate(root, pdf_stem, row["tesseract_done_at"]):
        # En omskanning hade byggt en ny textfil av bara den här sidan — en
        # ofullständig dubblett i indexet bredvid den fullständiga kopian.
        raise OperationFailed(
            f"{pdf_stem} är en dubblett vars text merge_wpu ersatt med den andra "
            "kopian (palmemordsarkivet/wpu.nu) — skanna om den kopian i stället."
        )
    pdf: Path | None = find_pdf(root, pdf_stem)
    if pdf is None:
        raise OperationFailed(
            f"PDF:en för {pdf_stem} saknas i downloaded/files och downloaded/wpu_files."
        )
    try:
        count = page_count(pdf)
    except Exception as exc:  # noqa: BLE001 - oläsbar PDF ska ge ett begripligt fel
        raise OperationFailed(f"{pdf.name} gick inte att läsa: {exc}") from exc
    if not 1 <= page <= count:
        raise OperationFailed(
            f"Sidan {page} finns inte i {pdf.name} (giltigt: 1–{count})."
        )
    ctx.log(f"rescan {pdf_stem} s{page}: {pdf} har {count} sidor", level="debug")
    return pdf


def _validate_restore_page(root: Path, pdf_stem: str, page: int, ctx: OperationContext) -> None:
    """Kontrollera sidan mot textfilen (PDF:en behövs inte för att återställa).

    Saknas textfilen används PDF:ens sidantal; saknas båda avbryts återställningen.
    """
    if not pdf_stem:
        raise OperationFailed("Ange ett dokument (--pdf-stem).")
    count = text_page_count(root, pdf_stem)
    source = f"{pdf_stem}.txt"
    if count is None:
        pdf = find_pdf(root, pdf_stem)
        if pdf is None:
            raise OperationFailed(
                f"Varken textfil eller PDF finns för {pdf_stem} — inget att återställa till."
            )
        try:
            count = page_count(pdf)
        except Exception as exc:  # noqa: BLE001 - oläsbar PDF ska ge ett begripligt fel
            raise OperationFailed(f"{pdf.name} gick inte att läsa: {exc}") from exc
        source = pdf.name
    if not 1 <= page <= count:
        raise OperationFailed(f"Sidan {page} finns inte i {source} (giltigt: 1–{count}).")
    ctx.log(f"rescan {pdf_stem} s{page}: återställning kontrollerad mot {source} "
            f"({count} sidor)", level="debug")


def _load_reading(pdf_stem: str, page: int, reading_id: int):
    """Den historiska läsningen; ``OperationFailed`` om den inte hör till sidan."""
    with _connect() as conn:
        reading = state_db.get_page_reading(conn, reading_id)
    if reading is None:
        raise OperationFailed(f"Läsning {reading_id} finns inte i historiken.")
    if reading["pdf_stem"] != pdf_stem or reading["page_num"] != page:
        raise OperationFailed(
            f"Läsning {reading_id} hör till {reading['pdf_stem']} sida "
            f"{reading['page_num']}, inte {pdf_stem} sida {page}."
        )
    if reading["text"] is None:
        raise OperationFailed(
            f"Läsning {reading_id} saknar text och kan inte återställas "
            "(äldre markörrad utan sidtext)."
        )
    return reading


def _ocr_page(
    root: Path, pdf: Path, page: int, engine: str, dpi: int, ctx: OperationContext,
    profile: str = "",
) -> None:
    """OCR:a sidan med ``ocr_pages.py --force``; ``OperationFailed`` vid fel.

    Den gällande raden i ``pdf_pages`` ersätts bara när sidan faktiskt lästs —
    ett misslyckat försök lämnar den orörd.
    """
    rc = ctx.run_process(ocr_argv(root, pdf, [page], engine, dpi, profile), cwd=root)
    ctx.log(f"rescan {pdf.stem} s{page}: ocr_pages ({engine}) rc={rc}", level="debug")
    if rc == EXIT_SURYA_UNAVAILABLE:
        raise OperationFailed(
            f"Surya-runtimen gick inte att använda för {pdf.stem} sida {page} "
            f"(exitkod {rc}) — orsaken står i jobbloggen (Admin → Logg). "
            "Den gällande läsningen är orörd."
        )
    if rc == EXIT_LLM_FATAL:
        raise OperationFailed(
            f"LLM-tjänsten gick inte att använda för {pdf.stem} sida {page} "
            f"(exitkod {rc}: profil, nyckel, kredit eller kvot) — orsaken står i "
            "jobbloggen (Admin → Logg). Den gällande läsningen är orörd."
        )
    if rc != 0:
        raise OperationFailed(
            f"Omskanningen av {pdf.stem} sida {page} med {engine} misslyckades "
            f"(exitkod {rc}). Den gällande läsningen är orörd."
        )


def run_rescan_page(
    *,
    root: Path,
    pdf_stem: str,
    page: int,
    engine: str,
    dpi: int,
    reindex: bool,
    restore_reading: int | None,
    context: OperationContext | None = None,
    profile: str = "",
) -> int:
    """Skanna om en sida (eller återställ läsningen ``restore_reading``). Returnerar 0.

    All validering sker före första skrivningen; fel ger ``OperationFailed``.
    ``profile`` krävs för motorn ``llm-vision`` (löses och bildkontrolleras före
    arkiveringen) och ignoreras för övriga motorer och vid återställning.
    """
    ctx = ensure_terminal_context(context)
    root = Path(root)
    if engine not in ENGINES:
        raise OperationFailed(f"Okänd motor {engine!r} (giltiga: {', '.join(ENGINES)}).")
    ctx.step("Skanna om sida")
    if restore_reading is not None:
        _validate_restore_page(root, pdf_stem, page, ctx)
        reading = _load_reading(pdf_stem, page, restore_reading)
        ctx.check_cancelled()
        archive_current(root, pdf_stem, page, "restore", ctx)
        restore_reading_row(pdf_stem, page, reading, ctx)
    else:
        reading = None
        pdf = _validate_page(root, pdf_stem, page, ctx)
        if engine == VISION_ENGINE:
            check_vision_profile(load_vision_profile(profile), ctx)
        elif profile:
            ctx.log(f"rescan {pdf_stem} s{page}: profilen {profile!r} ignoreras för "
                    f"motorn {engine}", level="debug")
        ctx.check_cancelled()
        ctx.log(f"rescan {pdf_stem} s{page}: motor {engine}, dpi {dpi}", level="debug")
        before = row_snapshot(pdf_stem, page)
        reading_id = archive_current(root, pdf_stem, page, "rescan", ctx)
        try:
            _ocr_page(root, pdf, page, engine, dpi, ctx, profile)
        except BaseException:
            forget_failed_attempt(pdf_stem, page, reading_id, before, ctx)
            raise
        with _connect() as conn:
            row = state_db.get_page_row(conn, pdf_stem, page)
        if row is not None:
            ctx.log(f"rescan {pdf_stem} s{page}: ny läsning ({row['engine']}, "
                    f"poäng {row['score']})", level="debug")
        ctx.check_cancelled()

    # Återställningen avbryts inte mellan skrivningen och sammanslagningen: en
    # återställd hel-fils-läsning får inte bli kvar som pdf_pages-rad.
    txt_dir = root / "generated" / "text"
    from .ocr import merge_and_normalize

    # Bara den här sidan: en inaktuell grannrad får inte skriva över filens
    # (kanske rättade) grannsida, som aldrig arkiverades.
    has_text = merge_and_normalize(pdf_stem, txt_dir, only_pages={page})
    ctx.log(f"rescan {pdf_stem} s{page}: merge + normalize klar "
            f"(textfil har innehåll: {has_text})", level="debug")
    if reading is not None and reading["engine"] == WHOLE_FILE_ENGINE:
        # Sidan saknade OCR-rad när läsningen sparades. En kvarlämnad rad hade
        # stängt sidan för Surya-redo (page_exists) och fått fallbacken att räkna
        # filen som ofullständig; texten finns nu i textfilen.
        with _connect() as conn:
            state_db.delete_page_row(conn, pdf_stem, page)
        ctx.log(f"rescan {pdf_stem} s{page}: återställd hel-fils-OCR-läsning, "
                "pdf_pages-raden borttagen — textfilen är den gällande läsningen",
                level="debug")

    rescore_documents(root, [pdf_stem], ctx, label="rescan-page")
    if reindex:
        ctx.check_cancelled()
        reindex_stems(root, {pdf_stem}, ctx)
    else:
        ctx.log(f"rescan {pdf_stem}: ingen omindexering (--no-reindex)", level="debug")

    action = "Återställde" if reading is not None else "Skannade om"
    ctx.log(f"{action} {pdf_stem} sida {page}.")
    return 0
