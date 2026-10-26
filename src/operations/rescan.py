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

import sys
from contextlib import closing
from pathlib import Path

import db as state_db
from normalize_text import markdown_is_fresh
from page_review import WHOLE_FILE_ENGINE, file_page, find_pdf, page_count, text_page_count
from surya_client import EXIT_SURYA_UNAVAILABLE

from .context import OperationContext, ensure_terminal_context
from .exceptions import OperationFailed

ROOT = Path(__file__).resolve().parents[2]

ENGINES: tuple[str, ...] = ("surya-layout", "surya", "tesseract", "vision")


def _connect():
    conn = state_db.connect()
    state_db.init_schema(conn)
    return closing(conn)


def _validate_page(root: Path, pdf_stem: str, page: int, ctx: OperationContext) -> Path:
    """PDF:en för stammen; ``OperationFailed`` om den saknas eller sidan inte finns."""
    if not pdf_stem:
        raise OperationFailed("Ange ett dokument (--pdf-stem).")
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


def _archive_current(
    root: Path, pdf_stem: str, page: int, reason: str, ctx: OperationContext,
) -> int | None:
    """Spara den gällande läsningen i historiken; id:t eller ``None`` om text saknas.

    Gällande är sidan i textfilen. Stämmer ``pdf_pages``-raden med den
    (``markdown_is_fresh``) kopieras raden med Markdown och layout. Har raden
    text ``NULL`` eller avviker den sparas textfilens sida — med radens motor när
    raden har (inaktuell) text, annars ``hel-fils-OCR`` — utan Markdown, layout,
    motoridentitet och poäng, som inte hör till den texten. Finns ingen sida i
    textfilen kopieras raden om den har text, annars arkiveras ingenting.
    """
    with _connect() as conn:
        row = state_db.get_page_row(conn, pdf_stem, page)
        file_text = file_page(root, pdf_stem, page)
        row_text = row["text"] if row is not None else None
        if row is not None and row_text is not None and (
            file_text is None or markdown_is_fresh(row_text, file_text)
        ):
            why = "raden stämmer med textfilen" if file_text is not None else (
                "ingen sida i textfilen att jämföra med")
            fields = {
                "engine": row["engine"], "text": row_text, "markdown": row["markdown"],
                "layout_json": row["layout_json"], "engine_identity": row["engine_identity"],
                "score": row["score"],
            }
        elif file_text is not None:
            if row is None:
                why, engine = "ingen pdf_pages-rad, textfilens sida", WHOLE_FILE_ENGINE
            elif row_text is None:
                why, engine = (f"pdf_pages-raden ({row['engine']}) har text NULL, "
                               "textfilens sida"), WHOLE_FILE_ENGINE
            else:
                why, engine = (f"pdf_pages-raden ({row['engine']}) avviker från "
                               "textfilen, textfilens sida"), str(row["engine"])
            fields = {
                "engine": engine, "text": file_text, "markdown": None,
                "layout_json": None, "engine_identity": None, "score": None,
            }
        else:
            ctx.log(f"rescan {pdf_stem} s{page}: ingen gällande läsning att arkivera "
                    "(ingen sida i textfilen och "
                    + ("ingen pdf_pages-rad)" if row is None else "pdf_pages-raden har text NULL)"),
                    level="debug")
            return None
        reading_id: int = state_db.archive_page_reading(
            conn, pdf_stem=pdf_stem, page_num=page, reason=reason, **fields,
        )
    ctx.log(f"rescan {pdf_stem} s{page}: arkiverade läsning {reading_id} "
            f"({fields['engine']}, poäng {fields['score']}, orsak {reason}; {why})",
            level="debug")
    return reading_id


def _row_snapshot(pdf_stem: str, page: int) -> dict | None:
    """Den gällande ``pdf_pages``-raden som dict (för att se om ett försök ändrade den)."""
    with _connect() as conn:
        row = state_db.get_page_row(conn, pdf_stem, page)
    return dict(row) if row is not None else None


