"""Citatuppslag: matcha [Nr X, sida Y]-citat mot PDF:er och rendera länkar.

Ren logik utan Streamlit-beroenden — webui.py wrappar funktionerna med
st.cache_resource och tillhandahåller den cachade nr→PDF-mappingen.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path

# Nr kan vara digitalt med valfritt antal led av "." eller "," (t.ex. 281,10 eller 1322.7).
# Valfria hakparenteser runt citatet konsumeras av regexen så hela [Nr X, sida Y]
# ersätts av <a>-taggen — undviker att Markdown-parsern manglar [<a>...</a>].
CITE_RE = re.compile(r"\[?Nr ([\w\-]+(?:[.,][\w\-]+)*),\s*sida (\d+)\]?")

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
        if key.startswith(nr) and not key[len(nr):len(nr) + 1].isalnum():
            seen.setdefault(str(path), path)
    return sorted(seen.values(), key=lambda p: p.stem)


def extract_cited_sources(answer: str, mapping: dict[str, Path]) -> list[dict]:
    """Bygg källlista ur ett MCP-svar genom att parsa unika Nr-citat."""
    seen: dict[str, dict] = {}
    for m in CITE_RE.finditer(answer):
        nr = m.group(1)
        for pdf in resolve_nr_all(nr, mapping):
            stem = pdf.stem
            if stem in seen:
                continue
            parts = [p.strip() for p in stem.split(" — ")]
            seen[stem] = {
                "source": stem + ".txt",
                "page": None,
                "nr": nr,
                "titel": parts[1] if len(parts) > 1 else stem,
            }
    return list(seen.values())


def linkify_citations(
    text: str,
    mapping: dict[str, Path],
    known_sources: set[str] | None = None,
) -> str:
    """Förvandla "Nr X, sida Y" till små inline-knappar som öppnar PDF lokalt
    via ?pdf=<base64>-handlern högst upp i webui-scriptet.
    Fungerar i både RAG- och MCP-läge — slår upp nr → PDF via ``mapping``.

    ``known_sources`` (källfilnamn som faktiskt hämtades för svaret, finns bara
    i RAG-läge) används för att särskilja när ett nr-prefix matchar flera filer.
    Återstår fler än en kandidat renderas en länk per fil så användaren kan välja.
    """

    def _anchor(pdf: Path, label: str, title: str) -> str:
        token = base64.urlsafe_b64encode(str(pdf).encode()).decode().rstrip("=")
        return (
            f'<a href="?pdf={token}" target="pdf_opener" '
            f'style="{_LINK_STYLE}" title="{html.escape(title, quote=True)}">{label}</a>'
        )

    def repl(m: re.Match) -> str:
        nr = m.group(1)
        cands = resolve_nr_all(nr, mapping)
        # Smalna av tvetydiga träffar till de källor som faktiskt hämtades.
        if known_sources and len(cands) > 1:
            narrowed = [p for p in cands if f"{p.stem}.txt" in known_sources]
            if narrowed:
                cands = narrowed
        if not cands:
            return m.group(0)
        if len(cands) == 1:
            return _anchor(cands[0], m.group(0), "Öppna PDF")
        # Genuint tvetydigt nr (flera dokument delar prefix): en länk per fil,
        # märkt med den särskiljande titeldelen ur stammen.
        links = []
        for pdf in cands:
            suffix = pdf.stem[len(nr):].lstrip("_- ") or pdf.stem
            links.append(_anchor(pdf, html.escape(suffix[:30]), pdf.stem))
        return f"{m.group(0)} ({' '.join(links)})"

    return CITE_RE.sub(repl, text)
