"""Tester för den rena layoutmodulen (HTML-block från Surya -> struktur, text, Markdown)."""

import json
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

import pytest

import surya_layout as sl
from surya_layout import BlockKind, Cell, FormField, LayoutBlock

FIXTURES = Path(__file__).parent / "fixtures" / "surya"


def load_fixture(name: str) -> list[dict]:
    """Läs ett riktigt workersvar och byt infoscry-nyckeln mot vårt protokoll."""
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    out = []
    for b in data["blocks"]:
        b = dict(b)
        b["reading_order"] = b.pop("readingOrder", None)
        out.append(b)
    return out


def raw(html, label="Text", order=0, bbox=(0, 0, 10, 10)):
    return {"html": html, "label": label, "reading_order": order, "bbox": list(bbox), "confidence": None}


@pytest.mark.parametrize(
    "label,html,kind",
    [
        ("SectionHeader", "<h3>Rubrik</h3>", BlockKind.HEADING),
        ("Text", "<p>Text</p>", BlockKind.PARAGRAPH),
        ("Bibliography", "<p>Källa</p>", BlockKind.PARAGRAPH),
        ("ListGroup", "<ul><li>a</li></ul>", BlockKind.LIST_ITEM),
        ("Table", "<table><tr><td>a</td></tr></table>", BlockKind.TABLE),
        ("TableOfContents", "<table><tr><td>a</td></tr></table>", BlockKind.TABLE),
        ("Form", "<table><tr><td>a</td></tr></table>", BlockKind.FORM),
        ("Equation", "<math>x^2</math>", BlockKind.FORMULA),
        ("Code", "<pre>x = 1</pre>", BlockKind.CODE),
        ("Caption", "<p>Bild</p>", BlockKind.CAPTION),
        ("Footnote", "<p>Not</p>", BlockKind.FOOTNOTE),
        ("PageHeader", "<p>Huvud</p>", BlockKind.PAGE_HEADER),
        ("PageFooter", "<p>Fot</p>", BlockKind.PAGE_FOOTER),
        ("Picture", "<p>Text i bild</p>", BlockKind.FIGURE),
        ("Figure", "<p>Text i bild</p>", BlockKind.FIGURE),
        ("Diagram", "<p>Text i bild</p>", BlockKind.FIGURE),
        ("ChemicalBlock", "<p>H2O</p>", BlockKind.FIGURE),
        ("Okänd", "<p>Något</p>", BlockKind.PARAGRAPH),
        (None, "<p>Något</p>", BlockKind.PARAGRAPH),
    ],
)
def test_label_mapping(label, html, kind):
    blocks, degraded = sl.parse_blocks([raw(html, label)])
    assert [b.kind for b in blocks] == [kind]
    assert degraded is False


def test_heading_level_and_default():
    blocks, _ = sl.parse_blocks(
        [raw("<h4>Fyra</h4>", "SectionHeader", 0), raw("<p>Utan nivå</p>", "SectionHeader", 1)]
    )
    assert [b.level for b in blocks] == [4, 2]
    assert sl.page_markdown(blocks) == "#### Fyra\n\n## Utan nivå"


def test_sorted_by_reading_order_stable():
    blocks, _ = sl.parse_blocks(
        [raw("B", order=2), raw("C", order=None), raw("D", order=None), raw("A", order=1)]
    )
    assert [b.text for b in blocks] == ["A", "B", "C", "D"]
    assert [b.order for b in blocks] == [0, 1, 2, 3]


def test_table_with_spans():
    html = "<table><tr><th>Namn</th><th>Tid</th></tr><tr><td colspan=\"2\">Okänd</td></tr></table>"
    blocks, _ = sl.parse_blocks([raw(html, "Table")])
    table = blocks[0]
    assert table.rows is not None
    assert table.rows[1][0].col_span == 2
    assert table.rows[0][0] == Cell("Namn", True)
    assert sl.page_markdown(blocks) == "| Namn | Tid |\n| --- | --- |\n| Okänd |  |"


def test_rowspan_repeats_empty_below_and_spans_are_clamped():
    html = (
        "<table><tr><td rowspan=\"2\">A</td><td colspan=\"999\">B</td></tr>"
        "<tr><td>C</td></tr></table>"
    )
    blocks, _ = sl.parse_blocks([raw(html, "Table")])
    assert blocks[0].rows[0][1].col_span == 50  # type: ignore[index]
    md = sl.page_markdown(blocks).splitlines()
    assert md[2].startswith("|  | C |")  # A:s rowspan lämnar tom position


