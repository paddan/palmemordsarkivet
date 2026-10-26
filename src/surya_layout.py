"""Ren layoutmodul: Suryas HTML-block -> typade block, ren text och Markdown.

Ingen Surya-import och ingen I/O. Workern (``surya_worker``) levererar råa block
``{"html", "label", "reading_order", "bbox", "confidence"}``; här tolkas de med
stdlib ``html.parser`` och en tillåtlista (tabeller, rubriker, listor, matte,
kod). Allt annat bidrar bara med sin text, och ingen markup sparas.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any

MAX_SPAN = 50
DEFAULT_HEADING_LEVEL = 2
HEADER_MAX_CHARS = 200
MASK_MARKER = "[MASKAD]"
# Suryas maskeringsmarkör i alla skrivsätt ("[REDACTED]", "[Redacted]", "[ redacted ]")
REDACTED_RE = re.compile(r"\[\s*redacted\s*\]", re.IGNORECASE)
CHECKBOX_OFF = "☐"
CHECKBOX_ON = "☒"


class BlockKind(StrEnum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    TABLE = "TABLE"
    FORMULA = "FORMULA"
    CODE = "CODE"
    CAPTION = "CAPTION"
    FOOTNOTE = "FOOTNOTE"
    PAGE_HEADER = "PAGE_HEADER"
    PAGE_FOOTER = "PAGE_FOOTER"
    FIGURE = "FIGURE"
    FORM = "FORM"


@dataclass(frozen=True)
class Cell:
    text: str
    header: bool
    row_span: int = 1
    col_span: int = 1


@dataclass(frozen=True)
class FormField:
    """Ett blankettfält. ``checked`` satt betyder en kryssrutealternativ-rad."""

    number: str | None
    label: str
    value: str
    checked: bool | None
    # etikettradens råa OCR-text när mallen skrivit om den (rättad etikett, ord som
    # flyttats till värde eller lös text); står bara i ren text, aldrig i Markdown
    source: str = ""


@dataclass(frozen=True)
class LayoutBlock:
    kind: BlockKind
    text: str
    order: int
    bbox: tuple[int, int, int, int] | None
    level: int | None = None
    rows: tuple[tuple[Cell, ...], ...] | None = None
    latex: str | None = None
    fields: tuple[FormField, ...] | None = None
    # en bbox per fält i ``fields`` (etikett- och värderaderna), så att en maskering
    # i ett fält inte gäller hela blankettens yta
    parts: tuple[tuple[int, int, int, int] | None, ...] | None = None


_LABEL_KIND: dict[str, BlockKind] = {
    "SectionHeader": BlockKind.HEADING,
    "Text": BlockKind.PARAGRAPH,
    "Bibliography": BlockKind.PARAGRAPH,
    "ListGroup": BlockKind.LIST_ITEM,
    "Table": BlockKind.TABLE,
    "TableOfContents": BlockKind.TABLE,
    "Form": BlockKind.FORM,
    "Equation": BlockKind.FORMULA,
    "Code": BlockKind.CODE,
    "Caption": BlockKind.CAPTION,
    "Footnote": BlockKind.FOOTNOTE,
    "PageHeader": BlockKind.PAGE_HEADER,
    "PageFooter": BlockKind.PAGE_FOOTER,
    "Picture": BlockKind.FIGURE,
    "Figure": BlockKind.FIGURE,
    "Diagram": BlockKind.FIGURE,
    "ChemicalBlock": BlockKind.FIGURE,
}


def _kind_of(label: str | None) -> BlockKind:
    """Etikettmappningen (okänd eller saknad etikett är ett stycke)."""
    return _LABEL_KIND.get(label or "", BlockKind.PARAGRAPH)


# --- HTML -> träd ---------------------------------------------------------

_VOID = {"br", "input", "img", "hr", "meta", "link", "wbr"}
_SKIP_CONTENT = {"script", "style", "head", "title"}
_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "tr", "table", "thead", "tbody", "tfoot", "caption",
    "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "br",
}
# tagg som stängs implicit: (nya taggen) -> (taggar som stängs, avgränsare att inte passera)
_AUTO_CLOSE: dict[str, tuple[set[str], set[str]]] = {
    "li": ({"li"}, {"ul", "ol"}),
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr", "td", "th"}, {"table"}),
    "p": ({"p"}, {"ul", "ol", "table", "td", "th", "li"}),
}


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[_Node | str] = []


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack: list[_Node] = [self.root]

    def _close_implicit(self, tag: str) -> None:
        rule = _AUTO_CLOSE.get(tag)
        if rule is None:
            return
        closes, barriers = rule
        for i in range(len(self.stack) - 1, 0, -1):
            name = self.stack[i].tag
            if name in barriers:
                return
            if name in closes:
                del self.stack[i:]
                return

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._close_implicit(tag)
        node = _Node(tag, {k: (v or "") for k, v in attrs})
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _parse_html(html: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def _walk(node: _Node) -> Iterator[_Node]:
    """Alla element i dokumentordning (inte noden själv)."""
    for child in node.children:
        if isinstance(child, _Node):
            yield child
            yield from _walk(child)


def _pieces(node: _Node, preserve: bool, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child if preserve else re.sub(r"\s+", " ", child))
            continue
        tag = child.tag
        if tag in _SKIP_CONTENT:
            continue
        if tag == "input":
            if child.attrs.get("type", "").lower() == "checkbox":
                out.append(CHECKBOX_ON if "checked" in child.attrs else CHECKBOX_OFF)
            continue
        if tag == "br":
            out.append("\n")
            continue
        block = tag in _BLOCK_TAGS
        if block:
            out.append("\n")
        _pieces(child, preserve or tag in ("pre", "code"), out)
        if block:
            out.append("\n")
        elif tag in ("td", "th"):
            out.append(" ")


def _text_of(node: _Node, preserve: bool = False) -> str:
    """Nodens text: ``<br>``/blockelement blir radbrytningar, inline-taggar tas bort."""
    out: list[str] = []
    _pieces(node, preserve, out)
    joined = "".join(out)
    if preserve:
        return joined.strip("\n").rstrip()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in joined.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _node_text(node: _Node | str) -> str:
    """Text för en enskild nod (en sträng, eller ett element inklusive kryssrutor)."""
    if isinstance(node, str):
        return re.sub(r"\s+", " ", node).strip()
    wrapper = _Node("#wrap")
    wrapper.children = [node]
    return _text_of(wrapper)


class _FlatText(HTMLParser):
    """Icke-rekursiv textutvinning, reserv när trädet är för djupt."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT:
            self._skip += 1
        elif tag == "input" and dict(attrs).get("type", "") == "checkbox":
            self.parts.append(CHECKBOX_ON if any(k == "checked" for k, _ in attrs) else CHECKBOX_OFF)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _flat_html_text(html: str) -> str:
    parser = _FlatText()
    parser.feed(html)
    parser.close()
    return _flat("".join(parser.parts))


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --- Block ----------------------------------------------------------------


