"""Citatuppslag: matcha [Nr X, sida Y]-citat mot PDF:er och rendera länkar.

Ren logik utan Streamlit-beroenden — Utredning.py wrappar funktionerna med
st.cache_resource och tillhandahåller den cachade nr→PDF-mappingen.
"""

from __future__ import annotations

import base64
import html
import json
import re
from pathlib import Path

import web_log

log = web_log.get_logger("citations")

# Nr kan vara digitalt med valfritt antal led av "." eller "," (t.ex. 281,10
# eller 1322.7), men WPU-referenser skrivs ibland som dokumentprefix utan
# "Nr" (t.ex. Pol-1996-12-19). Valfria hakparenteser runt en träff
# konsumeras av regexen så Markdown-parsern inte manglar [<a>...</a>].
# Modellen avkortar ibland långa WPU-id med "…" eller "..." ("Pol-1986-03-02_
# E15-01…"); avkortningen fångas i ``trunc``/``rtrunc`` och id:t före den löses
# som prefix. Tillbakasyftningar ("[samma dokument, sida 2]", "[samma sida]")
# fångas i ``back`` och ärver föregående entydigt lösta hänvisning.
_TRUNC = r"(?:…|\.\.\.)"
CITE_RE = re.compile(
    # [Nr X, sida Y] — flera sidnummer i samma citat ("sida 103, 112")
    # hör också till träffen så inget sidnummer lämnas utanför länken.
    r"\[?(?:Nr\s+)?(?P<nr>[\w\-]+(?:[.,_][\w\-]+)*)(?P<trunc>" + _TRUNC + r")?"
    r",\s*sida\s*(?P<page>\d+(?:\s*,\s*\d+)*)\]?"
    # [Nr X] och [Nr X, "titel"] — referens till hela filen, utan sida.
    # Kräver hakparentes så löst tal i prosan inte blir en länk.
    r"|\[Nr\s+(?P<ref>[\w\-]+(?:[.,_][\w\-]+)*)(?P<rtrunc>" + _TRUNC + r")?"
    r"(?:,[^\[\]]*)?\]"
    # [samma dokument, sida Y], [samma källa], [samma sida]
    r"|\[(?i:samma)\s+(?P<back>dokument|källa|sida)"
    r"(?:,\s*sida\s*(?P<bpage>\d+(?:\s*,\s*\d+)*))?\]"
)

# Sökträffarnas source-rad i search_archive (format_context(include_source=True)).
_SOURCE_LINE_RE = re.compile(r"^source: (\".*\")\s*$", re.MULTILINE)


def _cite_nr(m: re.Match) -> str:
    """Citerat Nr (sida-lösa träffar hamnar i gruppen ``ref``)."""
    return str(m.group("nr") or m.group("ref"))


def _cite_truncated(m: re.Match) -> bool:
    """Sant när modellen avkortat dokument-id:t med "…" eller "..."."""
    return bool(m.group("trunc") or m.group("rtrunc"))


def _cite_page(m: re.Match) -> int | None:
    """Första sidnumret i citatet, eller None för en sida-lös referens.

    Citat kan lista flera sidor ("sida 103, 112"); länken öppnar den första."""
    raw = m.group("page") or m.group("bpage")
    return int(raw.split(",")[0]) if raw else None


def sources_from_tool_output(text: str) -> set[str]:
    """Källfilnamnen (``source``-raderna) i ett search_archive-svar.

    Utredningsläget har inga hämtade träffar i sidans process; källorna i
    verktygssvaren används i stället för att särskilja avkortade id."""
    sources: set[str] = set()
    for raw in _SOURCE_LINE_RE.findall(text):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value:
            sources.add(value)
    return sources


def resolve_truncated(
    prefix: str, mapping: dict[str, Path], known_sources: set[str] | None = None
) -> Path | None:
    """Lös ett avkortat id ("Pol-…-E15-A…") till en PDF, eller None.

    Avkortningen kan ske mitt i ett ord, så alla stammar som börjar med
    prefixet är kandidater. Bara ett entydigt prefix (efter avsmalning mot
    hämtade källor) ger en PDF — ett tvetydigt får aldrig gissas fram."""
    seen: dict[str, Path] = {}
    for key, path in mapping.items():
        if key.startswith(prefix):
            seen.setdefault(str(path), path)
    cands = sorted(seen.values(), key=lambda p: p.stem)
    if known_sources and len(cands) > 1:
        narrowed = [p for p in cands if f"{p.stem}.txt" in known_sources]
        if narrowed:
            cands = narrowed
    if len(cands) == 1:
        return cands[0]
    if cands:
        log.debug(
            "avkortat id %r är tvetydigt (%d kandidater, t.ex. %s) — lämnas som text",
            prefix, len(cands), ", ".join(p.stem for p in cands[:3]),
        )
    else:
        log.debug("avkortat id %r matchar ingen PDF — lämnas som text", prefix)
    return None


