"""Operationen ``rescan-pages``: massomskanning av sidor med låg kvalitetspoäng.

Urvalet (``select_pages``) är rent läsande. Körningen (``rescan_pages`` /
``run_rescan_pages``) tar ett dokument i taget — eller flera samtidigt för
``llm-vision``, som är nätverksbunden — och gör för varje dokument:

1. arkiverar den gällande läsningen av varje vald sida (``archive_current``),
2. OCR:ar sidorna med ``ocr_pages.py --pages … --force`` i en underprocess
   (``ctx.run_process``: SIGTERM-hanteraren i ``ocr_pages.py`` stänger
   Surya-workern vid avbrott),
3. avgör utfallet per sida ur ``pdf_pages``-radens ögonblicksbild före och efter:
   oförändrad rad = misslyckad sida (historikraden tas bort, gällande läsning
   orörd); annars jämförs den nya läsningen mot den arkiverade med
   ``comparison_score``; bara en strikt högre poäng vinner, en sämre eller lika bra ny läsning förkastas (``reject_new_reading``),
4. slår in sidorna som fick en ny gällande läsning i textfilen
   (``merge_and_normalize(only_pages=…)``).

I slutet poängsätts de berörda dokumenten om (``quality.run_quality``) och —
med ``reindex`` — indexeras om. Avbrott: exitkod 3 (Surya saknas), exitkod 4
(LLM-tjänsten vägrar) och fem dokument i rad där varje sida misslyckades ger
``OperationFailed``; enstaka misslyckade sidor ger bara exitkod 1 i slutet.
Vid ``OperationFailed`` poängsätts och indexeras det som hann skannas; vid
``OperationCancelled`` poängsätts det bara (ingen omindexering) innan avbrottet
kastas vidare.

Trådar: ``ctx.log`` har inget eget tillstånd och skriver direkt till sinken;
jobbets ``JobSink`` serialiserar skrivningarna med ett lås, och
``ctx.run_process`` startar en egen process och läsartråd per anrop. Progress
skrivs däremot till SQLite i jobbets anslutning och rapporteras därför bara från
huvudtråden.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import db as state_db
from llm_vision import EXIT_LLM_FATAL
from page_review import WHOLE_FILE_ENGINE, find_pdf, is_removed_duplicate, page_count
from surya_client import EXIT_SURYA_UNAVAILABLE

from .context import OperationContext, ensure_terminal_context
from .exceptions import OperationCancelled, OperationFailed
from .page_readings import (
    ENGINES,
    VISION_ENGINE,
    archive_current,
    check_vision_profile,
    comparison_score,
    forget_failed_attempt,
    load_vision_profile,
    ocr_argv,
    reindex_stems,
    reject_new_reading,
    rescore_documents,
    row_snapshot,
)
from .page_readings import connect as _connect

# Så många dokument i rad där varje sida misslyckades avbryter körningen.
MAX_FAILED_DOCUMENTS_IN_ROW = 5
# Attribut på ett fel från ett dokument: dokumentets delresultat (``_DocResult``).
_PARTIAL_ATTR = "rescan_partial_result"

# Antal exempel per skäl som visas i debugloggen.
_EXEMPEL = 3


def select_document_pages(
    root: Path, pdf_stem: str, *, max_pages: int, ctx: OperationContext,
    engine: str, identity: str | None,
) -> dict[str, list[int]]:
    """Dokumentläget: sidorna 1…N i ``pdf_stem`` (N enligt PDF:en).

    Poängtröskel och källa gäller inte — användaren har valt dokumentet. Sidor som
    redan lästs med ``engine`` (gällande läsning eller historik, även underkända;
    ``identity`` måste stämma när den anges) hoppas över, så att en upprepad
    omskanning bara tar de sidor som återstår. ``max_pages`` begränsar som vanligt
    (0 = alla) och räknas efter bortvalet. En raderad dubblett eller saknad PDF ger
    ``OperationFailed`` före all skrivning.
    """
    with _connect() as conn:
        row = state_db.get_pdf_file(conn, pdf_stem)
        if row is not None and is_removed_duplicate(root, pdf_stem,
                                                    row["tesseract_done_at"]):
            raise OperationFailed(
                f"{pdf_stem} är en raderad dubblett (merge_wpu behöll den andra "
                "kopian) — skanna om den kopia som finns kvar.")
        pdf = find_pdf(root, pdf_stem)
        if pdf is None:
            raise OperationFailed(f"Hittar ingen PDF för {pdf_stem}.")
        total = page_count(pdf)
        already = [p for p in range(1, total + 1)
                   if state_db.page_read_with(conn, pdf_stem, p, engine=engine,
                                              engine_identity=identity)]
    pages = [p for p in range(1, total + 1) if p not in set(already)]
    if max_pages:
        pages = pages[:max_pages]
    if already:
        ctx.log(f"urval (dokumentläge): {len(already)} bortvalda (redan läst med "
                f"{engine}), t.ex. "
                f"{', '.join(f'{pdf_stem} s{p}' for p in already[:_EXEMPEL])}",
                level="debug")
    ctx.log(f"urval (dokumentläge): {pdf_stem}, {len(pages)} av {total} sidor — "
            "tröskel och källa gäller inte", level="debug")
    if not pages and already:
        ctx.log(f"{pdf_stem}: alla {len(already)} sidor har redan lästs med {engine}.")
    return {pdf_stem: pages} if pages else {}


def select_pages(
    root: Path, *, engine: str, identity: str | None, threshold: float,
    source: str, max_pages: int, ctx: OperationContext,
) -> dict[str, list[int]]:
    """Sidor att skanna om: dokument → sidnummer (stigande), sämst poäng först.

    Bort faller raderade dubbletter, dokument vars PDF saknas och sidor som redan
    lästs med ``engine`` (``identity`` måste också stämma när den anges, som för
    ``llm-vision``). ``max_pages`` begränsar totalt antal sidor (0 = alla); de
    sämsta sidorna tas först.
    """
    skipped: dict[str, list[str]] = {"dubblett": [], "PDF saknas": [], "redan läst": []}
    chosen: dict[str, list[int]] = {}
    total = 0
    # Per dokument: kan det väljas alls? (cachar dubblett- och PDF-kontrollen).
    doc_ok: dict[str, bool] = {}
    with _connect() as conn:
        rows = state_db.list_rescan_candidates(
            conn, threshold=threshold, source=None if source == "any" else source
        )
        ctx.log(f"urval: {len(rows)} sidor under poäng {threshold:g} "
                f"(källa {source})", level="debug")
        for row in rows:
            stem, page = row["pdf_stem"], row["page_num"]
            if stem not in doc_ok:
                if is_removed_duplicate(root, stem, row["tesseract_done_at"]):
                    skipped["dubblett"].append(stem)
                    doc_ok[stem] = False
                elif find_pdf(root, stem) is None:
                    skipped["PDF saknas"].append(stem)
                    doc_ok[stem] = False
                else:
                    doc_ok[stem] = True
            if not doc_ok[stem]:
                continue
            if state_db.page_read_with(conn, stem, page, engine=engine,
                                       engine_identity=identity):
                skipped["redan läst"].append(f"{stem} s{page}")
                continue
            if max_pages and total >= max_pages:
                break
            chosen.setdefault(stem, []).append(page)
            total += 1
    for pages in chosen.values():
        pages.sort()
    for reason, items in skipped.items():
        if items:
            ctx.log(f"urval: {len(items)} bortvalda ({reason}), t.ex. "
                    f"{', '.join(items[:_EXEMPEL])}", level="debug")
    ctx.log(f"urval: {total} sidor i {len(chosen)} dokument valda "
            f"(motor {engine}, max_pages {max_pages or 'alla'})", level="debug")
    return chosen


# --- Körningen ---------------------------------------------------------------


@dataclass
class RescanSummary:
    """Utfallet av en körning, räknat i sidor."""

    selected: int = 0
    scanned: int = 0
    improved: int = 0
    kept_old: int = 0
    failed: int = 0
    # Kvalitetsbedömningen av de berörda dokumenten misslyckades.
    quality_failed: bool = False

    @property
    def exit_code(self) -> int:
        """0 när ingen sida (och inte kvalitetssteget) misslyckades, annars 1."""
        return 1 if self.failed or self.quality_failed else 0


@dataclass
class _DocResult:
    stem: str
    scanned: int = 0
    improved: int = 0
    kept_old: int = 0
    failed: int = 0

    @property
    def touched(self) -> bool:
        """Sant när någon sida fick ny gällande läsning eller raden skrevs om."""
        return bool(self.improved or self.kept_old)

    @property
    def all_failed(self) -> bool:
        return self.scanned > 0 and self.failed == self.scanned


def _resolve_page(
    root: Path, stem: str, page: int, before: dict | None, reading_id: int | None,
    ctx: OperationContext, *, force: bool = False,
) -> str:
    """Avgör sidans utfall efter OCR:en: ``improved``, ``kept_old`` eller ``failed``."""
    after = row_snapshot(stem, page)
    if after == before or after is None or after["text"] is None:
        forget_failed_attempt(stem, page, reading_id, before, ctx)
        ctx.log(f"rescan-pages {stem} s{page}: ingen ny läsning (pdf_pages-raden "
                "oförändrad) — sidan räknas som misslyckad", level="debug")
        return "failed"
    new_score = comparison_score(after["text"])
    old = None
    if reading_id is not None:
        with _connect() as conn:
            old = state_db.get_page_reading(conn, reading_id)
    if old is None or old["text"] is None:
        ctx.log(f"rescan-pages {stem} s{page}: ny läsning ({after['engine']}, poäng "
                f"{new_score:.1f}) — ingen tidigare läsning att jämföra med, behåller ny",
                level="debug")
        return "improved"
    old_score = comparison_score(old["text"])
    # Bara en strikt högre poäng är ett belägg för att den nya läsningen är bättre.
    # Lika poäng (t.ex. 0 mot 0 för en kodlista som heuristiken inte kan bedöma)
    # behåller den gällande läsningen; den nya sparas som underkänd i historiken.
    # ``force``: användaren har valt bort jämförelsen — den nya läsningen blir
    # gällande ändå, och den gamla finns kvar i historiken för återställning.
    keep_new = force or new_score > old_score
    verdict = ("force, behåller ny" if force and new_score <= old_score
               else "behåller ny" if keep_new
               else "lika poäng, behåller gammal" if new_score == old_score
               else "behåller gammal")
    ctx.log(f"rescan-pages {stem} s{page}: ny {after['engine']} poäng {new_score:.1f} "
            f"mot gammal {old['engine']} poäng {old_score:.1f} → {verdict}", level="debug")
    if keep_new:
        return "improved"
    reject_new_reading(root, stem, page, old, ctx, fallback_score=old_score)
    return "kept_old"


def _rescan_document(
    root: Path, stem: str, pages: list[int], *, engine: str, profile: str, dpi: int,
    ctx: OperationContext, stop: threading.Event, force: bool = False,
) -> _DocResult | None:
    """Skanna om dokumentets sidor; ``None`` om körningen redan stoppats.

    Varje arkiverad läsning får ett utfall även när OCR:en avbryts eller
    avbrottskoderna 3/4 kommer: sidor vars rad ändrades jämförs och slås ihop,
    övriga får sin historikrad borttagen. Först därefter kastas felet vidare.
    """
    if stop.is_set():
        return None
    ctx.check_cancelled()
    result = _DocResult(stem=stem)
    pdf = find_pdf(root, stem)
    if pdf is None:
        ctx.log(f"rescan-pages {stem}: PDF:en saknas — {len(pages)} sidor hoppas över",
                level="warning")
        result.scanned = result.failed = len(pages)
        return result

    attempts: list[tuple[int, dict | None, int | None]] = []
    try:
        for page in pages:
            before = row_snapshot(stem, page)
            attempts.append((page, before, archive_current(root, stem, page, "rescan", ctx)))
    except BaseException:
        for page, before, reading_id in attempts:
            forget_failed_attempt(stem, page, reading_id, before, ctx)
        raise

    interrupted: BaseException | None = None
    rc: int | None = None
    try:
        rc = ctx.run_process(ocr_argv(root, pdf, pages, engine, dpi, profile), cwd=root)
    except BaseException as exc:  # noqa: BLE001 - utfallet avgörs innan felet kastas vidare
        interrupted = exc
    ctx.log(f"rescan-pages {stem}: ocr_pages ({engine}, sidor "
            f"{','.join(map(str, pages))}) rc={rc}", level="debug")

    new_pages: set[int] = set()
    pending = list(attempts)
    try:
        while pending:
            page, before, reading_id = pending[0]
            outcome = _resolve_page(root, stem, page, before, reading_id, ctx, force=force)
            pending.pop(0)
            result.scanned += 1
            if outcome == "improved":
                result.improved += 1
                new_pages.add(page)
            elif outcome == "kept_old":
                result.kept_old += 1
            else:
                result.failed += 1
    except BaseException:
        for page, before, reading_id in pending:
            forget_failed_attempt(stem, page, reading_id, before, ctx)
        raise
    finally:
        if new_pages:
            from .ocr import merge_and_normalize

            has_text = merge_and_normalize(stem, root / "generated" / "text",
                                           only_pages=new_pages)
            ctx.log(f"rescan-pages {stem}: merge + normalize av sidorna "
                    f"{sorted(new_pages)} klar (textfil har innehåll: {has_text})",
                    level="debug")

    ctx.log(f"{stem}: {result.improved} förbättrade, {result.kept_old} behöll gammal, "
            f"{result.failed} misslyckades")
    error: BaseException | None = interrupted
    if error is None and rc == EXIT_SURYA_UNAVAILABLE:
        error = OperationFailed(
            f"Surya-runtimen gick inte att använda för {stem} (exitkod {rc}) — orsaken "
            "står i jobbloggen (Admin → Logg). Sidor som inte lästs har kvar sin "
            "gällande läsning."
        )
    elif error is None and rc == EXIT_LLM_FATAL:
        error = OperationFailed(
            f"LLM-tjänsten gick inte att använda för {stem} (exitkod {rc}: profil, "
            "nyckel, kredit eller kvot) — orsaken står i jobbloggen (Admin → Logg). "
            "Sidor som inte lästs har kvar sin gällande läsning."
        )
    if error is not None:
        # Sidor som hann läsas är redan jämförda och sammanslagna: utfallet följer
        # med felet så att dokumentet räknas och kvalitetsbedöms/indexeras.
        setattr(error, _PARTIAL_ATTR, result)
        raise error
    return result


def _partial_result(exc: BaseException) -> _DocResult | None:
    """Delresultatet som ``_rescan_document`` fäste vid felet, eller ``None``."""
    result = getattr(exc, _PARTIAL_ATTR, None)
    return result if isinstance(result, _DocResult) else None


def _log_dry_run(chosen: dict[str, list[int]], engine: str, ctx: OperationContext) -> None:
    current: Counter[str] = Counter()
    with _connect() as conn:
        for stem, pages in chosen.items():
            for page in pages:
                row = state_db.get_page_row(conn, stem, page)
                current[str(row["engine"]) if row is not None and row["text"] is not None
                        else WHOLE_FILE_ENGINE] += 1
    total = sum(current.values())
    spread = ", ".join(f"{name} {count}" for name, count in current.most_common())
    ctx.log(f"Torrkörning: {total} sidor i {len(chosen)} dokument skulle skannas om "
            f"med {engine} (gällande motor: {spread or 'inga'}). Inget skrevs.")
    for stem, pages in list(chosen.items())[:10]:
        ctx.log(f"torrkörning: {stem} sidor {','.join(map(str, pages))}", level="debug")


def _rescore(root: Path, stems: list[str], ctx: OperationContext) -> bool:
    """Kvalitetsbedöm ``stems`` (per sida). Sant när kvalitetssteget gav exitkod 0."""
    return rescore_documents(root, stems, ctx, label="rescan-pages")


def _rescore_and_reindex(root: Path, touched: set[str], reindex: bool,
                         ctx: OperationContext) -> bool:
    """Kvalitet för de berörda dokumenten och (valbart) omindexering. Sant om allt gick."""
    if not touched:
        ctx.log("rescan-pages: inga dokument fick ny text — kvalitet och index orörda",
                level="debug")
        return True
    stems = sorted(touched)
    ok = _rescore(root, stems, ctx)
    if not reindex:
        ctx.log("rescan-pages: ingen omindexering (--no-reindex)", level="debug")
        return ok
    ctx.check_cancelled()
    reindex_stems(root, stems, ctx)
    return ok


def _run_documents(
    chosen: dict[str, list[int]], worker: Callable[[str, list[int]], _DocResult | None],
    *, parallel: int, record: Callable[[_DocResult], None], ctx: OperationContext,
    stop: threading.Event,
) -> None:
    """Kör ``worker`` per dokument, sekventiellt eller i ``parallel`` trådar.

    Vid fel eller avbrott schemaläggs inga fler dokument; pågående dokument får
    slutföras (och bokföras) innan felet kastas vidare. Ett dokument som avbröt
    körningen bokförs med sitt delresultat. ``stop`` sätts före den bokföringen,
    så att ``record`` inte kastar ett andra fel.
    """
    total = len(chosen)
    if parallel <= 1:
        for done, (stem, pages) in enumerate(chosen.items(), 1):
            try:
                result = worker(stem, pages)
            except BaseException as exc:
                stop.set()
                partial = _partial_result(exc)
                if partial is not None:
                    record(partial)
                raise
            if result is not None:
                record(result)
            ctx.progress(done, total, stem)
        return
    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="rescan-pages") as pool:
        futures: dict[Future, str] = {
            pool.submit(worker, stem, pages): stem for stem, pages in chosen.items()
        }
        handled: set[Future] = set()
        try:
            for done, future in enumerate(as_completed(futures), 1):
                handled.add(future)
                result = future.result()
                if result is not None:
                    record(result)
                ctx.progress(done, total, futures[future])
        except BaseException as exc:
            stop.set()
            partial = _partial_result(exc)
            if partial is not None:
                record(partial)
            for future in futures:
                future.cancel()
            for future in futures:
                if future in handled or future.cancelled():
                    continue
                try:
                    result = future.result()
                except BaseException as later:  # noqa: BLE001 - första felet rapporteras
                    result = _partial_result(later)
                if result is not None:
                    record(result)
            raise


def rescan_pages(
    *, root: Path, engine: str, profile: str, threshold: float, source: str,
    max_pages: int, dry_run: bool, reindex: bool, jobs: int, dpi: int, context=None,
    pdf_stem: str | None = None, force: bool = False,
) -> RescanSummary:
    """Skanna om sidorna under ``threshold`` och behåll den bättre läsningen per sida."""
    ctx = ensure_terminal_context(context)
    root = Path(root)
    if engine not in ENGINES:
        raise OperationFailed(f"Okänd motor {engine!r} (giltiga: {', '.join(ENGINES)}).")
    vision = None
    identity: str | None = None
    if engine == VISION_ENGINE:
        vision = load_vision_profile(profile)
        identity = f"llm-vision:{vision.name}:{vision.model}"
    elif profile:
        ctx.log(f"rescan-pages: profilen {profile!r} ignoreras för motorn {engine}",
                level="debug")

    ctx.step("Välj sidor")
    if pdf_stem:
        chosen = select_document_pages(root, pdf_stem, max_pages=max_pages, ctx=ctx,
                                       engine=engine, identity=identity)
    else:
        chosen = select_pages(root, engine=engine, identity=identity, threshold=threshold,
                              source=source, max_pages=max_pages, ctx=ctx)
    summary = RescanSummary(selected=sum(len(p) for p in chosen.values()))
    if not chosen:
        ctx.log(f"{pdf_stem} har inga sidor att skanna om." if pdf_stem else
                f"Inga sidor under poäng {threshold:g} att skanna om med {engine}.")
        return summary
    if dry_run:
        _log_dry_run(chosen, engine, ctx)
        return summary

    if vision is not None:
        check_vision_profile(vision, ctx)
    parallel = max(1, jobs) if engine == VISION_ENGINE else 1
    ctx.log(f"rescan-pages: {summary.selected} sidor i {len(chosen)} dokument, motor "
            f"{engine}, dpi {dpi}, {parallel} dokument åt gången"
            + (", force: ny läsning ersätter alltid den gamla" if force else ""),
            level="debug")

    touched: set[str] = set()
    failed_in_row = 0
    stop = threading.Event()

    def record(result: _DocResult) -> None:
        nonlocal failed_in_row
        summary.scanned += result.scanned
        summary.improved += result.improved
        summary.kept_old += result.kept_old
        summary.failed += result.failed
        if result.touched:
            touched.add(result.stem)
        failed_in_row = failed_in_row + 1 if result.all_failed else 0
        if failed_in_row >= MAX_FAILED_DOCUMENTS_IN_ROW and not stop.is_set():
            stop.set()
            raise OperationFailed(
                f"{failed_in_row} dokument i rad kunde inte läsas alls (senast "
                f"{result.stem}) — körningen avbryts. Orsaken står i jobbloggen "
                "(Admin → Logg); de gällande läsningarna är orörda."
            )

    def worker(stem: str, pages: list[int]) -> _DocResult | None:
        return _rescan_document(root, stem, pages, engine=engine, profile=profile,
                                dpi=dpi, ctx=ctx, stop=stop, force=force)

    ctx.step("Skanna om sidor", total=len(chosen))
    try:
        _run_documents(chosen, worker, parallel=parallel, record=record, ctx=ctx, stop=stop)
    except OperationFailed:
        _log_summary(summary, ctx, prefix="Avbruten: ")
        # Det som redan skannats ska ändå få aktuella poäng (och index).
        try:
            _rescore_and_reindex(root, touched, reindex, ctx)
        except Exception as exc:  # noqa: BLE001 - avbrottsorsaken är felet som rapporteras
            ctx.log(f"rescan-pages: kvalitet/index efter avbrottet misslyckades: {exc}",
                    level="error")
        raise
    except OperationCancelled:
        _log_summary(summary, ctx, prefix="Avbruten: ")
        # Förbättrade sidor är redan inslagna i textfilen: poängsätt om dem så att
        # quality_pages (och Sidgranskning) stämmer. Ingen omindexering — den är
        # lång och avbrottet ska respekteras; ingest tar dokumenten vid nästa körning.
        if touched:
            ctx.log(f"rescan-pages: avbruten — kvalitetsbedömer {len(touched)} berörda "
                    "dokument men indexerar inte om", level="debug")
            try:
                _rescore(root, sorted(touched), ctx.without_cancellation())
            except Exception as exc:  # noqa: BLE001 - avbrottet är det som rapporteras
                ctx.log(f"rescan-pages: kvalitet efter avbrottet misslyckades: {exc}",
                        level="error")
        raise

    ctx.step("Kvalitet och index")
    summary.quality_failed = not _rescore_and_reindex(root, touched, reindex, ctx)
    _log_summary(summary, ctx)
    return summary


def _log_summary(summary: RescanSummary, ctx: OperationContext, *, prefix: str = "") -> None:
    ctx.log(f"{prefix}Skanna om sidor: {summary.selected} valda, {summary.scanned} "
            f"skannade, {summary.improved} förbättrade, {summary.kept_old} behöll gammal, "
            f"{summary.failed} misslyckades.")


def run_rescan_pages(
    *, root: Path, engine: str, profile: str, threshold: float, source: str,
    max_pages: int, dry_run: bool, reindex: bool, jobs: int, dpi: int, context=None,
    pdf_stem: str | None = None, force: bool = False,
) -> int:
    """Kör ``rescan_pages``; 0 när ingen sida misslyckades, annars 1."""
    summary = rescan_pages(root=root, engine=engine, profile=profile, threshold=threshold,
                           source=source, max_pages=max_pages, dry_run=dry_run,
                           reindex=reindex, jobs=jobs, dpi=dpi, context=context,
                           pdf_stem=pdf_stem, force=force)
    return summary.exit_code