def test_table_without_header_row_uses_first_row_as_header():
    blocks, _ = sl.parse_blocks([raw("<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>", "Table")])
    assert sl.page_markdown(blocks) == "| a | b |\n| --- | --- |\n| c | d |"


def test_malformed_table_degrades():
    blocks, degraded = sl.parse_blocks([raw("<p>bara text</p>", "Table")])
    assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH]
    assert blocks[0].text == "bara text"
    assert degraded is True
    blocks, degraded = sl.parse_blocks([raw("<p>bara text</p>", "Form")])
    assert [b.kind for b in blocks] == [BlockKind.FORM]
    assert blocks[0].text == "bara text"
    assert degraded is False


def test_text_around_table_is_kept_as_paragraphs():
    html = "<p>Före</p><table><tr><td>x</td></tr></table><p>Efter</p>"
    blocks, _ = sl.parse_blocks([raw(html, "Table")])
    assert [(b.kind, b.text) for b in blocks] == [
        (BlockKind.PARAGRAPH, "Före"),
        (BlockKind.TABLE, "x"),
        (BlockKind.PARAGRAPH, "Efter"),
    ]


def test_list_group_splits_items():
    blocks, _ = sl.parse_blocks([raw("<ul><li>a</li><li>b</li></ul>", "ListGroup")])
    assert [(b.kind, b.text) for b in blocks] == [(BlockKind.LIST_ITEM, "a"), (BlockKind.LIST_ITEM, "b")]
    assert sl.page_markdown(blocks) == "- a\n\n- b"


def test_list_without_items_keeps_text():
    blocks, _ = sl.parse_blocks([raw("<p>ensam</p>", "ListGroup")])
    assert [(b.kind, b.text) for b in blocks] == [(BlockKind.LIST_ITEM, "ensam")]


def test_page_text_uses_tabs_and_keeps_headers():
    blocks, _ = sl.parse_blocks(
        [
            raw("<p>Sid 2(5)</p>", "PageHeader", 0),
            raw("<table><tr><th>Namn</th><th>Tid</th></tr><tr><td>X</td><td>10</td></tr></table>", "Table", 1),
            raw("<p>76</p>", "PageFooter", 2),
        ]
    )
    text = sl.page_text(blocks)
    assert "Namn\tTid" in text
    assert "Sid 2(5)" in text
    assert "76" in text


def test_markdown_header_line_and_no_footer():
    blocks, _ = sl.parse_blocks(
        [
            raw("<p>Förhör med X</p>", "PageHeader", 0),
            raw("<p>Sid 2(5)</p>", "PageHeader", 1),
            raw("<p>Brödtext</p>", "Text", 2),
            raw("<p>76</p>", "PageFooter", 3),
        ]
    )
    md = sl.page_markdown(blocks)
    assert md == "> Sidhuvud: Förhör med X · Sid 2(5)\n\nBrödtext"
    assert "76" not in md


def test_markdown_header_truncated_to_200():
    blocks, _ = sl.parse_blocks([raw("<p>" + "x" * 500 + "</p>", "PageHeader", 0)])
    line = sl.page_markdown(blocks)
    assert line == "> Sidhuvud: " + "x" * 200


def test_markdown_escapes_content():
    blocks, _ = sl.parse_blocks(
        [
            raw("<p># 12 i ordningen</p>", "Text", 0),
            raw("<p>1. punkt</p>", "Text", 1),
            raw("<table><tr><td>a|b</td></tr></table>", "Table", 2),
        ]
    )
    md = sl.page_markdown(blocks)
    assert "\\# 12 i ordningen" in md
    assert "\\1. punkt" in md
    assert "a\\|b" in md


def test_cell_newline_becomes_space_in_markdown():
    blocks, _ = sl.parse_blocks([raw("<table><tr><td>rad1<br/>rad2</td></tr></table>", "Table")])
    assert "rad1 rad2" in sl.page_markdown(blocks)
    assert "\n" not in blocks[0].rows[0][0].text  # type: ignore[index]


def test_formula_code_caption_footnote_markdown():
    blocks, _ = sl.parse_blocks(
        [
            raw("<math>E=mc^2</math>", "Equation", 0),
            raw("<pre>a = 1\n  b = 2</pre>", "Code", 1),
            raw("<p>Bild 1</p>", "Caption", 2),
            raw("<p>Not 1</p>", "Footnote", 3),
        ]
    )
    assert blocks[0].latex == "E=mc^2"
    md = sl.page_markdown(blocks)
    assert "$$\nE=mc^2\n$$" in md
    assert "```\na = 1\n  b = 2\n```" in md
    assert "*Bildtext:* Bild 1" in md
    assert "*Fotnot:* Not 1" in md