def _span(value: str | None) -> int:
    try:
        return max(1, min(MAX_SPAN, int((value or "").strip())))
    except ValueError:
        return 1


def _table_rows(table: _Node) -> tuple[tuple[Cell, ...], ...]:
    """Tabellens egna rader (en nästlad tabells rader hör till den)."""
    rows: list[tuple[Cell, ...]] = []

    def visit(node: _Node) -> None:
        for child in node.children:
            if not isinstance(child, _Node):
                continue
            if child.tag == "table":
                continue
            if child.tag == "tr":
                cells = tuple(
                    Cell(
                        _flat(_text_of(c)),
                        c.tag == "th",
                        _span(c.attrs.get("rowspan")),
                        _span(c.attrs.get("colspan")),
                    )
                    for c in child.children
                    if isinstance(c, _Node) and c.tag in ("td", "th")
                )
                rows.append(cells)
            else:
                visit(child)

    visit(table)
    return tuple(rows)


def _rows_text(rows: tuple[tuple[Cell, ...], ...]) -> str:
    return "\n".join("\t".join(c.text for c in row) for row in rows)


def _table_stray(table: _Node) -> tuple[list[str], list[str]]:
    """Text i tabellen som inte ligger i en cell: (bildtexter, övrig lös text)."""
    captions: list[str] = []
    stray: list[str] = []

    def add(target: list[str], text: str) -> None:
        if text.strip():
            target.append(text.strip())

    def visit(node: _Node) -> None:
        for child in node.children:
            if isinstance(child, str) or child.tag in _SKIP_CONTENT:
                add(stray, _node_text(child) if isinstance(child, str) else "")
            elif child.tag == "caption":
                add(captions, _text_of(child))
            elif child.tag in ("thead", "tbody", "tfoot"):
                visit(child)
            elif child.tag == "tr":
                for c in child.children:
                    if isinstance(c, str) or c.tag not in ("td", "th"):
                        add(stray, _node_text(c))
            else:
                add(stray, _node_text(child))  # bl.a. cell utanför rad och nästlad tabell

    visit(table)
    return captions, stray


