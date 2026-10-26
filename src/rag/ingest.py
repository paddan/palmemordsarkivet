#!/usr/bin/env python3
"""
Indexera textfiler från ../text/ till en lokal LanceDB-vektor-DB.

Kör:
    python ingest.py            # indexera alla nya filer
    python ingest.py --rebuild  # börja om från noll
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db as state_db  # noqa: E402
from normalize_text import markdown_is_fresh, normalize_markdown  # noqa: E402

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

ROOT = Path(__file__).resolve().parents[2]
TEXT_DIR = ROOT / "generated" / "text"
DB_DIR = ROOT / "generated" / "lancedb"
TABLE = "chunks"
MODEL_NAME = "intfloat/multilingual-e5-large"
EMBED_DIM = 1024

CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
MIN_CHUNK_CHARS = 80  # kortare chunks indexeras inte (is_useful)
MIN_ALNUM_RATIO = 0.55  # filtrera OCR-skräp

# Filnamn: "<Nr> — <Titel> — <Beställt> — <Upplagt> — <Anmärkning> — <Sidor>.txt"
NAME_FIELDS = ["nr", "titel", "bestallt", "upplagt", "anmarkning", "antal_sidor"]


def parse_filename(stem: str) -> dict:
    parts = [p.strip() for p in stem.split(" — ")]
    out = {f: "" for f in NAME_FIELDS}
    for f, v in zip(NAME_FIELDS, parts, strict=False):
        out[f] = v
    if not out["nr"]:
        out["nr"] = stem[:40]
    return out


def split_pages(text: str) -> list[str]:
    # pdftotext -layout separerar sidor med \f
    return text.split("\f") if "\f" in text else [text]


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Returnerar (start, end, chunk) — bryter helst på radslut.

    OBS: chunkar INOM en sida; ``\f`` får aldrig förekomma i text som skickas hit.
    Korta sidor blir egna (en) chunk.
    """
    # Kollapsa multipla horisontella blanksteg (pdftotext -layout-artefakter)
    # men bevara radbrytningar (används vid chunk-gränsval).
    text = re.sub(r"[^\S\n]+", " ", text)
    text = text.strip()
    if not text:
        return []
    if "\f" in text:
        # defensiv: callern ska ha splittat först
        text = text.replace("\f", " ")
    if len(text) <= size:
        return [(0, len(text), text)]
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            # backa till närmsta radbrytning eller mellanslag
            window = text.rfind("\n", i + size // 2, end)
            if window == -1:
                window = text.rfind(" ", i + size // 2, end)
            if window != -1 and window > i + 100:
                end = window
        chunk = text[i:end].strip()
        if chunk:
            chunks.append((i, end, chunk))
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return chunks


# --- Markdown från Surya-layout ---------------------------------------------

_SIDHUVUD = "> Sidhuvud:"
# Replikgräns: talarmarkör i fetstil först på raden (``**F:**``, ``**Fråga:**``,
# ``**[MASKAD]**``). Kortnamn i versaler (1–3 tecken) täcker F/S/FL/M/J m.fl.
_TURN_RE = re.compile(r"^\*\*(?:(?:[A-ZÅÄÖ]{1,3}|Fråga|Svar):|\[MASKAD\]:?)\*\*")
_QUESTION_RE = re.compile(r"^\*\*(?:F|FL|Fråga):\*\*")
_TABLE_SEP_RE = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
_FORM_FIELD_PREFIX = "- **"


def _md_blocks(body: str, base: int) -> list[tuple[int, int, str]]:
    """Dela vid tomrader. Returnerar ``(start, end, block)`` med offset i sidans Markdown."""
    blocks: list[tuple[int, int, str]] = []
    pos = 0
    start: int | None = None
    end = 0
    for line in body.split("\n"):
        if line.strip():
            if start is None:
                start = pos
            end = pos + len(line)
        elif start is not None:
            blocks.append((base + start, base + end, body[start:end]))
            start = None
        pos += len(line) + 1
    if start is not None:
        blocks.append((base + start, base + end, body[start:end]))
    return blocks


def _is_table(lines: list[str]) -> bool:
    return (
        len(lines) >= 2
        and all(ln.startswith("|") for ln in lines)
        and bool(_TABLE_SEP_RE.match(lines[1]))
    )


def _is_form(lines: list[str]) -> bool:
    top = [ln for ln in lines if not ln.startswith(" ")]
    return bool(top) and lines[0].startswith(_FORM_FIELD_PREFIX) and all(
        ln.startswith(_FORM_FIELD_PREFIX) for ln in top
    )


def _split_turns(start: int, text: str) -> list[tuple[int, int, str]]:
    """Dela ett block före varje rad som inleder en ny replik."""
    parts: list[tuple[int, int, str]] = []
    cur_start = 0
    pos = 0
    for i, line in enumerate(text.split("\n")):
        if i and _TURN_RE.match(line):
            parts.append((start + cur_start, start + pos - 1, text[cur_start:pos - 1]))
            cur_start = pos
        pos += len(line) + 1
    parts.append((start + cur_start, start + len(text), text[cur_start:]))
    return parts


def _split_words(text: str, budget: int) -> list[str]:
    """Dela ``text`` vid blanksteg i bitar om högst ``budget`` tecken.

    Ingen token kapas — utom ett enskilt ord som ensamt är längre än ``budget``,
    som då delas hårt (annars kan biten aldrig rymmas).
    """
    pieces: list[str] = []
    cur = ""
    for word in text.split():
        while len(word) > budget:
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(word[:budget])
            word = word[budget:]
        if not word:
            continue
        if cur and len(cur) + 1 + len(word) > budget:
            pieces.append(cur)
            cur = ""
        cur = f"{cur} {word}" if cur else word
    if cur:
        pieces.append(cur)
    return pieces


def _split_row(row: str, budget: int) -> list[str]:
    """Dela en för lång tabellrad vid cellgränser (``|``), i sista hand vid ordgränser.

    Bitarna skrivs som radfragment ``| cell | cell |``; en cell som ensam inte
    ryms delas vid ordgränser.
    """
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    parts: list[str] = []
    for cell in cells:
        if len(cell) + 4 <= budget:
            parts.append(cell)
        else:
            parts += _split_words(cell, max(1, budget - 4))
    pieces: list[str] = []
    cur: list[str] = []
    for part in parts:
        if cur and len("| " + " | ".join([*cur, part]) + " |") > budget:
            pieces.append("| " + " | ".join(cur) + " |")
            cur = []
        cur.append(part)
    if cur:
        pieces.append("| " + " | ".join(cur) + " |")
    return pieces


def _split_group(g: str, budget: int) -> list[str]:
    """Dela en radgrupp som ensam är större än ``budget`` utan att kapa någon token."""
    if g.startswith("|") and "\n" not in g:
        return _split_row(g, budget)
    if "\n" in g:
        # blankettfält med alternativrader: dela mellan raderna i första hand
        out: list[str] = []
        for line in g.split("\n"):
            if len(line) <= budget:
                out.append(line)
            else:
                out += _split_words(line, budget)
        return _pack_lines(out, budget)
    return _split_words(g, budget)


def _pack_lines(
    groups: list[str], budget: int, prefix: str = ""
) -> list[str]:
    """Packa radgrupper (fält, tabellrader) girigt till bitar om högst ``budget`` tecken.

    ``prefix`` (tabellens rubrik + avdelare) inleder en bit bara när det lämnar
    plats åt minst en hel rad. En grupp som inte ryms ens ensam delas med
    ``_split_group`` (tabellrad vid cellgränser, annars vid ordgränser) och dess
    fragment får inget prefix. Kom prefixet aldrig med (ingen rad ryms bredvid
    rubriken) packas tabellen om med rubrik och avdelare som vanliga rader, så
    att kolumnnamnen ändå indexeras (regel AJ).
    """
    items: list[tuple[str, bool]] = []  # (text, får inledas med prefix)
    for g in groups:
        if len(g) <= budget:
            items.append((g, True))
        else:
            items += [(piece, False) for piece in _split_group(g, budget)]

    pieces: list[str] = []
    cur: list[str] = []
    used_prefix = False

    def fits(lines: list[str]) -> bool:
        return len("\n".join(lines)) <= budget

    for text, may_prefix in items:
        with_prefix = bool(prefix) and may_prefix and fits([prefix, text])
        # en bit utan rubrik (startad av en rad som inte rymdes bredvid den)
        # fortsätter inte med rader som kan få rubriken framför sig
        headless = bool(cur) and bool(prefix) and cur[0] != prefix
        if cur and fits([*cur, text]) and not (headless and with_prefix):
            cur.append(text)
            continue
        if cur:
            pieces.append("\n".join(cur))
        if with_prefix:
            cur = [prefix, text]
            used_prefix = True
        else:
            cur = [text]
    if cur:
        pieces.append("\n".join(cur))
    if prefix and not used_prefix:
        return _pack_lines(prefix.split("\n") + groups, budget)
    return pieces


def _oversized(kind: str, text: str, budget: int, overlap: int) -> list[str]:
    """Dela ett block som inte ryms i en chunk efter dess struktur."""
    lines = text.split("\n")
    if kind == "table" and len(lines) > 2:
        # mellan rader; rubrik + avdelare upprepas, ingen överlapp inuti tabellen
        return _pack_lines(lines[2:], budget, prefix=lines[0] + "\n" + lines[1])
    if kind == "form":
        fields: list[str] = []
        for ln in lines:
            if ln.startswith(" ") and fields:
                fields[-1] += "\n" + ln
            else:
                fields.append(ln)
        return _pack_lines(fields, budget)
    return [c for _, _, c in chunk_text(text, budget, overlap)]


def chunk_markdown(md: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Strukturmedveten chunkning av en layoutsidas Markdown.

    Returnerar ``(start, end, chunk)`` som ``chunk_text``; offset pekar på
    innehållet i ``md`` (ungefärliga för block som delats med ``chunk_text``).

    - Gränser läggs vid tomrader (block) och före varje ny replik.
    - Block som ryms packas ihop (skilda med tomrad) upp till ``size``.
    - Ett för stort block delas: tabeller mellan rader med rubrik och avdelare
      upprepade (ingen överlapp inuti tabellen), blankettfält mellan fält
      (kryssrutealternativen följer sitt fält), övrigt med ``chunk_text``.
    - En fråga (``**F:**``, ``**FL:**``, ``**Fråga:**``) hålls ihop med nästa
      replik — inklusive löpande stycken emellan — när paret ryms.
    - En inledande rad ``> Sidhuvud: …`` tas ur flödet och läggs först i varje
      chunk. Den räknas mot ``size``; är den minst halva ``size`` kortas den till
      en fjärdedel.
    """
    md = md.strip()
    if not md:
        return []
    header = ""
    base = 0
    body = md
    if md.startswith(_SIDHUVUD):
        first, _, body = md.partition("\n")
        header = first.rstrip()
        if len(header) >= size / 2:
            header = header[: size // 4]
        base = len(first) + 1
    budget = size - len(header) - 1 if header else size

    # 1. Enheter: (typ, start, end, text)
    units: list[tuple[str, int, int, str]] = []
    for start, end, block in _md_blocks(body, base):
        lines = block.split("\n")
        if _is_table(lines):
            parts = [("table", start, end, block)]
        elif _is_form(lines):
            parts = [("form", start, end, block)]
        else:
            parts = [
                ("question" if _QUESTION_RE.match(t) else "turn" if _TURN_RE.match(t) else "text", s, e, t)
                for s, e, t in _split_turns(start, block)
            ]
        for kind, s, e, t in parts:
            if len(t) <= budget:
                units.append((kind, s, e, t))
            else:
                units += [(kind, s, e, piece) for piece in _oversized(kind, t, budget, overlap)]

    # 2. Fråga + svar: slå ihop frågan, löpande stycken och nästa replik när de ryms
    merged: list[tuple[str, int, int, str]] = []
    i = 0
    while i < len(units):
        kind, s, e, t = units[i]
        if kind == "question":
            j = i + 1
            while j < len(units) and units[j][0] == "text":
                j += 1
            # en fråga paras bara med ett icke-frågande svar; fråga följd av fråga
            # står för sig (den andra frågan paras i sin tur med sitt svar)
            if j < len(units) and units[j][0] == "turn":
                pair = "\n\n".join(u[3] for u in units[i:j + 1])
                if len(pair) <= budget:
                    merged.append(("turn", s, units[j][2], pair))
                    i = j + 1
                    continue
        merged.append(units[i])
        i += 1

    # 3. Packa girigt upp till budget
    chunks: list[tuple[int, int, str]] = []
    cur: list[tuple[str, int, int, str]] = []
    cur_len = 0
    for u in merged:
        add = len(u[3]) + (2 if cur else 0)
        if cur and cur_len + add > budget:
            chunks.append(_md_chunk(cur))
            cur, cur_len, add = [], 0, len(u[3])
        cur.append(u)
        cur_len += add
    if cur:
        chunks.append(_md_chunk(cur))
    # 4. Ingen enhet får falla ur indexet (regel AJ)
    chunks = _rescue_short(chunks, budget, overlap)
    return [(s, e, f"{header}\n{c}" if header else c) for s, e, c in chunks]


def _md_chunk(units: list[tuple[str, int, int, str]]) -> tuple[int, int, str]:
    return (units[0][1], units[-1][2], "\n\n".join(u[3] for u in units))


def _tail(text: str, limit: int, need: int = 0) -> str:
    """Slutet på ``text`` om högst ``limit`` tecken.

    Radjusterat i första hand (helst från en icke-indragen rad, dvs. fältets
    huvud); räcker det inte till ``need`` tecken tas ett ordjusterat slut.
    """
    lines = text.split("\n")
    picked: list[str] = []
    head_start: int | None = None  # längsta svans som börjar på en icke-indragen rad
    for ln in reversed(lines):
        if len("\n".join([ln, *picked])) > limit:
            break
        picked.insert(0, ln)
        if ln.strip() and not ln.startswith(" "):
            head_start = len(picked)
    if head_start is not None and len("\n".join(picked[len(picked) - head_start:])) >= need:
        picked = picked[len(picked) - head_start:]
    tail = "\n".join(picked).strip("\n")
    if tail and len(tail) >= need:
        return tail
    cut = text[-limit:]
    space = next((i for i, ch in enumerate(cut) if ch.isspace()), -1)
    return (cut[space + 1:] if 0 <= space < len(cut) - 1 and len(cut) < len(text) else cut).strip()


def _head(text: str, limit: int, need: int = 0) -> str:
    """Början på ``text`` om högst ``limit`` tecken, rad- eller ordjusterad (spegling av ``_tail``)."""
    lines = text.split("\n")
    picked: list[str] = []
    for ln in lines:
        if len("\n".join([*picked, ln])) > limit:
            break
        picked.append(ln)
    head = "\n".join(picked).strip("\n")
    if head and len(head) >= need:
        return head
    cut = text[:limit]
    space = max((i for i, ch in enumerate(cut) if ch.isspace()), default=-1)
    return (cut[:space] if space > 0 and len(cut) < len(text) else cut).strip()


def _borrow_tail(text: str, limit: int, need: int) -> str:
    """Slutet av en chunk att låna som överlapp åt en kort efterföljare.

    Slutar chunken på en tabell får de lånade raderna tabellens rubrik- och
    avdelarrad före sig (annars saknar de sina kolumnnamn). Ryms inte rubriken
    plus rader lånas i stället slutet av närmaste icke-tabellenhet.
    """
    blocks = text.split("\n\n")
    last = blocks[-1].split("\n")
    if not _is_table(last):
        return _tail(text, limit, need)
    prefix = last[0] + "\n" + last[1]
    room = limit - len(prefix) - 1
    if len(last) > 2 and room > 0:
        rows = _tail("\n".join(last[2:]), room, 0)
        if rows and len(rows) <= room and all(ln.startswith("|") for ln in rows.split("\n")):
            with_header = f"{prefix}\n{rows}"
            if len(with_header) >= need:
                return with_header
    others = "\n\n".join(b for b in blocks[:-1] if not _is_table(b.split("\n")))
    if others:
        tail = _tail(others, limit, need)
        if len(tail) >= need:
            return tail
    # rader utan rubrik är bättre än att enheten faller ur indexet
    return _tail(text, limit, need)


def _rescue_short(
    chunks: list[tuple[int, int, str]], budget: int, overlap: int
) -> list[tuple[int, int, str]]:
    """Kort chunk (under ``MIN_CHUNK_CHARS``, utan sidhuvud) skulle förkastas av ``is_useful``
    och dess text finns inte i någon annan chunk. Lägg den sist i föregående chunk om det
    ryms, annars först i nästa; annars får den föregående chunks slut som överlapp
    (sidans första chunk lånar i stället början av nästa)."""
    items = list(chunks)
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(items):
        s, e, c = items[i]
        if len(c) < MIN_CHUNK_CHARS:
            if out and len(out[-1][2]) + 2 + len(c) <= budget:
                ps, _, pc = out[-1]
                out[-1] = (ps, e, f"{pc}\n\n{c}")
                i += 1
                continue
            if i + 1 < len(items) and len(c) + 2 + len(items[i + 1][2]) <= budget:
                _, ne, nc = items[i + 1]
                items[i + 1] = (s, ne, f"{c}\n\n{nc}")
                i += 1
                continue
            limit = min(overlap, budget - len(c) - 2)
            need = MIN_CHUNK_CHARS - len(c) - 2
            if out:
                tail = _borrow_tail(out[-1][2], limit, need)
                if tail:
                    c = f"{tail}\n\n{c}"
            elif i + 1 < len(items):
                # första chunken saknar föregångare: låna början av nästa i stället
                head = _head(items[i + 1][2], limit, need)
                if head:
                    c = f"{c}\n\n{head}"
        out.append((s, e, c))
        i += 1
    return out


def parse_reindex_since(value: str) -> float:
    """Tolka ``--reindex-since`` som ISO 8601 eller unix-sekunder.

    Returnerar unix-timestamp (float). Höjer ValueError vid ogiltigt format.
    """
    try:
        return float(value)
    except ValueError:
        pass
    # ISO 8601 — acceptera både med och utan tidszon. Naiva tider tolkas som lokal tid.
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def should_reingest(
    stored_mtime: float,
    disk_mtime: float,
    reindex_since: float | None,
) -> bool:
    """Avgör om en redan indexerad fil ska re-indexeras.

    - ``stored_mtime`` är mtime som lagrats vid indexering (0.0 = legacy-rad
      från innan mtime-tracking infördes; vi vet inte när den indexerades).
    - ``disk_mtime`` är aktuell mtime på .txt-filen.
    - ``reindex_since`` (om satt) tvingar re-index för filer modifierade efter
      denna tidpunkt — används för att täcka legacy-rader vid en känd re-OCR-våg.
    """
    if stored_mtime == 0:
        return reindex_since is not None and disk_mtime > reindex_since
    return disk_mtime > stored_mtime


def find_orphans(stored_sources: set[str], disk_filenames: set[str]) -> list[str]:
    """Källor som finns i tabellen men inte på disk längre (sorterat)."""
    return sorted(stored_sources - disk_filenames)


def get_table_sources(table) -> set[str]:
    """Hämta unika source-värden utan att läsa embedding-vektorerna."""
    rows = (
        table.search()
        .select(["source"])
        .limit(table.count_rows())
        .to_list()
    )
    return {row["source"] for row in rows}


def classify_reingest(
    *,
    filename: str,
    disk_mtime: float,
    already: dict[str, float],
    table_sources: set[str],
    reindex_since: float | None,
) -> bool:
    """Sann om filens gamla chunks måste ersättas före indexering."""
    return classify_index_action(
        filename=filename,
        disk_mtime=disk_mtime,
        already=already,
        table_sources=table_sources,
        reindex_since=reindex_since,
    ) == "reingest"


def classify_index_action(
    *,
    filename: str,
    disk_mtime: float,
    already: dict[str, float],
    table_sources: set[str],
    reindex_since: float | None,
    unusable_sources: set[str] | None = None,
) -> str:
    """Returnera ``new``, ``reingest`` eller ``skip`` för en textfil."""
    unusable_sources = unusable_sources or set()
    if filename not in table_sources:
        if (
            filename in unusable_sources
            and filename in already
            and not should_reingest(already[filename], disk_mtime, reindex_since)
        ):
            return "skip"
        return "new"
    if filename not in already:
        return "reingest"
    if should_reingest(already[filename], disk_mtime, reindex_since):
        return "reingest"
    return "skip"


_SAFE_SOURCE_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")


def _source_predicate(source: str) -> str:
    """Returnerar ett LanceDB-delete-predikat för en specifik source.

    Kastar ValueError om filnamnet innehåller kontrolltecken eller null-bytes
    som kan orsaka oväntad SQL-tolkning.
    """
    if not _SAFE_SOURCE_RE.match(source):
        raise ValueError(f"Ogiltigt source-filnamn (kontrolltecken): {source!r}")
    safe = source.replace("'", "''")
    return f"source = '{safe}'"


def delete_source_for_reingest(table, source: str, *, is_reingest: bool) -> None:
    """Radera gamla chunks när en source ska ersättas."""
    if is_reingest:
        table.delete(_source_predicate(source))


_MD_SYNTAX_RE = re.compile(r"[*#>|\-`\s]")


def is_useful(chunk: str, markdown: bool = False) -> bool:
    """Sann om chunken är tillräckligt lång och textrik för att indexeras.

    För Markdown-chunks bedöms innehållet utan sidhuvudraden (``> Sidhuvud: …``),
    och andelen alfanumeriska tecken räknas efter att
    Markdown-syntax (``*#>|-``, backtick) och blanksteg tagits bort — annars
    förkastas glesa tabeller och blankettfält. Textvägen är oförändrad.
    """
    if markdown and chunk.startswith(_SIDHUVUD):
        # sidhuvudet är kontext — chunken ska bära sig på sitt eget innehåll
        chunk = chunk.partition("\n")[2]
    if len(chunk) < MIN_CHUNK_CHARS:
        return False
    text = _MD_SYNTAX_RE.sub("", chunk) if markdown else chunk
    if not text:
        return False
    alnum = sum(c.isalnum() for c in text)
    return alnum / len(text) >= MIN_ALNUM_RATIO


def _table_exists(db: lancedb.LanceDBConnection, name: str) -> bool:
    # lancedb ≥0.20 returnerar ListTablesResponse (Pydantic), inte en ren lista.
    result = db.list_tables()
    names = result.tables if hasattr(result, "tables") else list(result)
    return name in names


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def _chunk_schema() -> pa.Schema:
    """Schemat för chunk-tabellen: vektor, text, källa, sida, chunk-index, mtime och namnfält."""
    return pa.schema([
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("page", pa.int32()),
        pa.field("chunk_idx", pa.int32()),
        pa.field("mtime", pa.float64()),
        *[pa.field(f, pa.string()) for f in NAME_FIELDS],
    ])


def _oppna_tabell(db, rebuild: bool, ctx: Any):
    """Öppna chunk-tabellen eller skapa den; ``rebuild`` tar bort den först.

    En äldre tabell utan mtime-kolumn migreras genom att kolumnen läggs till.
    """
    if rebuild and _table_exists(db, TABLE):
        db.drop_table(TABLE)
    if _table_exists(db, TABLE):
        table = db.open_table(TABLE)
        if "mtime" not in table.schema.names:
            ctx.log("Migrerar tabell: lägger till mtime-kolumn (default 0.0).")
            table.add_columns({"mtime": "cast(0.0 as double)"})
        return table
    return db.create_table(TABLE, schema=_chunk_schema())


def _lasa_indexerade_mtimes(state_conn) -> dict[str, float]:
    """Hämta senast indexerad text_mtime per textfil från state.db."""
    return {
        row["pdf_stem"] + ".txt": row["text_mtime"]
        for row in state_db.list_ingested_mtimes(state_conn)
    }


def _oanvandbara_mtimes_fil(unusable_list: Path) -> Path:
    """Sökväg till filen med mtime för oanvändbara textfiler, bredvid unusable-listan."""
    return Path(unusable_list).with_name("unusable_mtimes.json")


def _lasa_oanvandbara(mtimes_file: Path, already: dict[str, float], ctx: Any) -> set[str]:
    """Läs oanvändbara filers mtime in i ``already`` och returnera deras namn.

    Ett fel vid läsningen loggas som varning; filerna hanteras då som vanligt.
    """
    unusable_sources: set[str] = set()
    if mtimes_file.exists():
        try:
            for fname, mtime in json.loads(
                mtimes_file.read_text(encoding="utf-8")
            ).items():
                m = float(mtime)
                unusable_sources.add(fname)
                if fname not in already or m > already[fname]:
                    already[fname] = m
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            ctx.log(
                f"Ignorerar {mtimes_file.name}: kunde inte läsas, "
                f"oanvändbara filer hanteras som vanligt ({type(exc).__name__}: {exc})",
                level="warning",
            )
    return unusable_sources


def _rensa_foraldralosa(
    table, files: list[Path], already: dict[str, float], table_sources: set[str], state_conn, ctx: Any
) -> None:
    """Ta bort poster för textfiler som inte längre finns på disk, i tabell och state.db."""
    disk_names = {f.name for f in files}
    orphans = find_orphans(set(already) | table_sources, disk_names)
    if not orphans:
        return
    ctx.log(f"Rensar {len(orphans)} föräldralösa poster (text/-fil borta):")
    for s in orphans:
        table.delete(_source_predicate(s))
        already.pop(s, None)
        table_sources.discard(s)
        stem = s[:-4] if s.endswith(".txt") else s
        state_db.delete_ingest(state_conn, stem)
        ctx.log(f"  - {s}")
    state_conn.commit()


def _planera_indexering(
    files: list[Path],
    already: dict[str, float],
    table_sources: set[str],
    reindex_since: float | None,
    unusable_sources: set[str],
    ctx: Any,
) -> tuple[list[tuple[Path, float, bool]], int]:
    """Klassificera varje fil som ny, re-indexerad eller överhoppad och logga planen.

    Returnerar ``(path, disk_mtime, is_reingest)`` för filer som ska indexeras
    samt antalet överhoppade filer.
    """
    todo: list[tuple[Path, float, bool]] = []  # (path, disk_mtime, is_reingest)
    skipped = 0
    for f in files:
        disk_mtime = f.stat().st_mtime
        action = classify_index_action(
            filename=f.name,
            disk_mtime=disk_mtime,
            already=already,
            table_sources=table_sources,
            reindex_since=reindex_since,
            unusable_sources=unusable_sources,
        )
        if action == "new":
            todo.append((f, disk_mtime, False))
        elif action == "reingest":
            stored = already.get(f.name)
            ctx.log(
                f"[delta] {f.name}: re-indexeras (lagrad mtime {stored!r}, "
                f"på disk {disk_mtime:.3f}, reindex-since {reindex_since!r})",
                level="debug",
            )
            todo.append((f, disk_mtime, True))
        else:
            skipped += 1

    new_count = sum(1 for _, _, r in todo if not r)
    reindex_count = sum(1 for _, _, r in todo if r)
    ctx.log(
        f"Indexerar {len(todo)} av {len(files)} filer "
        f"(nya: {new_count}, re-index: {reindex_count}, skippar: {skipped})."
    )
    ctx.log(
        f"[delta] {skipped} filer hoppas över: oförändrad text_mtime i ingest-tabellen",
        level="debug",
    )
    return todo, skipped


def _chunka_fil(
    raw: str,
    filnamn: str,
    meta: dict,
    disk_mtime: float,
    chunk_chars: int,
    chunk_overlap: int,
    page_markdown: dict[int, tuple[str, str]] | None = None,
    context: Any = None,
) -> list[dict[str, Any]]:
    """Dela texten i sidvisa chunks och returnera en tabellrad per användbar chunk.

    ``page_markdown`` är ``{sidnr: (pdf_pages.text, markdown)}`` för layoutsidor.
    En sidas Markdown används bara när den är färsk, dvs. när
    ``normalize(pdf_pages.text)`` är exakt lika med sidan i textfilen (rå, före
    ``chunk_text``s blankstegskollaps). Annars har texten ändrats efter OCR
    (LLM-korrigering, merge_wpu, manuell redigering) och sidan chunkas som ren
    text precis som tidigare.
    """
    page_markdown = page_markdown or {}
    rows: list[dict[str, Any]] = []
    chunk_idx = 0
    from_md = stale = 0
    stale_pages: list[int] = []
    for page_idx, page in enumerate(split_pages(raw), start=1):
        chunks = None
        markdown = False
        if page_idx in page_markdown:
            db_text, md = page_markdown[page_idx]
            if not (md or "").strip():
                pass  # tom Markdown (t.ex. blank sida) — ren text som i dag
            elif markdown_is_fresh(db_text, page):
                chunks = chunk_markdown(normalize_markdown(md), chunk_chars, chunk_overlap)
                markdown = True
                from_md += 1
            else:
                stale += 1
                stale_pages.append(page_idx)
        if chunks is None:
            chunks = chunk_text(page, chunk_chars, chunk_overlap)
        for _, _, chunk in chunks:
            if not is_useful(chunk, markdown=markdown):
                continue
            rows.append({
                "text": chunk,
                "source": filnamn,
                "page": page_idx,
                "chunk_idx": chunk_idx,
                "mtime": disk_mtime,
                **meta,
            })
            chunk_idx += 1
    if stale_pages and context is not None:
        context.log(
            f"[markdown] {filnamn}: Markdown förkastad på {len(stale_pages)} sidor "
            f"(texten ändrad efter OCR), första: {stale_pages[:10]}",
            level="debug",
        )
    if page_markdown and context is not None:
        context.log(
            f"[markdown] {filnamn}: {from_md} sidor chunkade från Markdown, "
            f"{stale} med inaktuell Markdown (ren text i stället)"
        )
    return rows


def _embedda_chunks(model, rows: list[dict[str, Any]]):
    """Bädda in chunkarna med e5-prefixet och kontrollera att varje vektor är giltig.

    Kastar ``ValueError`` vid NaN, Inf eller en all-nollvektor.
    """
    # e5 vill ha "passage: " på dokument
    embeddings = model.encode(
        [f"passage: {r['text']}" for r in rows],
        batch_size=32,
        show_progress_bar=False,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    for idx, v in enumerate(embeddings):
        if not all(math.isfinite(x) for x in v):
            raise ValueError(f"chunk {idx}: embedding innehåller NaN/Inf")
        if not any(v):
            raise ValueError(f"chunk {idx}: embedding är all-nollor")
    return embeddings


def _bygg_fts_index(table, ctx: Any) -> None:
    """Bygg eller uppdatera FTS-indexet (BM25). Kräver lancedb med tantivy/native FTS."""
    try:
        table.create_fts_index("text", replace=True)
        ctx.log("✓ FTS-index uppdaterat (BM25 på 'text') — hybridsök tillgängligt.")
    except Exception as e:  # noqa: BLE001
        ctx.log(
            f"⚠ FTS-index kunde inte skapas ({e}).\n"
            f"  Hybridsök (--hybrid) fungerar inte förrän det fixas.\n"
            f"  Prova: pip install --upgrade lancedb",
            level="error",
        )


def _skriv_oanvandbara(
    unusable: list[str],
    unusable_mtimes: dict[str, float],
    unusable_path: Path,
    mtimes_file: Path,
    ctx: Any,
) -> None:
    """Skriv listan över filer utan användbara chunks och deras mtime för nästa körning."""
    unusable_path.write_text("\n".join(unusable) + "\n", encoding="utf-8")
    # Spara mtime per oanvändbar fil så nästa körning kan hoppa över dem.
    existing_um: dict[str, float] = {}
    if mtimes_file.exists():
        with contextlib.suppress(Exception):
            existing_um = {
                k: float(v)
                for k, v in json.loads(
                    mtimes_file.read_text(encoding="utf-8")
                ).items()
            }
    existing_um.update(unusable_mtimes)
    mtimes_file.write_text(
        json.dumps(existing_um, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    ctx.log(
        f"\n{len(unusable)} filer producerade noll användbara chunks — "
        f"skrivna till {unusable_path}.\n"
        f"Kör om OCR med:  .venv/bin/python scripts/ocr.py --redo --mode files "
        f"--from-list {unusable_path}",
        level="warning",
    )


def _las_textfil(f: Path, i: int, antal: int, ctx: Any) -> str | None:
    """Läs textfilen; vid läsfel loggas en SKIP-rad och ``None`` returneras."""
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        ctx.log(f"  [{i}/{antal}] SKIP {f.name}: {e}", level="error")
        return None


def _markera_oanvand_fil(
    table, state_conn, f: Path, disk_mtime: float, is_reingest: bool, i: int, antal: int, ctx: Any
) -> None:
    """Ta bort gamla chunks för en fil utan användbar text och spara den i state.db med 0 chunks."""
    ctx.log(
        f"  [{i}/{antal}] {f.name}: inga användbara chunks",
        level="warning",
    )
    delete_source_for_reingest(table, f.name, is_reingest=is_reingest)
    state_db.record_ingest(
        state_conn, pdf_stem=f.stem,
        text_mtime=disk_mtime, chunks=0,
    )


def _lagra_chunks(
    table,
    state_conn,
    model,
    rows: list[dict[str, Any]],
    f: Path,
    disk_mtime: float,
    is_reingest: bool,
    i: int,
    antal: int,
    ctx: Any,
) -> bool:
    """Bädda in och skriv chunkarna till tabellen samt spegla filen i state.db.

    Returnerar ``False`` om inbäddningen misslyckades; filen hoppas då över.
    """
    try:
        embeddings = _embedda_chunks(model, rows)
    except Exception as e:  # noqa: BLE001
        ctx.log(
            f"  [{i}/{antal}] SKIP {f.name}: embedding-fel — {e}",
            level="error",
        )
        log_error("ingest.embed", f.name, str(e))
        return False

    for r, v in zip(rows, embeddings, strict=False):
        r["vector"] = v.tolist()

    delete_source_for_reingest(table, f.name, is_reingest=is_reingest)
    table.add(rows)

    # Spegla i state.db för delta-frågor.
    try:
        state_db.record_ingest(
            state_conn, pdf_stem=f.stem,
            text_mtime=disk_mtime, chunks=len(rows),
        )
    except Exception as e:  # noqa: BLE001
        ctx.log(
            f"  [{i}/{antal}] varning: state_db.record_ingest failed: {e}",
            level="warning",
        )
    return True


def _logga_framsteg(
    f: Path,
    i: int,
    antal: int,
    antal_chunks: int,
    is_reingest: bool,
    total_chunks: int,
    t0: float,
    ctx: Any,
) -> None:
    """Logga en rad per indexerad fil med antal chunks och beräknad återstående tid."""
    elapsed = time.monotonic() - t0
    rate = i / elapsed if elapsed else 0
    eta = (antal - i) / rate if rate else 0
    tag = "↻" if is_reingest else "+"
    ctx.log(
        f"  [{i:>4}/{antal}] {tag} {f.name[:58]:58s} "
        f"+{antal_chunks:>3} chunks (totalt {total_chunks}, eta {int(eta // 60)}m{int(eta % 60):02d}s)"
    )


def _valj_filer(
    text_dir: Path,
    limit: int | None,
    table,
    already: dict[str, float],
    table_sources: set[str],
    state_conn,
    ctx: Any,
) -> list[Path]:
    """Lista textfilerna (sorterade), rensa föräldralösa poster och begränsa till ``limit``."""
    files = sorted(text_dir.glob("*.txt"))
    if not limit and (already or table_sources):
        _rensa_foraldralosa(table, files, already, table_sources, state_conn, ctx)
    if limit:
        files = files[:limit]
    return files


def run_ingest(
    *,
    rebuild: bool,
    limit: int | None,
    text_dir: Path,
    db_dir: Path,
    chunk_chars: int,
    chunk_overlap: int,
    model_name: str,
    unusable_list: Path,
    reindex_since: float | None,
    context=None,
) -> int:
    """Indexera text till LanceDB. Returnerar exitkod."""
    ctx = _ctx(context)

    # Fallback till standardmodellen när anroparen skickar ett tomt modellnamn
    # (t.ex. registry-defaults eller pipeline) — annars byggs ett tomt index.
    model_name = model_name or MODEL_NAME

    if not text_dir.exists():
        ctx.log(f"Saknar {text_dir}/ — kör ocr först.", level="error")
        return 1

    db_dir.mkdir(exist_ok=True)
    db = lancedb.connect(str(db_dir))
    table = _oppna_tabell(db, rebuild, ctx)

    state_conn = state_db.connect()
    state_db.init_schema(state_conn)
    already = _lasa_indexerade_mtimes(state_conn)
    table_sources = get_table_sources(table)
    unusable_mtimes_file = _oanvandbara_mtimes_fil(unusable_list)
    unusable_sources = _lasa_oanvandbara(unusable_mtimes_file, already, ctx)

    ctx.log(f"Laddar embedding-modell {model_name} (första gången tar några minuter)…")
    model = SentenceTransformer(model_name)

    files = _valj_filer(text_dir, limit, table, already, table_sources, state_conn, ctx)

    todo, _skipped = _planera_indexering(
        files, already, table_sources, reindex_since, unusable_sources, ctx
    )

    t0 = time.monotonic()
    total_chunks = 0
    unusable: list[str] = []
    unusable_mtimes: dict[str, float] = {}

    for i, (f, disk_mtime, is_reingest) in enumerate(todo, 1):
        ctx.check_cancelled()
        ctx.progress(i, len(todo), f.name)
        meta = parse_filename(f.stem)
        raw = _las_textfil(f, i, len(todo), ctx)
        if raw is None:
            continue

        page_markdown = state_db.get_page_markdown_map(state_conn, f.stem)
        rows = _chunka_fil(
            raw, f.name, meta, disk_mtime, chunk_chars, chunk_overlap,
            page_markdown=page_markdown, context=ctx,
        )
        if not rows:
            _markera_oanvand_fil(table, state_conn, f, disk_mtime, is_reingest, i, len(todo), ctx)
            unusable.append(f.name)
            unusable_mtimes[f.name] = disk_mtime
            continue

        if not _lagra_chunks(table, state_conn, model, rows, f, disk_mtime, is_reingest, i, len(todo), ctx):
            continue
        total_chunks += len(rows)
        _logga_framsteg(f, i, len(todo), len(rows), is_reingest, total_chunks, t0, ctx)

    _bygg_fts_index(table, ctx)

    ctx.log(f"\nKlart. Tabell '{TABLE}' har {table.count_rows()} chunks.")

    if unusable:
        _skriv_oanvandbara(
            unusable, unusable_mtimes, Path(unusable_list), unusable_mtimes_file, ctx
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="börja om från noll")
    ap.add_argument("--limit", type=int, help="max antal filer att indexera")
    ap.add_argument("--text-dir",
                    default=os.environ.get("TEXT_DIR", str(TEXT_DIR)),
                    help=f"katalog med .txt-filer (default: {TEXT_DIR})")
    ap.add_argument("--db-dir",
                    default=os.environ.get("DB_DIR", str(DB_DIR)),
                    help=f"LanceDB-katalog (default: {DB_DIR})")
    ap.add_argument("--chunk-chars", type=int,
                    default=int(os.environ.get("CHUNK_CHARS", str(CHUNK_CHARS))),
                    help=f"chunk-storlek i tecken (default: {CHUNK_CHARS})")
    ap.add_argument("--chunk-overlap", type=int,
                    default=int(os.environ.get("CHUNK_OVERLAP", str(CHUNK_OVERLAP))),
                    help=f"chunk-överlapp i tecken (default: {CHUNK_OVERLAP})")
    ap.add_argument("--model",
                    default=os.environ.get("EMBED_MODEL", MODEL_NAME),
                    help=f"embedding-modell (default: {MODEL_NAME})")
    ap.add_argument("--unusable-list",
                    default=os.environ.get("UNUSABLE_LIST",
                                           str(ROOT / "generated" / "unusable.txt")),
                    help="skriv filer som producerade noll användbara chunks "
                         "till denna fil (default: generated/unusable.txt)")
    ap.add_argument("--reindex-since",
                    help="tvinga re-index av filer modifierade efter denna tid "
                         "(ISO 8601 t.ex. '2026-05-01' eller unix-sekunder).")
    args = ap.parse_args()

    reindex_since: float | None = None
    if args.reindex_since:
        try:
            reindex_since = parse_reindex_since(args.reindex_since)
        except ValueError as e:
            print(f"Ogiltigt --reindex-since: {e}", file=sys.stderr)
            return 2

    return run_ingest(
        rebuild=args.rebuild,
        limit=args.limit,
        text_dir=Path(args.text_dir),
        db_dir=Path(args.db_dir),
        chunk_chars=args.chunk_chars,
        chunk_overlap=args.chunk_overlap,
        model_name=args.model,
        unusable_list=Path(args.unusable_list),
        reindex_since=reindex_since,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