def _forget_failed_attempt(
    pdf_stem: str, page: int, reading_id: int | None, before: dict | None,
    ctx: OperationContext,
) -> None:
    """Ta bort historikraden som sparades före ett misslyckat eller avbrutet försök.

    Raden behålls om ``pdf_pages`` ändå hann ändras — då är den den enda kopian
    av läsningen som ersattes.
    """
    if reading_id is None:
        return
    if _row_snapshot(pdf_stem, page) != before:
        ctx.log(f"rescan {pdf_stem} s{page}: försöket misslyckades men pdf_pages "
                f"ändrades, historikrad {reading_id} behålls", level="warning")
        return
    with _connect() as conn:
        state_db.delete_page_reading(conn, reading_id)
    ctx.log(f"rescan {pdf_stem} s{page}: försöket misslyckades, historikrad "
            f"{reading_id} borttagen (gällande läsning orörd)", level="debug")


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
) -> None:
    """OCR:a sidan med ``ocr_pages.py --force``; ``OperationFailed`` vid fel.

    Den gällande raden i ``pdf_pages`` ersätts bara när sidan faktiskt lästs —
    ett misslyckat försök lämnar den orörd.
    """
    argv = [
        sys.executable, str(ROOT / "src" / "ocr_pages.py"),
        "--in", str(pdf), "--out-dir", str(root / "generated" / "text_pages"),
        "--engine", engine, "--pages", str(page), "--dpi", str(dpi), "--force",
    ]
    rc = ctx.run_process(argv, cwd=root)
    ctx.log(f"rescan {pdf.stem} s{page}: ocr_pages ({engine}) rc={rc}", level="debug")
    if rc == EXIT_SURYA_UNAVAILABLE:
        raise OperationFailed(
            f"Surya-runtimen gick inte att använda för {pdf.stem} sida {page} "
            f"(exitkod {rc}) — orsaken står i jobbloggen (Admin → Logg). "
            "Den gällande läsningen är orörd."
        )
    if rc != 0:
        raise OperationFailed(
            f"Omskanningen av {pdf.stem} sida {page} med {engine} misslyckades "
            f"(exitkod {rc}). Den gällande läsningen är orörd."
        )


def _restore(pdf_stem: str, page: int, reading, ctx: OperationContext) -> None:
    """Skriv den historiska läsningen som gällande rad i ``pdf_pages``."""
    with _connect() as conn:
        state_db.record_page(
            conn, pdf_stem=pdf_stem, page_num=page, engine=reading["engine"],
            text=reading["text"], score=reading["score"], markdown=reading["markdown"],
            layout_json=reading["layout_json"], engine_identity=reading["engine_identity"],
        )
    ctx.log(f"rescan {pdf_stem} s{page}: återställde läsning {reading['id']} "
            f"({reading['engine']}, poäng {reading['score']})", level="debug")


def _reindex(root: Path, pdf_stem: str, ctx: OperationContext) -> None:
    """Tvinga omindexering av dokumentet och kör inkrementell ingest.

    Ingest-raden tas bort först: en ny läsning kan ge byte-identisk text (och då
    oförändrad mtime) men ny Markdown, och utan raden räknas dokumentet som nytt.
    Övriga ingest-värden är registrets standardvärden för operationen ``ingest``.
    """
    from .adapters import ingest_adapter
    from .registry import get_registry

    with _connect() as conn:
        state_db.delete_ingest(conn, pdf_stem)
        conn.commit()
    ctx.log(f"rescan {pdf_stem}: ingest-raden borttagen, dokumentet indexeras om",
            level="debug")
    params: dict[str, object] = {
        parameter.name: parameter.default
        for parameter in get_registry().get("ingest").parameters
    }
    params.update(root=root, rebuild=False)
    ingest_adapter(ctx, params)


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
) -> int:
    """Skanna om en sida (eller återställ läsningen ``restore_reading``). Returnerar 0.

    All validering sker före första skrivningen; fel ger ``OperationFailed``.
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
        _archive_current(root, pdf_stem, page, "restore", ctx)
        _restore(pdf_stem, page, reading, ctx)
    else:
        reading = None
        pdf = _validate_page(root, pdf_stem, page, ctx)
        ctx.check_cancelled()
        ctx.log(f"rescan {pdf_stem} s{page}: motor {engine}, dpi {dpi}", level="debug")
        before = _row_snapshot(pdf_stem, page)
        reading_id = _archive_current(root, pdf_stem, page, "rescan", ctx)
        try:
            _ocr_page(root, pdf, page, engine, dpi, ctx)
        except BaseException:
            _forget_failed_attempt(pdf_stem, page, reading_id, before, ctx)
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

    if reindex:
        ctx.check_cancelled()
        _reindex(root, pdf_stem, ctx)
    else:
        ctx.log(f"rescan {pdf_stem}: ingen omindexering (--no-reindex)", level="debug")

    action = "Återställde" if reading is not None else "Skannade om"
    ctx.log(f"{action} {pdf_stem} sida {page}.")
    return 0