def _outside_table(body: _Node, table: _Node) -> tuple[list[str], list[str]]:
    """Text före och efter tabellen i dokumentordning, så inget går förlorat."""
    before: list[str] = []
    after: list[str] = []
    state = {"passed": False}

    def add(text: str) -> None:
        if text.strip():
            (after if state["passed"] else before).append(text.strip())

    def contains(node: _Node) -> bool:
        return any(n is table for n in _walk(node))

    def visit(parent: _Node) -> None:
        for child in parent.children:
            if child is table:
                state["passed"] = True
            elif isinstance(child, str):
                add(child)
            elif contains(child):
                visit(child)
            else:
                add(_node_text(child))

    visit(body)
    return before, after


def _parse_one(item: dict[str, Any]) -> tuple[list[LayoutBlock], bool]:
    html = item.get("html")
    html = html if isinstance(html, str) else ""
    label = item.get("label") if isinstance(item.get("label"), str) else None
    kind = _kind_of(label)
    bbox = _bbox(item.get("bbox"))
    body = _parse_html(html)

    def mk(k: BlockKind, text: str, **kw: Any) -> LayoutBlock:
        return LayoutBlock(k, text, 0, bbox, **kw)

    if kind is BlockKind.HEADING:
        level = DEFAULT_HEADING_LEVEL
        for el in _walk(body):
            if re.fullmatch(r"h[1-6]", el.tag):
                level = int(el.tag[1])
                break
        return [mk(kind, _text_of(body), level=level)], False

    if kind is BlockKind.LIST_ITEM:
        out: list[LayoutBlock] = []
        pending = _Node("#rest")
        found = False

        def flush() -> None:
            text = _text_of(pending)
            pending.children = []
            if text:
                out.append(mk(BlockKind.PARAGRAPH, text))

        def scan(node: _Node) -> None:
            nonlocal found
            for c in node.children:
                if isinstance(c, _Node) and c.tag == "li":
                    flush()
                    found = True
                    out.append(mk(kind, _text_of(c)))
                elif isinstance(c, _Node) and any(n.tag == "li" for n in _walk(c)):
                    scan(c)  # text före/efter punkterna behåller sin plats
                else:
                    pending.children.append(c)

        scan(body)
        if not found:
            return [mk(kind, _text_of(body))], False
        flush()
        return out, False

    if kind in (BlockKind.TABLE, BlockKind.FORM):
        table = next((e for e in _walk(body) if e.tag == "table"), None)
        rows = _table_rows(table) if table is not None else ()
        if table is None or not any(c.text for r in rows for c in r):
            if kind is BlockKind.FORM:
                return [mk(BlockKind.FORM, _text_of(body))], False
            return [mk(BlockKind.PARAGRAPH, _text_of(body))], label == "Table"
        before, after = _outside_table(body, table)
        captions, stray = _table_stray(table)
        out = [mk(BlockKind.PARAGRAPH, t) for t in before]
        out += [mk(BlockKind.CAPTION, t) for t in captions]
        out.append(mk(kind, _rows_text(rows), rows=rows))
        out += [mk(BlockKind.PARAGRAPH, t) for t in stray + after]
        return out, False

    if kind is BlockKind.FORMULA:
        maths = [_text_of(e).strip() for e in _walk(body) if e.tag == "math"]
        maths = [m for m in maths if m]
        if not maths:
            text = _text_of(body)
            return [mk(kind, text, latex=text)], False
        latex = "\n".join(maths)
        out = [mk(kind, latex, latex=latex)]
        rest = _Node("#rest")
        rest.children = _without_tag(body, "math")
        extra = _text_of(rest)
        if extra:
            out.append(mk(BlockKind.PARAGRAPH, extra))
        return out, False

    if kind is BlockKind.CODE:
        return [mk(kind, _text_of(body, preserve=True))], False

    return [mk(kind, _text_of(body))], False