def test_inline_formatting_entities_and_checkboxes():
    html = (
        "<p><b>Fet</b> <u>under</u> <i>kursiv</i> &amp; &lt;x&gt; "
        "<input type=\"checkbox\"/> nej <input checked=\"\" type=\"checkbox\"/> ja</p>"
        "<script>alert(1)</script>"
    )
    blocks, _ = sl.parse_blocks([raw(html)])
    assert blocks[0].text == "Fet under kursiv & <x> ☐ nej ☒ ja"


def test_br_and_paragraphs_become_lines():
    blocks, _ = sl.parse_blocks([raw("<p>a<br/>b</p><p>c</p>")])
    assert blocks[0].text == "a\nb\nc"


def test_empty_and_figure_without_text_are_dropped():
    blocks, _ = sl.parse_blocks([raw("<img/>", "Picture", 0), raw("<p> </p>", "Text", 1), raw("<p>kvar</p>", "Text", 2)])
    assert [b.text for b in blocks] == ["kvar"]


def test_bbox_parsed_and_invalid_bbox_is_none():
    ok = {"html": "a", "label": "Text", "reading_order": 0, "bbox": [1.0, 2, 3, 4], "confidence": 0.9}
    bad = {"html": "b", "label": "Text", "reading_order": 1, "bbox": None, "confidence": None}
    blocks, _ = sl.parse_blocks([ok, bad])
    assert blocks[0].bbox == (1, 2, 3, 4)
    assert blocks[1].bbox is None


def test_parse_requires_reading_order_key():
    legacy = {"html": "A", "label": "Text", "readingOrder": 0, "bbox": [0, 0, 1, 1]}
    blocks, _ = sl.parse_blocks([legacy, raw("B", order=0)])
    assert [b.text for b in blocks] == ["B", "A"]  # readingOrder ignoreras -> sist


