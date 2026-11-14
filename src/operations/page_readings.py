"""Läsningshistorik för sidor, gemensam för ``rescan-page`` och ``rescan-pages``.

Gällande läsning är sidan i textfilen; ``pdf_pages`` är radens motor, Markdown och
layout. Historiken (``pdf_page_readings``) sparar en läsning innan den ersätts, så
att ingenting går förlorat och allt kan återställas.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from contextlib import closing
from pathlib import Path

import db as state_db
import llm_vision
from normalize_text import markdown_is_fresh, normalize
from page_review import WHOLE_FILE_ENGINE, file_page
from quality import score_text

from .context import OperationContext
from .exceptions import OperationFailed

ROOT = Path(__file__).resolve().parents[2]

# Motorerna som rescan-page och rescan-pages kan skanna om med.
ENGINES: tuple[str, ...] = ("surya-layout", "surya", "tesseract", "vision", "llm-vision")
# Motorn som kräver en LLM-profil (--profile); övriga motorer ignorerar profilen.
VISION_ENGINE = "llm-vision"


def connect():
    """En state.db-anslutning (med schema) som stängs av ``with``."""
    conn = state_db.connect()
    state_db.init_schema(conn)
    return closing(conn)


_connect = connect


def comparison_score(text: str) -> float:
    """Jämförelsepoäng för en sidtext: normaliserad text, utan hunspell."""
    return float(score_text(normalize(text), use_hunspell=False)["score"])


def archive_current(
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


def row_snapshot(pdf_stem: str, page: int) -> dict | None:
    """Den gällande ``pdf_pages``-raden som dict (för att se om ett försök ändrade den)."""
    with _connect() as conn:
        row = state_db.get_page_row(conn, pdf_stem, page)
    return dict(row) if row is not None else None


def forget_failed_attempt(
    pdf_stem: str, page: int, reading_id: int | None, before: dict | None,
    ctx: OperationContext,
) -> None:
    """Ta bort historikraden som sparades före ett misslyckat eller avbrutet försök.

    Raden behålls om ``pdf_pages`` ändå hann ändras — då är den den enda kopian
    av läsningen som ersattes.
    """
    if reading_id is None:
        return
    if row_snapshot(pdf_stem, page) != before:
        ctx.log(f"rescan {pdf_stem} s{page}: försöket misslyckades men pdf_pages "
                f"ändrades, historikrad {reading_id} behålls", level="warning")
        return
    with _connect() as conn:
        state_db.delete_page_reading(conn, reading_id)
    ctx.log(f"rescan {pdf_stem} s{page}: försöket misslyckades, historikrad "
            f"{reading_id} borttagen (gällande läsning orörd)", level="debug")


def restore_reading(
    pdf_stem: str, page: int, reading: Mapping, ctx: OperationContext) -> None:
    """Skriv den historiska läsningen som gällande rad i ``pdf_pages``."""
    with _connect() as conn:
        state_db.record_page(
            conn, pdf_stem=pdf_stem, page_num=page, engine=reading["engine"],
            text=reading["text"], score=reading["score"], markdown=reading["markdown"],
            layout_json=reading["layout_json"], engine_identity=reading["engine_identity"],
        )
    ctx.log(f"rescan {pdf_stem} s{page}: återställde läsning {reading['id']} "
            f"({reading['engine']}, poäng {reading['score']})", level="debug")


def reject_new_reading(
    root: Path, pdf_stem: str, page: int, old: Mapping, ctx: OperationContext,
    *, fallback_score: float | None = None,
) -> int:
    """Förkasta den nya läsningen och återinför ``old`` som gällande rad.

    ``fallback_score`` blir radens poäng när ``old`` saknar egen (en arkiverad
    textfilsida har ``score = NULL``) — jämförelsepoängen som avgjorde valet, så
    att Sidgranskningens motorrad inte visar "–" för den återinförda läsningen.

    Den gällande ``pdf_pages``-raden (den nya läsningen) arkiveras med orsaken
    ``rejected``; ``old`` (en rad formad som ``pdf_page_readings``) skrivs som
    gällande. Även en ``hel-fils-OCR``-läsning skrivs som rad — raden tas inte
    bort, så att Full OCR:s Surya-redo inte läser om sidan. Returnerar
    historikradens id.
    """
    with _connect() as conn:
        new = state_db.get_page_row(conn, pdf_stem, page)
        if new is None:
            raise ValueError(f"{pdf_stem} s{page}: ingen ny läsning att förkasta")
        new_id: int = state_db.archive_page_reading(
            conn, pdf_stem=pdf_stem, page_num=page, engine=new["engine"],
            text=new["text"], markdown=new["markdown"], layout_json=new["layout_json"],
            engine_identity=new["engine_identity"], score=new["score"],
            reason="rejected",
        )
        score = old["score"] if old["score"] is not None else fallback_score
        state_db.record_page(
            conn, pdf_stem=pdf_stem, page_num=page, engine=old["engine"],
            text=old["text"], score=score, markdown=old["markdown"],
            layout_json=old["layout_json"], engine_identity=old["engine_identity"],
        )
    ctx.log(f"rescan {pdf_stem} s{page}: förkastade ny läsning ({new['engine']}, "
            f"poäng {new['score']}), behåller gammal ({old['engine']}, poäng "
            f"{score}{' (jämförelsepoäng)' if old['score'] is None else ''}); "
            f"historikrad {new_id}", level="debug")
    return new_id


# --- Delat mellan rescan-page och rescan-pages ---------------------------------


def ocr_argv(root: Path, pdf: Path, pages: Iterable[int], engine: str, dpi: int,
             profile: str = "") -> list[str]:
    """Kommandoraden för ``ocr_pages.py --force`` över ``pages`` (``--profile`` för llm-vision)."""
    argv = [
        sys.executable, str(ROOT / "src" / "ocr_pages.py"),
        "--in", str(pdf), "--out-dir", str(root / "generated" / "text_pages"),
        "--engine", engine, "--pages", ",".join(str(p) for p in pages),
        "--dpi", str(dpi), "--force",
    ]
    if engine == VISION_ENGINE:
        argv += ["--profile", profile]
    return argv


def load_vision_profile(name: str) -> llm_vision.VisionProfile:
    """Lös LLM-profilen för llm-vision; ``OperationFailed`` om den saknas eller är okänd."""
    if not name:
        raise OperationFailed("Motorn llm-vision kräver en LLM-profil (--profile).")
    try:
        return llm_vision.load_profile(name)
    except ValueError as exc:
        raise OperationFailed(str(exc)) from exc


def check_vision_profile(profile: llm_vision.VisionProfile, ctx: OperationContext) -> None:
    """Logga vart sidbilderna skickas och gör bildkontrollen; ``OperationFailed`` vid fel.

    Ska köras före all skrivning och före första sidbilden.
    """
    ctx.log(f"llm-vision: profil {profile.name}, modell {profile.model} — sidbilder "
            f"skickas till {llm_vision.destination(profile)}")
    try:
        llm_vision.check_profile(profile)
    except llm_vision.VisionError as exc:
        raise OperationFailed(str(exc)) from exc
    ctx.log(f"llm-vision: bildkontrollen godkänd för profilen {profile.name}", level="debug")


def reindex_stems(root: Path, stems: Iterable[str], ctx: OperationContext) -> None:
    """Tvinga omindexering av dokumenten och indexera bara dem.

    Ingest-raderna tas bort först: en ny läsning kan ge byte-identisk text (och då
    oförändrad mtime) men ny Markdown, och utan raden räknas dokumentet som nytt.
    Ingest begränsas till dokumenten (``only_stems``): annars indexeras alla
    filer som blivit inaktuella sedan förra körningen om, vilket efter en
    OCR-körning kan vara hela arkivet. Övriga ingest-värden är registrets
    standardvärden för operationen ``ingest``.
    """
    from .adapters import ingest_adapter
    from .registry import get_registry

    wanted = sorted(set(stems))
    with _connect() as conn:
        for stem in wanted:
            state_db.delete_ingest(conn, stem)
        conn.commit()
    ctx.log(f"omindexering: ingest-raderna borttagna för {len(wanted)} dokument "
            f"({', '.join(wanted[:3])}{', …' if len(wanted) > 3 else ''}), de indexeras om",
            level="debug")
    params: dict[str, object] = {
        parameter.name: parameter.default
        for parameter in get_registry().get("ingest").parameters
    }
    params.update(root=root, rebuild=False, only_stems=set(wanted))
    ingest_adapter(ctx, params)