def _without_tag(node: _Node, tag: str) -> list[_Node | str]:
    out: list[_Node | str] = []
    for c in node.children:
        if isinstance(c, _Node):
            if c.tag == tag:
                continue
            copy = _Node(c.tag, c.attrs)
            copy.children = _without_tag(c, tag)
            out.append(copy)
        else:
            out.append(c)
    return out


def _bbox(value: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (int(round(float(v))) for v in value)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1)


def _order_key(item: dict[str, Any]) -> tuple[bool, int]:
    ro = item.get("reading_order")
    if isinstance(ro, bool) or not isinstance(ro, (int, float)) or not math.isfinite(ro):
        return (True, 0)  # saknad eller icke-ändlig ordning sorteras sist
    return (False, int(ro))


def _has_text(block: LayoutBlock) -> bool:
    if block.fields:
        return True
    if block.rows is not None:
        return any(c.text.strip() for r in block.rows for c in r)
    return bool(block.text.strip())


def parse_blocks(raw: list[dict[str, Any]]) -> tuple[list[LayoutBlock], bool]:
    """Råa workerblock -> sorterade, omnumrerade block. Andra värdet är ``degraded``."""
    items = [r for r in raw if isinstance(r, dict)]
    items = sorted(items, key=_order_key)  # stabil: lika/saknad ordning behåller mottagen ordning
    blocks: list[LayoutBlock] = []
    degraded = False
    for item in items:
        try:
            parsed, bad = _parse_one(item)
        except RecursionError:
            # för djupt nästlad HTML: behåll texten som ett stycke
            html = item.get("html")
            text = _flat_html_text(html) if isinstance(html, str) else ""
            parsed, bad = [LayoutBlock(BlockKind.PARAGRAPH, text, 0, _bbox(item.get("bbox")))], True
        degraded = degraded or bad
        blocks += [b for b in parsed if _has_text(b)]
    return _renumber(blocks), degraded


def parse_page(
    raw: list[dict[str, Any]],
    image: Any = None,
    page_height: int | None = None,
    redactions: list[tuple[int, int]] | None = None,
    trace: dict[str, Any] | None = None,
) -> tuple[list[LayoutBlock], bool]:
    """``parse_blocks`` plus blankett- och förhörstolkning (``form_layout``).

    ``image`` är sidbilden (för kryssrutor som egentligen är maskeringar).
    ``page_height`` är sidbildens höjd i bildpunkter och ska alltid skickas in
    av anroparen: värdeavståndet för blankettfält är en andel av sidhöjden, och
    reserven (största bbox-y2) skalar med hur långt ned texten når, inte med sidan.
    ``redactions`` är pixeldetekterade maskeringar ``(y0, y1)``; de läggs in efter
    tolkningen, så att en maskering jämförs med de enskilda fältens rutor.
    ``trace`` fylls (om den ges) med en textfri sammanfattning av besluten för
    debugloggning: mall, antal etiketter, fält, avvisade etiketter, spärrar,
    lösta kryssrutemasker och ``degraded``. Den bär aldrig dokumenttext.
    """
    import form_layout  # lokalt: form_layout importerar den här modulen

    blocks, degraded = parse_blocks(raw)
    blocks = form_layout.postprocess(blocks, image=image, page_height=page_height, trace=trace)
    if redactions:
        blocks = with_redactions(blocks, redactions)
    if trace is not None:
        trace["degraded"] = degraded
    return blocks, degraded