# generated/ocr/ skriver över downloaded/-katalogerna → föredras (har OCR-textlager)
PDF_DIRS = ("downloaded/files", "downloaded/wpu_files", "generated/ocr")

_LINK_STYLE = (
    "display:inline-block;padding:1px 6px;margin:0 2px;"
    "border:1px solid rgba(128,128,128,0.4);border-radius:6px;"
    "font-size:0.82em;background:rgba(128,128,128,0.12);"
    "color:inherit;text-decoration:none;"
    "font-family:ui-monospace,SFMono-Regular,monospace;"
)


def build_nr_to_pdf(root: Path) -> dict[str, Path]:
    """Bygg nr → PDF-sökväg från filsystemet (ocr/ föredras framför files/)."""
    mapping: dict[str, Path] = {}
    for d in PDF_DIRS:
        folder = root / d
        if not folder.is_dir():
            continue
        for pdf in folder.glob("*.pdf"):
            nr = pdf.stem.split(" — ")[0].strip()
            if nr:
                mapping[nr] = pdf
                # WPU-filer kan ha "_-_"-separerade suffix (t.ex. "_-_total_version_nr_0").
                # Registrera prefixet som alternativ nyckel för att matcha äldre index-poster.
                if "_-_" in nr:
                    prefix = nr.split("_-_")[0]
                    if prefix and prefix not in mapping:
                        mapping[prefix] = pdf
    return mapping


def resolve_nr_all(nr: str, mapping: dict[str, Path]) -> list[Path]:
    """Returnera alla PDF-kandidater för ett citerat Nr.

    Modellen förkortar ibland långa WPU-nr genom att klippa bort det
    titel-liknande suffixet (t.ex. citerar "Pol-...-04-C" fast stammen är
    "Pol-...-04-C_Förhör-Jan-Stocklassa"). Vid avsaknad av exakt träff faller
    vi tillbaka på prefix-matchning. Två olika dokument kan dela samma
    dokument-ID-prefix (t.ex. "...EA9982-00-B" finns i två filer) — då
    returneras båda och callern får särskilja eller länka till var och en.
    """
    if nr in mapping:
        return [mapping[nr]]
    # Dedup på sökväg men behåll stabil ordning för deterministisk rendering.
    seen: dict[str, Path] = {}
    for key, path in mapping.items():
        # WPU-stammar med ett Pol-dokument-ID kan ha titeln direkt efter ID:t
        # ("...HRdomenChrister..."), utan avskiljare. För sådana referenser
        # är ett Pol-prefix med understreck tillräckligt specifikt. Vanliga
        # nummer behåller spärren mot att "28" felmatchar "281".
        joined_wpu_title = nr.startswith("Pol-") and "_" in nr
        if key.startswith(nr) and (
            joined_wpu_title or not key[len(nr):len(nr) + 1].isalnum()
        ):
            seen.setdefault(str(path), path)
    return sorted(seen.values(), key=lambda p: p.stem)


def extract_cited_sources(answer: str, mapping: dict[str, Path]) -> list[dict]:
    """Bygg källlista ur ett MCP-svar genom att parsa unika Nr-citat."""
    seen: dict[str, dict] = {}
    for m in CITE_RE.finditer(answer):
        if m.group("back"):
            continue  # tillbakasyftning — dokumentet finns redan i listan
        nr = _cite_nr(m)
        page = _cite_page(m)
        if _cite_truncated(m):
            pdf = resolve_truncated(nr, mapping)
            pdfs = [pdf] if pdf else []
        else:
            pdfs = resolve_nr_all(nr, mapping)
        for pdf in pdfs:
            stem = pdf.stem
            if stem in seen:
                continue
            parts = [p.strip() for p in stem.split(" — ")]
            seen[stem] = {
                "source": stem + ".txt",
                "page": page,
                "nr": nr,
                "titel": parts[1] if len(parts) > 1 else stem,
            }
    return list(seen.values())