class _Visible(HTMLParser):
    """Oberoende textutvinning för testet: synlig text med kryssrutor som ☐/☒."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self.skip += 1
        elif tag == "input" and dict(attrs).get("type") == "checkbox":
            self.out.append("☒" if any(k == "checked" for k, _ in attrs) else "☐")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def visible_chars(html: str) -> Counter:
    v = _Visible()
    v.feed(html)
    v.close()
    return Counter("".join("".join(v.out).split()))


def assert_conserved(items: list[dict]) -> None:
    expected: Counter = Counter()
    for it in items:
        expected += visible_chars(it["html"])
    blocks, _ = sl.parse_blocks(items)
    got = Counter("".join(sl.page_text(blocks).split()))
    assert got == expected, (expected - got, got - expected)


@pytest.mark.parametrize("name", ["form-v1-a", "form-v1-c", "form-v1-d", "form-v1-e", "form-v2-a", "form-v2-b", "form-v2-c", "form-v2-d", "form-v2-e", "forhor-dialog-a", "forhor-dialog-b", "forhor-dialog-c"])
def test_real_fixtures_parse_and_keep_text(name):
    items = load_fixture(name)
    blocks, degraded = sl.parse_blocks(items)
    assert blocks and degraded is False
    assert_conserved(items)
    sl.page_markdown(blocks)


@pytest.mark.parametrize(
    "html,label",
    [
        ("<table><caption>Rubrik</caption><tr><td>a</td></tr></table>", "Table"),
        ("<table><tr>lös text<td>a</td>mer lös</tr></table>", "Table"),
        ("<table><thead>i thead<tr><td>a</td></tr></thead><tbody>i tbody<tr><td>b</td></tr></tbody>text i table</table>", "Table"),
        ("<table><td>utanför rad</td><tr><td>a</td></tr></table>", "Form"),
        ("<table><tr><td>a<table><tr><td>inre</td></tr></table></td></tr></table>", "Table"),
        ("<table><tr><td>a</td></tr><table><tr><td>direkt nästlad</td></tr></table></table>", "Table"),
        ("<table><tr><td>a<td>b<tr><td>c<td><input checked=\"\" type=\"checkbox\"/>d", "Table"),
        ("före<table><tr><td>a</td></tr></table><input type=\"checkbox\"/>efter", "Form"),
        ("<ul>före<li>a</li>mitt<li>b<ul><li>c</li></ul></li>efter</ul>", "ListGroup"),
        ("<table><tr><td></td></tr></table><p>bara utanför</p>", "Table"),
    ],
)
def test_malformed_tables_keep_every_character(html, label):
    assert_conserved([raw(html, label)])


def test_caption_comes_before_table_and_stray_text_after():
    html = "<table><caption>Rubrik</caption><tr>lös<td>a</td></tr></table>"
    blocks, _ = sl.parse_blocks([raw(html, "Table")])
    assert [(b.kind, b.text) for b in blocks] == [
        (BlockKind.CAPTION, "Rubrik"),
        (BlockKind.TABLE, "a"),
        (BlockKind.PARAGRAPH, "lös"),
    ]


def test_list_preface_and_trailing_text_keep_position():
    blocks, _ = sl.parse_blocks([raw("<ul>före<li>a</li>mitt<li>b</li>efter</ul>", "ListGroup")])
    assert [(b.kind, b.text) for b in blocks] == [
        (BlockKind.PARAGRAPH, "före"),
        (BlockKind.LIST_ITEM, "a"),
        (BlockKind.PARAGRAPH, "mitt"),
        (BlockKind.LIST_ITEM, "b"),
        (BlockKind.PARAGRAPH, "efter"),
    ]


def test_non_finite_reading_order_counts_as_missing():
    items = [raw("B", order=float("nan")), raw("A", order=1), raw("C", order=float("inf")), raw("Z", order=0)]
    blocks, _ = sl.parse_blocks(items)
    assert [b.text for b in blocks] == ["Z", "A", "B", "C"]


def test_deep_nesting_falls_back_to_plain_paragraph():
    html = "<div>" * 5000 + "djup text &amp; mer" + "</div>" * 5000
    blocks, degraded = sl.parse_blocks([raw(html, "Text"), raw("<p>vanlig</p>", "Text", 1)])
    assert degraded is True
    assert [(b.kind, b.text) for b in blocks] == [
        (BlockKind.PARAGRAPH, "djup text & mer"),
        (BlockKind.PARAGRAPH, "vanlig"),
    ]


def test_form_markdown_escapes_labels_and_values():
    form = LayoutBlock(
        BlockKind.FORM, "", 0, None, fields=(FormField("01", "A|B", "# 12 | x", None), FormField(None, "v|w", "", False))
    )
    assert sl.page_markdown([form]) == "- **01 A\\|B:** \\# 12 \\| x\n  - [ ] v\\|w"


def test_real_fixture_checkbox_and_form_table():
    blocks, _ = sl.parse_blocks(load_fixture("form-v1-a"))
    assert "☒ per telefon" in sl.page_text(blocks)
    blocks, _ = sl.parse_blocks(load_fixture("form-v2-b"))
    forms = [b for b in blocks if b.kind is BlockKind.FORM]
    assert forms and forms[0].rows
    assert "07 Avsnitt\t08 Löpande nr" in sl.page_text(blocks)


def test_form_with_fields_markdown_and_text():
    form = LayoutBlock(
        BlockKind.FORM,
        "",
        0,
        None,
        fields=(
            FormField("01", "Polisdistrikt", "Stockholm", None),
            FormField("02", "Arbetsenhet", "", None),
            FormField(None, "per telefon", "", True),
            FormField(None, "per brev", "", False),
        ),
    )
    md = sl.page_markdown([form])
    # tomt fält med alternativ under behåller sin rubrik
    assert md == (
        "- **01 Polisdistrikt:** Stockholm\n- **02 Arbetsenhet:**\n  - [x] per telefon\n  - [ ] per brev"
    )
    text = sl.page_text([form])
    assert "01 Polisdistrikt: Stockholm" in text
    assert "02 Arbetsenhet: " in text


def test_with_redactions_orders_by_y():
    blocks, _ = sl.parse_blocks(
        [raw("först", order=0, bbox=(0, 100, 50, 120)), raw("sist", order=1, bbox=(0, 300, 50, 320))]
    )
    out = sl.with_redactions(blocks, [(200, 210)])
    assert [b.text for b in out] == ["först", "[MASKAD]", "sist"]
    assert [b.order for b in out] == [0, 1, 2]
    assert out[1].kind is BlockKind.PARAGRAPH


def test_with_redactions_skips_already_masked_block():
    blocks, _ = sl.parse_blocks(
        [
            raw("Stockholm, [REDACTED].", order=0, bbox=(0, 100, 50, 140)),
            raw("annat", order=1, bbox=(0, 300, 50, 320)),
        ]
    )
    out = sl.with_redactions(blocks, [(110, 130), (200, 210)])
    assert [b.text for b in out] == ["Stockholm, [REDACTED].", "[MASKAD]", "annat"]
    masked = sl.with_redactions(
        [replace_text(blocks[0], "Stockholm, [MASKAD].")], [(110, 130)]
    )
    assert [b.text for b in masked] == ["Stockholm, [MASKAD]."]


@pytest.mark.parametrize("marker", ["[Redacted]", "[redacted]", "[ REDACTED ]"])
def test_with_redactions_recognises_redacted_in_any_case(marker):
    # Beslut BD: kontrollen "har redan en maskering" (beslut D) känner igen samma
    # varianter som replace_redacted, även i ett blankettfält
    blocks, _ = sl.parse_blocks([raw(f"Stockholm, {marker}.", order=0, bbox=(0, 100, 50, 140))])
    assert [b.text for b in sl.with_redactions(blocks, [(110, 130)])] == [f"Stockholm, {marker}."]
    form = LayoutBlock(
        BlockKind.FORM, "", 0, (0, 0, 100, 400),
        fields=(FormField("01", "L", marker, None), FormField("02", "M", "x", None)),
        parts=((0, 100, 50, 140), (0, 300, 50, 340)),
    )
    out = sl.with_redactions([form], [(110, 130), (310, 330)])
    assert [b.text for b in out if b.fields is None] == ["[MASKAD]"]


def replace_text(block, text):
    import dataclasses

    return dataclasses.replace(block, text=text)


def test_json_roundtrip():
    blocks, _ = sl.parse_blocks(
        [
            raw("<h2>R</h2>", "SectionHeader", 0),
            raw("<table><tr><th>A</th><td rowspan=\"2\">B</td></tr></table>", "Table", 1),
            raw("<math>x</math>", "Equation", 2),
        ]
    )
    blocks.append(
        LayoutBlock(BlockKind.FORM, "", 3, (1, 2, 3, 4), fields=(FormField("01", "L", "V", None), FormField(None, "k", "", True)))
    )
    again = sl.blocks_from_json(sl.blocks_to_json(blocks))
    assert again == blocks


def test_markdown_escapes_quote_and_setext_lines():
    blocks, _ = sl.parse_blocks(
        [
            raw("<p>&gt; huvud</p>", "PageHeader", 0),
            raw("<p>&gt; citat<br/>---<br/>===<br/>- - -<br/>Rubrik<br/>=<br/>vanlig - text</p>", "Text", 1),
        ]
    )
    md = sl.page_markdown(blocks)
    assert md.split("\n")[0] == "> Sidhuvud: > huvud"  # vår egen kontextrad escapas inte
    assert md.split("\n\n")[1] == "\\> citat\n\\---\n\\===\n\\- - -\nRubrik\n\\=\nvanlig - text"


def test_field_text_formatting_has_one_home():
    import form_layout

    assert not hasattr(form_layout, "_fields_text")
    fields = (FormField("01", "Ort", "Sthlm", None), FormField("01", "per brev", "", True))
    assert sl.fields_text(fields) == "01 Ort: Sthlm\n☒ per brev"


def test_json_roundtrip_keeps_field_parts():
    form = LayoutBlock(
        BlockKind.FORM, "01 L: V", 0, (1, 2, 30, 40), fields=(FormField("01", "L", "V", None),), parts=((1, 2, 30, 20),)
    )
    assert sl.blocks_from_json(sl.blocks_to_json([form])) == [form]


def test_layout_json_carries_dpi():
    # beslut BB: layout_json bär renderingens dpi, så att ramarna kan skalas rätt
    # även för sidor som renderats i lägre upplösning än den begärda
    blocks, _ = sl.parse_blocks([raw("<p>Hej</p>", bbox=(1, 2, 30, 40))])
    data = sl.layout_to_json(blocks, 212)
    assert json.loads(data)["dpi"] == 212
    assert sl.layout_from_json(data) == (212, blocks)
    assert sl.blocks_from_json(data) == blocks


def test_layout_json_old_list_format_is_300_dpi():
    blocks, _ = sl.parse_blocks([raw("<p>Hej</p>", bbox=(1, 2, 30, 40))])
    old = sl.blocks_to_json(blocks)
    assert old.startswith("[")
    assert sl.layout_from_json(old) == (sl.LEGACY_LAYOUT_DPI, blocks)
    assert sl.LEGACY_LAYOUT_DPI == 300


@pytest.mark.parametrize("bad", ['{"dpi": 0, "blocks": []}', '{"dpi": "300", "blocks": []}', '{"blocks": []}', "7"])
def test_layout_json_rejects_bad_dpi(bad):
    with pytest.raises(ValueError):
        sl.layout_from_json(bad)