def _renumber(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    return [dataclasses.replace(b, order=i) for i, b in enumerate(blocks)]


# --- Text -----------------------------------------------------------------


def fields_text(fields: tuple[FormField, ...] | list[FormField]) -> str:
    """Fältens rena text: ``01 Etikett: värde`` per fält, ``☒ alternativ`` per kryssruta.

    Har mallen skrivit om etikettraden (``source``) följer den råa raden på egen rad.
    """
    lines = []
    for f in fields:
        if f.checked is not None:
            lines.append(f"{CHECKBOX_ON if f.checked else CHECKBOX_OFF} {f.label}")
        else:
            head = f"{f.number} {f.label}" if f.number else f.label
            lines.append(f"{head}: {f.value}")
            if f.source:
                lines.append(f.source)  # så att ren text aldrig tappar ett ord från raden
    return "\n".join(lines)


def _block_text(block: LayoutBlock) -> str:
    if block.fields is not None:
        return fields_text(block.fields)
    return block.text


def page_text(blocks: list[LayoutBlock]) -> str:
    """Blockens rena text i ordning, skilda med radbrytning (celler med tabb)."""
    return "\n".join(t for t in (_block_text(b) for b in blocks) if t)


# --- Markdown ---------------------------------------------------------------

# rader som annars blir rubrik, citat, lista eller (setext-)rubrikstreck/avdelare
_ESCAPE_LINE = re.compile(r"^(#|>|[-*+] |\d+\. |=+\s*$|-+\s*$|(?:[-*_]\s*){3,}$)")


def _escape_lines(text: str) -> str:
    return "\n".join("\\" + ln if _ESCAPE_LINE.match(ln) else ln for ln in text.split("\n"))


def _cell_md(text: str) -> str:
    return _flat(text).replace("|", "\\|")


def _table_md(rows: tuple[tuple[Cell, ...], ...]) -> str:
    grid: list[list[str]] = []
    taken: set[tuple[int, int]] = set()
    for r, row in enumerate(rows):
        while len(grid) <= r:
            grid.append([])
        c = 0
        for cell in row:
            while (r, c) in taken:
                c += 1
            for dr in range(cell.row_span):
                for dc in range(cell.col_span):
                    if dr or dc:
                        taken.add((r + dr, c + dc))
            line = grid[r]
            line += [""] * (c - len(line))
            line.append(_cell_md(cell.text))
            c += 1
            for _ in range(cell.col_span - 1):
                line.append("")
                c += 1
    width = max((len(g) for g in grid), default=0)
    if width == 0:
        return ""
    # rowspan kan peka under sista raden: spannade rader utan egna celler
    lines = []
    for g in grid:
        g = g + [""] * (width - len(g))
        lines.append("| " + " | ".join(g) + " |")
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def _form_md(fields: tuple[FormField, ...]) -> str:
    def clean(text: str) -> str:
        return _escape_lines(_flat(text)).replace("|", "\\|")

    # gruppera: fält följt av sina kryssrutealternativ
    groups: list[tuple[FormField | None, list[FormField]]] = []
    for f in fields:
        if f.checked is None or not groups:
            groups.append((f, []) if f.checked is None else (None, [f]))
        else:
            groups[-1][1].append(f)
    lines: list[str] = []
    for head_field, options in groups:
        # tomma fält skrivs inte (blankettens huvud upprepas tomt på förhörssidor);
        # ett fält med en ikryssad ruta visas med alla sina alternativ
        if head_field is not None:
            if not head_field.value.strip() and not any(o.checked for o in options):
                continue
            head = clean(f"{head_field.number} {head_field.label}" if head_field.number else head_field.label)
            lines.append(f"- **{head}:** {clean(head_field.value)}".rstrip())
        elif not any(o.checked for o in options):
            continue
        lines += [f"  - [{'x' if o.checked else ' '}] {clean(o.label)}".rstrip() for o in options]
    return "\n".join(lines)


def _fence(text: str) -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _block_md(b: LayoutBlock) -> str:
    k = b.kind
    if k is BlockKind.HEADING:
        return "#" * (b.level or DEFAULT_HEADING_LEVEL) + " " + _flat(b.text)
    if k is BlockKind.LIST_ITEM:
        first, *rest = _escape_lines(b.text).split("\n")
        return "\n".join([f"- {first}"] + [f"  {ln}" for ln in rest])
    if k is BlockKind.FORM and b.fields is not None:
        return _form_md(b.fields)
    if k in (BlockKind.TABLE, BlockKind.FORM) and b.rows is not None:
        return _table_md(b.rows)
    if k is BlockKind.FORMULA:
        return f"$$\n{b.latex if b.latex is not None else b.text}\n$$"
    if k is BlockKind.CODE:
        fence = _fence(b.text)
        return f"{fence}\n{b.text}\n{fence}"
    if k is BlockKind.CAPTION:
        return "*Bildtext:* " + _escape_lines(b.text)
    if k is BlockKind.FOOTNOTE:
        return "*Fotnot:* " + _escape_lines(b.text)
    return _escape_lines(b.text)


def _turn_md(who: str | None, said: str) -> str:
    """En replik som eget stycke med talarmarkören i fetstil (``**F:** …``)."""
    text = _escape_lines(said)
    if who is None:
        return text
    marker = who if who.startswith("[") else f"{who}:"
    return f"**{marker}** {text}".rstrip()


def page_markdown(blocks: list[LayoutBlock]) -> str:
    """GFM för sidan. Sidhuvud blir en första kontextrad, sidfot utelämnas."""
    parts: list[str] = []
    headers = [_flat(b.text) for b in blocks if b.kind is BlockKind.PAGE_HEADER and b.text.strip()]
    if headers:
        parts.append("> Sidhuvud: " + " · ".join(headers)[:HEADER_MAX_CHARS])
    from form_layout import split_turns  # lokalt: form_layout importerar den här modulen

    # en maskerad talare ([MASKAD] först) räknas bara på en sida som har dialog
    dialog = any(
        who is not None
        for b in blocks
        if b.kind is BlockKind.PARAGRAPH
        for who, _ in split_turns(b.text, allow_masked=False)
    )
    for b in blocks:
        if b.kind in (BlockKind.PAGE_HEADER, BlockKind.PAGE_FOOTER):
            continue
        if b.kind is BlockKind.PARAGRAPH:
            # maskerad talare bara i ett block på en rad (en replik), aldrig i prosa med radbrott
            turns = split_turns(b.text, allow_masked=dialog and "\n" not in b.text)
            if any(who is not None for who, _ in turns):
                parts += [_turn_md(who, said) for who, said in turns if who is not None or said.strip()]
                continue
        md = _block_md(b)
        if md.strip():
            parts.append(md)
    return "\n\n".join(parts)


# --- JSON -------------------------------------------------------------------


# Layout sparad före beslut BB är en ren blocklista utan dpi; alla sådana sidor
# renderades i operationernas standard, 300 dpi.
LEGACY_LAYOUT_DPI = 300


def layout_to_json(blocks: list[LayoutBlock], dpi: int) -> str:
    """``pdf_pages.layout_json``: ``{"dpi": N, "blocks": [...]}``.

    ``dpi`` är upplösningen sidbilden faktiskt renderades i; blockens bbox är i
    den bildens pixlar. En sida som är för stor för workerns pixelgräns renderas
    i lägre dpi än den begärda, så dpi:n måste följa med för att ramarna ska
    kunna skalas rätt.
    """
    return json.dumps({"dpi": dpi, "blocks": _blocks_data(blocks)}, ensure_ascii=False)


def layout_from_json(s: str) -> tuple[int, list[LayoutBlock]]:
    """``(dpi, block)`` ur ``layout_json``; det äldre listformatet läses som 300 dpi.

    Höjer ``ValueError`` (även ``json.JSONDecodeError``) för en ogiltig layout.
    """
    data = json.loads(s)
    if isinstance(data, list):
        return LEGACY_LAYOUT_DPI, _blocks_from_data(data)
    if not isinstance(data, dict) or not isinstance(data.get("blocks"), list):
        raise ValueError("layout_json är varken en blocklista eller {dpi, blocks}")
    dpi = data.get("dpi")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError(f"layout_json har ogiltig dpi: {dpi!r}")
    return dpi, _blocks_from_data(data["blocks"])


def blocks_to_json(blocks: list[LayoutBlock]) -> str:
    """Bara blocklistan (det äldre formatet, utan dpi); se ``layout_to_json``."""
    return json.dumps(_blocks_data(blocks), ensure_ascii=False)


def _blocks_data(blocks: list[LayoutBlock]) -> list[dict[str, Any]]:
    def conv(b: LayoutBlock) -> dict[str, Any]:
        return {
            "kind": b.kind.value,
            "text": b.text,
            "order": b.order,
            "bbox": list(b.bbox) if b.bbox is not None else None,
            "level": b.level,
            "rows": [[dataclasses.asdict(c) for c in row] for row in b.rows] if b.rows is not None else None,
            "latex": b.latex,
            "fields": [dataclasses.asdict(f) for f in b.fields] if b.fields is not None else None,
            "parts": [list(p) if p is not None else None for p in b.parts] if b.parts is not None else None,
        }

    return [conv(b) for b in blocks]


def blocks_from_json(s: str) -> list[LayoutBlock]:
    """Blocken ur ``layout_json`` i båda formaten (dpi:n kastas; se ``layout_from_json``)."""
    return layout_from_json(s)[1]


def _blocks_from_data(data: list[Any]) -> list[LayoutBlock]:
    out: list[LayoutBlock] = []
    for d in data:
        rows = d.get("rows")
        fields = d.get("fields")
        parts = d.get("parts")
        out.append(
            LayoutBlock(
                kind=BlockKind(d["kind"]),
                text=d["text"],
                order=d["order"],
                bbox=tuple(d["bbox"]) if d.get("bbox") is not None else None,
                level=d.get("level"),
                rows=tuple(tuple(Cell(**c) for c in row) for row in rows) if rows is not None else None,
                latex=d.get("latex"),
                fields=tuple(FormField(**f) for f in fields) if fields is not None else None,
                parts=tuple(tuple(p) if p is not None else None for p in parts) if parts is not None else None,
            )
        )
    return out


# --- Maskeringar --------------------------------------------------------------


def has_mask(text: str) -> bool:
    """Texten har redan en maskeringsmarkör: ``[MASKAD]`` eller Suryas
    ``[REDACTED]`` i valfritt skiftläge och med blanksteg innanför hakarna."""
    return MASK_MARKER in text or REDACTED_RE.search(text) is not None


def with_redactions(blocks: list[LayoutBlock], redactions: list[tuple[int, int]]) -> list[LayoutBlock]:
    """Infoga ``[MASKAD]``-stycken för pixeldetekterade maskeringar (y0, y1).

    Ett block placeras efter det sista blocket vars y-mitt ligger ovanför
    maskeringens. En maskering hoppas över när ett block som redan har en
    maskeringsmarkör täcker samma y-intervall (så ingen maskering räknas två gånger).
    För ett blankettblock med fältrutor (``parts``) jämförs varje fält för sig:
    blankettens samlade ruta spänner över hela sidhuvudet.
    """
    result = list(blocks)
    masked: list[tuple[int, int, int, int]] = []
    for b in blocks:
        if b.fields is not None and b.parts is not None:
            for f, part in zip(b.fields, b.parts, strict=True):
                if part is not None and has_mask(fields_text((f,))):
                    masked.append(part)
        elif b.bbox is not None and has_mask(_block_text(b)):
            masked.append(b.bbox)
    for y0, y1 in sorted(redactions, key=lambda r: r[0] + r[1]):
        if any(y0 <= bb[3] and bb[1] <= y1 for bb in masked):
            continue
        mid = (y0 + y1) / 2
        pos = 0
        for i, b in enumerate(result):
            if b.bbox is not None and (b.bbox[1] + b.bbox[3]) / 2 <= mid:
                pos = i + 1
        result.insert(pos, LayoutBlock(BlockKind.PARAGRAPH, MASK_MARKER, 0, (0, y0, 0, y1)))
    return _renumber(result)