def pdf_anchor(
    pdf: Path, label: str, title: str = "Öppna PDF", page: int | None = None
) -> str:
    """HTML-länk till PDF-handlern som öppnar PDF:en i ny webbläsarflik.

    Handlern körs på sidan som renderar citatet och laddas i den gömda
    ``pdf_opener``-iframen. ``label`` ska redan vara HTML-säker.
    """
    token = base64.urlsafe_b64encode(str(pdf).encode()).decode().rstrip("=")
    page_param = f"&page={page}" if page and page > 0 else ""
    return (
        f'<a href="?pdf={token}{page_param}" target="pdf_opener" '
        f'style="{_LINK_STYLE}" title="{html.escape(title, quote=True)}">{label}</a>'
    )


def linkify_citations(
    text: str,
    mapping: dict[str, Path],
    known_sources: set[str] | None = None,
) -> str:
    """Förvandla "Nr X, sida Y" till små inline-knappar som öppnar PDF i
    ny webbläsarflik via ?pdf=<base64>&page=N-handlern på sidan som renderar
    svaret.
    Fungerar i både RAG- och MCP-läge — slår upp nr → PDF via ``mapping``.

    ``known_sources`` (källfilnamn som faktiskt hämtades för svaret, finns bara
    i RAG-läge) används för att särskilja när ett nr-prefix matchar flera filer.
    Återstår fler än en kandidat renderas en länk per fil så användaren kan välja.

    Ett avkortat id ("…") länkas bara när prefixet är entydigt, och
    "[samma dokument, sida Y]"/"[samma sida]" ärver närmast föregående
    hänvisning när den gav exakt en PDF — annars förblir de text.
    """
    # Senast entydigt lösta hänvisning (PDF, sida) för tillbakasyftningar.
    # Nollställs av en olöst eller tvetydig hänvisning, så "samma dokument"
    # aldrig hoppar över den och pekar på en äldre källa.
    prev: tuple[Path, int | None] | None = None

    def _anchor(pdf: Path, label: str, title: str, page: int | None) -> str:
        return pdf_anchor(pdf, label, title, page=page)

    def repl(m: re.Match) -> str:
        nonlocal prev
        label = html.escape(m.group(0), quote=False)
        if m.group("back"):
            if prev is None:
                log.debug("tillbakasyftning %r utan entydig föregående källa — text", m.group(0))
                return label
            pdf, prev_page = prev
            page = _cite_page(m)
            if page is None and m.group("back") == "sida":
                page = prev_page
            prev = (pdf, page if page is not None else prev_page)
            return _anchor(pdf, label, "Öppna PDF", page)
        nr = _cite_nr(m)
        page = _cite_page(m)
        if _cite_truncated(m):
            hit = resolve_truncated(nr, mapping, known_sources)
            prev = (hit, page) if hit else None
            return _anchor(hit, label, "Öppna PDF", page) if hit else label
        cands = resolve_nr_all(nr, mapping)
        # Smalna av tvetydiga träffar till de källor som faktiskt hämtades.
        if known_sources and len(cands) > 1:
            narrowed = [p for p in cands if f"{p.stem}.txt" in known_sources]
            if narrowed:
                cands = narrowed
        if not cands:
            prev = None
            return label
        if len(cands) == 1:
            prev = (cands[0], page)
            return _anchor(cands[0], label, "Öppna PDF", page)
        prev = None
        # Genuint tvetydigt nr (flera dokument delar prefix): en länk per fil,
        # märkt med den särskiljande titeldelen ur stammen.
        links = []
        for pdf in cands:
            suffix = pdf.stem[len(nr):].lstrip("_- ") or pdf.stem
            links.append(
                _anchor(pdf, html.escape(suffix[:30], quote=False), pdf.stem, page)
            )
        return f"{label} ({' '.join(links)})"

    parts: list[str] = []
    last = 0
    for m in CITE_RE.finditer(text):
        parts.append(html.escape(text[last:m.start()], quote=False))
        parts.append(repl(m))
        last = m.end()
    parts.append(html.escape(text[last:], quote=False))
    return "".join(parts)
