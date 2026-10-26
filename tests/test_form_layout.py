"""Tester för blankett- och förhörstolkningen ovanpå layoutmodulen (Task 2b)."""

import difflib
import re
from collections import Counter
from html.parser import HTMLParser

import numpy as np
import pytest
from test_surya_layout import load_fixture, raw

import form_layout as fl
import surya_layout as sl
from surya_layout import BlockKind, FormField

FIXTURES = [
    "form-v1-a", "form-v1-c", "form-v1-d", "form-v1-e", "form-v2-a", "form-v2-b", "form-v2-c", "form-v2-d", "form-v2-e",
    "forhor-dialog-a", "forhor-dialog-b", "forhor-dialog-c",
]
# sidor med blankettfält (forhor-dialog-c är en ren dialogsida utan blankett och mall)
FORM_FIXTURES = [n for n in FIXTURES if n != "forhor-dialog-c"]


def form_fields(blocks):
    forms = [b for b in blocks if b.kind is BlockKind.FORM and b.fields and b.fields[0].number]
    assert len(forms) == 1, forms
    return forms[0].fields


def field(fields, number, label=None):
    hits = [f for f in fields if f.number == number and f.checked is None]
    assert len(hits) == 1, (number, hits)
    if label is not None:
        assert hits[0].label == label
    return hits[0]


def option(fields, number, label):
    hits = [f for f in fields if f.number == number and f.checked is not None and f.label == label]
    assert len(hits) == 1, (number, label, [f for f in fields if f.number == number])
    return hits[0]


# --- Blankettfält ur riktiga svar ----------------------------------------------


def test_parse_spaningsuppslag_fields():
    blocks, degraded = sl.parse_page(load_fixture("form-v1-a"))
    assert degraded is False
    fields = form_fields(blocks)
    text = sl.page_text(blocks)
    for line in (
        "01 Polisdistrikt (där uppslaget mottagits): Stockholm",
        "02 Arbetsenhet: KCV",
        "03 Uppgiftsmottagare: Insp NEK Persson",
        "04 telefon: 769 45 81",
        "05 datum: 860316",
        "06 klockan: 09.10",
        "07 Avsnitt: D",
        "08 Löpande nr: 2449",
        "10 Brott med vilket uppslaget hör samman: Mordet på Palme",
    ):
        assert line in text.split("\n"), line
    assert option(fields, "11", "per telefon").checked is True
    assert option(fields, "11", "per brev").checked is False
    # Surya skrev "person-ligt" utan radbrott, så ingen avstavning slås ihop
    assert option(fields, "11", "vid person-ligt besök").checked is False
    assert option(fields, "16", "misstänkt person").checked is True
    assert option(fields, "16", "fordon").checked is False
    assert field(fields, "12").value == ""  # svart balk som Surya utelämnade

    # etikett och värde i samma <br/>-block
    blocks, _ = sl.parse_page(load_fixture("form-v2-b"))
    fields = form_fields(blocks)
    assert field(fields, "01", "Polisdistrikt").value == "Stockholm"
    assert field(fields, "02").value == "RK/A2"
    assert field(fields, "09").value == "Mordet på statsminister Olof Palme"
    # handskrivet: testas så som Surya läste det (bilden: H / 90-66 B)
    assert field(fields, "07").value == "H"
    assert field(fields, "08").value == "90668"

    # fälten kommer som PageHeader
    blocks, _ = sl.parse_page(load_fixture("form-v2-a"))
    fields = form_fields(blocks)
    assert "03 Upprättat(d) av: Krinsp Sören Morberg" in sl.page_text(blocks).split("\n")
    assert field(fields, "02").value == "RKP-A2"
    md = sl.page_markdown(blocks)
    header = [ln for ln in md.split("\n") if ln.startswith("> Sidhuvud:")]
    # blankettfälten hör inte till sidhuvudet; det onumrerade tryckta "telefon*"
    # är inget fält och står kvar som Suryas PageHeader
    assert header == ["> Sidhuvud: telefon* · Sid nr"]


def test_form_block_placed_at_first_label_and_fields_sorted():
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    assert blocks[0].kind is BlockKind.FORM
    numbers = [f.number for f in blocks[0].fields if f.checked is None]
    assert numbers == sorted(numbers)
    assert [b.order for b in blocks] == list(range(len(blocks)))


def test_free_text_field_not_collected():
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    assert field(form_fields(blocks), "18").value == ""
    texts = [b.text for b in blocks if b.fields is None]
    assert "Tipset inringt från vh. befäl Jönköpingspolisen, Götesson,." in texts


def test_empty_header_fields_omitted():
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-a"))
    md = sl.page_markdown(blocks)
    assert not [ln for ln in md.split("\n") if ln.startswith("- **")], md
    assert "Polisdistrikt" not in md
    text = sl.page_text(blocks)
    assert "01 Polisdistrikt: " in text
    assert "08 Löpande nr: " in text
    assert "05 Förhörsprot: " in text  # numret står ovanför den tryckta etiketten


# --- Maskeringar ----------------------------------------------------------------


def _page_image(size, rect=None, hollow=False):
    w, h = size
    img = np.full((h, w), 255, dtype=np.uint8)
    if rect is not None:
        x0, y0, x1, y1 = rect
        img[y0:y1, x0:x1] = 0
        if hollow:
            img[y0 + 4 : y1 - 4, x0 + 4 : x1 - 4] = 255
    return img


def _dialog_b_block(blocks, start):
    hits = [b for b in blocks if b.text.startswith(start)]
    assert len(hits) == 1
    return hits[0]


def test_checkbox_mask_resolved():
    items = load_fixture("forhor-dialog-b")
    # blocket "F: Ja, ☐, klockan …" har bbox [390, 1414, 2161, 1551]
    filled = _page_image((2481, 3509), (620, 1430, 720, 1480))
    blocks, _ = sl.parse_page(items, image=filled)
    b = _dialog_b_block(blocks, "F: Ja,")
    assert b.text.startswith("F: Ja, [MASKAD], klockan börjar")
    # övriga block har ingen fylld ruta i bilden och behåller kryssrutan
    assert _dialog_b_block(blocks, "☐ Ja, det har jag hört.")

    hollow = _page_image((2481, 3509), (620, 1430, 720, 1480), hollow=True)
    blocks, _ = sl.parse_page(items, image=hollow)
    assert _dialog_b_block(blocks, "F: Ja,").text.startswith("F: Ja, ☐, klockan")

    blocks, _ = sl.parse_page(items)  # utan bild rörs inget
    assert _dialog_b_block(blocks, "F: Ja,").text.startswith("F: Ja, ☐, klockan")


def test_filled_dark_boxes_counts_only_filled():
    img = _page_image((400, 100))
    img[20:70, 20:90] = 0  # fylld
    img[20:70, 150:220] = 0
    img[24:66, 154:216] = 255  # ihålig ram
    img[20:70, 300:305] = 0  # smal stapel (som ett "l")
    assert fl.filled_dark_boxes(img, (0, 0, 400, 100)) == 1
    assert fl.filled_dark_boxes(img, (100, 0, 400, 100)) == 0
    assert fl.filled_dark_boxes(_page_image((50, 50)), (0, 0, 50, 50)) == 0


def test_checkbox_mask_needs_enough_boxes_and_skips_fields():
    blocks, _ = sl.parse_blocks([raw('<p>A <input type="checkbox"/> och <input type="checkbox"/></p>', bbox=(0, 0, 400, 100))])
    img = _page_image((400, 100), (20, 20, 90, 70))
    out = fl.resolve_checkbox_masks(blocks, img)
    assert out[0].text == "A ☐ och ☐"  # en fylld ruta räcker inte för två kryssrutor
    img[20:70, 200:270] = 0
    out = fl.resolve_checkbox_masks(blocks, img)
    assert out[0].text == "A [MASKAD] och [MASKAD]"
    form = sl.LayoutBlock(BlockKind.FORM, "", 0, (0, 0, 400, 100), fields=(FormField("11", "per brev", "", False),))
    assert fl.resolve_checkbox_masks([form], img) == [form]


def test_redacted_becomes_maskad():
    blocks, _ = sl.parse_page(load_fixture("form-v2-a"))
    text = sl.page_text(blocks)
    assert "[REDACTED]" not in text
    assert "Stockholm, [MASKAD]." in text
    assert "1986 så bodde Avsan på [MASKAD] där han bott" in text
    assert "[REDACTED]" not in sl.page_markdown(blocks)


@pytest.mark.parametrize("marker", ["[REDACTED]", "[Redacted]", "[redacted]", "[ Redacted ]", "[\tREDACTED  ]"])
def test_redacted_any_case_becomes_maskad(marker):
    # Beslut BD: piloten fann "[Redacted]" (sida 5), som tidigare stod kvar orört
    table = f"<table><tr><td>{marker}</td><td>x</td></tr></table>"
    blocks, _ = sl.parse_blocks([raw(f"<p>Stockholm, {marker}.</p>"), raw(table, "Table", 1)])
    out = fl.replace_redacted(blocks)
    assert out[0].text == "Stockholm, [MASKAD]."
    assert out[1].rows[0][0].text == "[MASKAD]"
    assert "edacted" not in sl.page_markdown(out).lower()


def test_redacted_variants_on_pilot_form():
    # sida 5 i piloten (form-v1-c): Surya skrev "[Redacted]" i fält 12, 13 och 15
    blocks, _ = sl.parse_page(load_fixture("form-v1-c"), page_height=3507)
    fields = form_fields(blocks)
    for number in ("12", "13", "15"):
        assert field(fields, number).value == "[MASKAD]", number
    md = sl.page_markdown(blocks)
    assert "edacted" not in md.lower()
    assert "**[MASKAD]** var på hemväg" in md or "[MASKAD] var på hemväg" in md


def test_dehyphenate():
    assert fl.dehyphenate("polis-\ninspektören") == "polisinspektören"
    assert fl.dehyphenate("Palme-\nUtredningen") == "Palme-\nUtredningen"
    assert fl.dehyphenate("med-\nborgare och öst-\nändan") == "medborgare och öständan"
    assert fl.dehyphenate("ett - \nstreck") == "ett - \nstreck"
    blocks, _ = sl.parse_page(load_fixture("form-v2-a"))
    assert "svenska medborgare 1967" in sl.page_text(blocks).replace("\n", " ")


# --- Mallar ----------------------------------------------------------------------


# två etiketter långt från övriga testinnehåll, så att mallen godtar minst tre
_OTHER_LABELS = (
    ("<p>02 Arbetsenhet</p>", (290, 2000, 600, 2040)),
    ("<p>03 Uppgiftsmottagare</p>", (290, 2300, 700, 2340)),
)


def _page(*htmls_bboxes):
    return [raw(h, "Text", i, bb) for i, (h, bb) in enumerate(htmls_bboxes)]


def test_template_fixes_ocr_label():
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 200, 1700, 260)),
        ("<p>01 Poliodistrikt (där uppslaget mottagits)</p>", (290, 193, 826, 239)),
        ("<p><b>Stockholm</b></p>", (310, 240, 568, 284)),
        *_OTHER_LABELS,
    )
    blocks, _ = sl.parse_blocks(items)
    rest, fields = fl.extract_form_fields(blocks, fl.load_templates(), page_height=3500)
    # beslut BI: den råa OCR-raden följer med (bara i ren text), eftersom mallen rättat den
    assert fields[0] == FormField(
        "01", "Polisdistrikt (där uppslaget mottagits)", "Stockholm", None,
        source="01 Poliodistrikt (där uppslaget mottagits)",
    )
    assert [b.text for b in rest if b.fields is None] == ["SPANINGSUPPSLAG"]
    md = sl.page_markdown(rest)
    assert "Poliodistrikt" not in md and "Poliodistrikt" in sl.page_text(rest)


def test_template_needs_three_accepted_labels():
    # en ensam etikett räcker inte för att mallen ska gälla (minst tre godtagna)
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 200, 1700, 260)),
        ("<p>01 Poliodistrikt (där uppslaget mottagits)</p>", (290, 193, 826, 239)),
        ("<p><b>Stockholm</b></p>", (310, 240, 568, 284)),
    )
    blocks, _ = sl.parse_blocks(items)
    assert fl.extract_form_fields(blocks, fl.load_templates(), page_height=3500) == (blocks, [])


def test_load_templates():
    templates = fl.load_templates()
    names = {t.name: t for t in templates}
    assert set(names) == {"spaningsuppslag-1", "spaningsuppslag-2"}
    assert names["spaningsuppslag-1"].labels["03"] == "Uppgiftsmottagare"
    assert names["spaningsuppslag-2"].labels["03"] == "Upprättat(d) av"
    assert names["spaningsuppslag-1"].free_text == ("18",)
    assert names["spaningsuppslag-2"].free_text == ("10",)


@pytest.mark.parametrize("header", ["", "SPANINGSUPPSLAG"])
def test_no_template_page_blocks_unchanged(header):
    # utan matchande mall tas inga fält ut: blocken lämnas exakt som de var
    items = _page(
        (f"<p>{header or 'Fordon'}</p>", (1300, 100, 1700, 150)),
        ("<p>01 Fordonsslag</p>", (100, 200, 500, 240)),
        ("<p>Personbil</p>", (110, 245, 400, 285)),
        ("<p>02 Färg*</p>", (100, 300, 500, 340)),
        ("<p>Röd</p>", (110, 345, 400, 385)),
        ("<p>03 Ägare</p>", (100, 400, 500, 440)),
    )
    blocks, _ = sl.parse_blocks(items)
    assert fl.extract_form_fields(blocks, fl.load_templates(), page_height=3500) == (blocks, [])
    assert fl.postprocess(blocks, page_height=3500) == blocks


_CHRONOLOGY = [
    ("<p>Kronologi</p>", "Text"),
    ("<p>28 februari</p>", "SectionHeader"),
    ("<p>Mordet på Sveavägen.</p>", "Text"),
    ("<p>01 mars</p>", "SectionHeader"),
    ("<p>Spaningsledningen bildas.</p>", "Text"),
    ("<p>02 mars</p>", "SectionHeader"),
    ("<p>Fantombilden publiceras.</p>", "Text"),
]
_QUANTITIES = [
    ("<p>Förhör med vittnet</p>", "Text"),
    ("<p>Han räknade upp vad han hade på sig:</p>", "Text"),
    ("<p>12 st cigaretter</p>", "Text"),
    ("<p>10 kr i växel</p>", "Text"),
    ("<p>20 års medlemskort</p>", "Text"),
    ("<p>Sedan gick han hem.</p>", "Text"),
]
_TIMETABLE = [
    ("<p>Tidsschema</p>", "Text"),
    ("<p>21 15 Makarna lämnar bostaden</p>", "Text"),
    ("<p>Västerlånggatan</p>", "Text"),
    ("<p>23 15 Biografen slutar</p>", "Text"),
    ("<p>Grand</p>", "Text"),
    ("<p>23 21 Skotten</p>", "Text"),
    ("<p>Sveavägen</p>", "Text"),
]
_LONG_NUMBERED = [
    ("<p>01 Fordonsslag</p>", "Text"),
    ("<p>02 Färg</p>", "Text"),
    ("<p>12 personer var samlade utanför biografen när skotten föll vid korsningen.</p>", "Text"),
    ("<p>03 Ägare</p>", "Text"),
]


@pytest.mark.parametrize(
    "rows", [_CHRONOLOGY, _QUANTITIES, _TIMETABLE, _LONG_NUMBERED], ids=["kronologi", "antal", "tidsschema", "numrerat"]
)
def test_no_template_pages_keep_order_and_text(rows):
    # numrerade rader utan mall är vanlig text: läsordningen och varje rad står kvar
    items = [raw(h, label, i, (100, 100 + 60 * i, 900, 130 + 60 * i)) for i, (h, label) in enumerate(rows)]
    blocks, _ = sl.parse_page(items, page_height=3500)
    lines = [re.sub(r"<[^>]+>", "", h) for h, _ in rows]
    assert not any(b.fields for b in blocks)
    assert sl.page_text(blocks) == "\n".join(lines)
    md_lines = [ln.lstrip("# ") for ln in sl.page_markdown(blocks).split("\n\n")]
    assert md_lines == lines
    assert_conserved_page(items)


def test_no_template_page_keeps_redaction_hyphen_turn_and_header_rules():
    # [REDACTED], avstavning, repliker, förhörsmetadata och kryssrutemasker är ingen
    # blankettolkning och gäller även utan mall
    items = _page(
        ("<p>Förhörsledare: krinsp Sören Morberg.</p>", (100, 100, 1500, 140)),
        ("<p>F: Var bodde polis-<br/>inspektören?</p>", (100, 200, 1500, 280)),
        ("<p>M: På [REDACTED].</p>", (100, 300, 1500, 340)),
        ('<p>F: Och <input type="checkbox"/> var där?</p>', (100, 400, 1500, 460)),
    )
    img = _page_image((1600, 600), (300, 410, 360, 450))
    blocks, _ = sl.parse_page(items, image=img, page_height=600)
    text = sl.page_text(blocks)
    assert "F: Var bodde polisinspektören?" in text.replace("\n", " ")
    assert "M: På [MASKAD]." in text
    assert "F: Och [MASKAD] var där?" in text
    md = sl.page_markdown(blocks)
    assert "- **Förhörsledare:** krinsp Sören Morberg." in md
    assert "**M:** På [MASKAD]." in md.split("\n\n")


def test_free_text_cell_in_table_stays_paragraph_after_form():
    # fritextfältets värdecell förbrukas aldrig; vittnesmålet står kvar som stycke efter fälten
    testimony = "Hon såg en man springa uppför trapporna mot Malmskillnadsgatan klockan 23.25"
    items = [
        raw("<p>SPANINGSUPPSLAG</p>", "Text", 0, (100, 50, 800, 90)),
        raw(
            "<table><tr><td>03 Uppgiftsmottagare</td><td>05 datum</td><td>06 klockan</td></tr>"
            "<tr><td>Insp Persson</td><td>860316</td><td>09.10</td></tr>"
            "<tr><td>18 I sak</td><td></td><td></td></tr>"
            f"<tr><td>{testimony}</td><td></td><td></td></tr></table>",
            "Table",
            1,
            (100, 100, 2400, 600),
        ),
    ]
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "03").value == "Insp Persson"
    assert field(fields, "06").value == "09.10"
    assert field(fields, "18").value == ""
    form_at = [i for i, b in enumerate(blocks) if b.fields][0]
    paras = [i for i, b in enumerate(blocks) if b.text == testimony]
    assert paras and paras[0] > form_at and blocks[paras[0]].kind is BlockKind.PARAGRAPH
    assert testimony in sl.page_text(blocks).split("\n")
    assert testimony in _md_paragraphs(blocks)
    assert_conserved_page(items)


def _masked_header_page():
    return _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt</p>", (100, 200, 500, 240)),
        ("<p>[REDACTED]</p>", (110, 245, 400, 285)),
        ("<p>02 Arbetsenhet</p>", (100, 400, 500, 440)),
        ("<p>KCV</p>", (110, 445, 400, 485)),
        ("<p>03 Uppgiftsmottagare</p>", (100, 600, 500, 640)),
        ("<p>Persson</p>", (110, 645, 400, 685)),
    )


def test_masked_field_value_does_not_suppress_other_redaction():
    # en maskering som redan står i fält 01:s värde hoppas över, men en pixelmaskering
    # vid fält 02 i samma blankett räknas (fältens egna rutor, inte FORM-blockets union)
    blocks, _ = sl.parse_page(_masked_header_page(), page_height=3500, redactions=[(250, 280), (450, 480)])
    fields = form_fields(blocks)
    assert field(fields, "01").value == "[MASKAD]"
    inserted = [b for b in blocks if b.fields is None and b.text == "[MASKAD]"]
    assert len(inserted) == 1 and inserted[0].bbox == (0, 450, 0, 480)
    assert [b.order for b in blocks] == list(range(len(blocks)))
    assert sl.blocks_from_json(sl.blocks_to_json(blocks)) == blocks
    form = [b for b in blocks if b.fields][0]
    assert form.parts is not None and len(form.parts) == len(form.fields)


def test_parse_page_redactions_without_template():
    items = _page(("<p>först</p>", (0, 100, 50, 120)), ("<p>sist</p>", (0, 300, 50, 320)))
    blocks, _ = sl.parse_page(items, redactions=[(200, 210)])
    assert [b.text for b in blocks] == ["först", "[MASKAD]", "sist"]


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("name", FORM_FIXTURES)
def test_trace_summarises_decisions_without_text(name):
    trace: dict = {}
    blocks, degraded = sl.parse_page(load_fixture(name), trace=trace)
    assert set(trace) == {
        "template", "accepted_labels", "fields", "rejected_labels", "barriers", "masks_resolved", "degraded",
        "label_rest_values", "label_rest_text", "label_words_text",
        # beslut BJ: etikettpar, aliasetiketter, värden ur etikettrader (och delade), värden bredvid etiketten
        "label_pairs", "alias_labels", "row_values", "row_splits", "right_values", "row_prose",
    }
    names = {t.name for t in fl.load_templates()}
    assert trace["template"] in names
    assert trace["fields"] == sum(len(b.fields) for b in blocks if b.fields and b.fields[0].number)
    assert trace["degraded"] is degraded
    assert all(
        isinstance(trace[k], int)
        for k in (
            "accepted_labels", "fields", "rejected_labels", "barriers", "masks_resolved",
            "label_rest_values", "label_rest_text", "label_words_text",
            "label_pairs", "alias_labels", "row_values", "row_splits", "right_values", "row_prose",
        )
    )
    # inga dokumentord: bara nycklar och mallens namn är strängar
    assert set(_strings(trace)) - set(trace) <= names


def test_trace_without_template():
    trace: dict = {}
    sl.parse_page(_page(("<p>01 Fordonsslag</p>", (100, 200, 500, 240))), trace=trace)
    assert trace["template"] is None and trace["fields"] == 0 and trace["accepted_labels"] == 0


def test_template_validation_names_file_and_key(tmp_path):
    bad = tmp_path / "trasig.json"
    bad.write_text('{"templates": [{"name": "x", "labels": {"01": "A"}, "fritext": ["01"]}]}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"trasig\.json.*fritext"):
        fl.load_templates(tmp_path)
    wrong = tmp_path / "fel" / "fel.json"
    wrong.parent.mkdir()
    wrong.write_text('{"templates": [{"name": "x", "labels": {"01": "A"}, "free_text": ["18"]}]}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"fel\.json.*free_text"):
        fl.load_templates(wrong.parent)


def test_checkboxes():
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>11 Spaningsuppslaget lämnat</p>", (1322, 481, 1726, 523)),
        ('<p><input checked="" type="checkbox"/> per telefon</p>', (1330, 525, 1600, 580)),
        ('<p><input type="checkbox"/> per brev</p>', (1640, 525, 1900, 580)),
        # övriga etiketter ovanför 11 i nummerordning (en etikett 11 högt
        # över 02 och 03 vore själv ordningsbrottet)
        ("<p>02 Arbetsenhet</p>", (290, 284, 501, 326)),
        ("<p>03 Uppgiftsmottagare</p>", (290, 375, 583, 421)),
    )
    blocks, _ = sl.parse_blocks(items)
    _, fields = fl.extract_form_fields(blocks, fl.load_templates(), page_height=3500)
    assert [f for f in fields if f.number == "11"] == [
        FormField("11", "Spaningsuppslaget lämnat", "", None),
        FormField("11", "per telefon", "", True),
        FormField("11", "per brev", "", False),
    ]
    md = sl.page_markdown(sl.parse_page(items)[0])
    assert "- **11 Spaningsuppslaget lämnat:**\n  - [x] per telefon\n  - [ ] per brev" in md


def test_value_rules_distance_length_and_column():
    long_value = "x" * 201
    items = _page(
        ("<p>01 Ett</p>", (100, 100, 400, 140)),
        ("<p>långt bort</p>", (100, 400, 400, 440)),  # > 0,035 × sidhöjd under etiketten
        ("<p>02 Två</p>", (800, 100, 1100, 140)),
        (f"<p>{long_value}</p>", (800, 145, 1100, 185)),  # för lång
        ("<p>03 Tre</p>", (1500, 100, 1800, 140)),
        ("<p>vänster om etiketten</p>", (1300, 145, 1700, 185)),  # börjar före etikettens x0
    )
    blocks, _ = sl.parse_blocks(items)
    tpl = fl.FormTemplate("test", (), {"01": "Ett", "02": "Två", "03": "Tre"})
    rest, fields = fl.extract_form_fields(blocks, [tpl], page_height=3500)
    assert [f.value for f in fields] == ["", "", ""]
    assert [b.text for b in rest if b.fields is None] == ["långt bort", long_value, "vänster om etiketten"]


# --- Inget får försvinna -----------------------------------------------------------


class _Lines(HTMLParser):
    """Synlig text med radbrytning för <br/>, block och celler (oberoende av modulen)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "input" and dict(attrs).get("type") == "checkbox":
            self.out.append("☒" if any(k == "checked" for k, _ in attrs) else "☐")
        elif tag in ("br", "p", "td", "th", "tr", "div", "li"):
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def _lines(html: str) -> list[str]:
    p = _Lines()
    p.feed(html)
    p.close()
    return [ln.strip() for ln in "".join(p.out).split("\n") if ln.strip()]


def _form_numbers(blocks) -> dict[str, str]:
    """Fältnummer -> etikett för sidans numrerade fält."""
    return {f.number: f.label for b in blocks if b.fields for f in b.fields if f.number and f.checked is None}


def _like(a: str, b: str) -> bool:
    """Oberoende helradslikhet för testet (gemener, utan * och avslutande parentes,
    | läst som I); korta etiketter kräver 0,8."""
    def n(t):
        t = re.sub(r"\s*\([^()]*\)\s*$", "", t.replace("*", ""))
        return " ".join(t.replace("|", "i").lower().split())

    a, b = n(a), n(b)
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= (0.8 if len(b) < 8 else 0.6)


def _inner_label(rest: str, first: str, numbers: dict[str, str]) -> tuple[str, int] | None:
    """Beslut BJ: en etikettrad med två fältnummer (``03 Uppgiftsmottagare 04 telefon``).
    Svaret är det inre numret och dess position i ``rest`` när båda halvorna liknar
    sina fälts etiketter, annars ``None``."""
    for m in re.finditer(r"(?<=\s)(\d{2})(?=\s+\S)", rest):
        n2 = m.group(1)
        if n2 in numbers and int(n2) > int(first) and _like(rest[: m.start()], numbers[first]) and _like(
            rest[m.end():], numbers[n2]
        ):
            return n2, m.start()
    return None


def _consumed_label_line(line: str, next_line: str, numbers: dict[str, str]) -> bool:
    """En indatarad som faktiskt blivit en etikett: numret finns bland fälten och
    etikettexten (eller raden under ett ensamt nummer) liknar fältets etikett."""
    m = re.match(r"(\d{2})(?:\s+(\d{2}))?(?:\s+(.*))?$", line)
    if not m:
        return False
    first, second, rest = m.group(1), m.group(2), (m.group(3) or "")
    if second:
        return first in numbers and second in numbers and _like(rest, numbers[second])
    if first not in numbers:
        return False
    if _inner_label(rest, first, numbers) is not None:
        return True
    if rest:
        return _like(rest, numbers[first])
    return _like(next_line.replace("☐", "").replace("☒", ""), numbers[first])


def assert_conserved_page(items, image=None, masked_boxes=0):
    """Allt synligt finns kvar i ``page_text``.

    Tillåtna skillnader: ``[REDACTED]`` (alla skrivsätt) → ``[MASKAD]``, avstavningens ``-`` vid
    radbrott, ``masked_boxes`` kryssrutor som blivit ``[MASKAD]`` och tecken i
    rader som faktiskt blivit mallens fältetiketter (mallen rättar OCR-fel):
    raden börjar med ett fältnummer som finns bland sidans fält och står inte
    längre kvar som text.
    """
    blocks, _ = sl.parse_page(items, image=image)
    text = sl.page_text(blocks)
    flat_text = " ".join(text.split())
    numbers = _form_numbers(blocks)
    expected: Counter = Counter()
    allowed: Counter = Counter()
    for it in items:
        lines = [re.sub(r"\[\s*redacted\s*\]", "[MASKAD]", ln, flags=re.I) for ln in _lines(it["html"])]
        for i, ln in enumerate(lines):
            expected += Counter("".join(ln.split()))
            if _consumed_label_line(ln, lines[i + 1] if i + 1 < len(lines) else "", numbers) and (
                " ".join(ln.split()) not in flat_text
            ):
                allowed += Counter("".join(ln.split()))
                if re.fullmatch(r"\d{2}", ln) and i + 1 < len(lines):
                    allowed += Counter("".join(lines[i + 1].split()))
        allowed += Counter("-" * len(re.findall(r"-<br/>", it["html"])))
    allowed += Counter("☐" * masked_boxes)
    got = Counter("".join(text.split()))
    lost = expected - got
    assert not (lost - allowed), lost - allowed
    assert_markdown_conserved(blocks)
    assert_label_rests_in_markdown(items, blocks)


def assert_markdown_conserved(blocks):
    """Varje tecken i varje block som inte är fält eller sidfot finns i Markdown
    (Markdowns escape-snedstreck räknas inte)."""
    md = Counter("".join(sl.page_markdown(blocks).replace("\\", "").split()))
    need: Counter = Counter()
    for b in blocks:
        if b.fields is None and b.kind is not BlockKind.PAGE_FOOTER:
            need += Counter("".join(sl.page_text([b]).split()))
    assert not (need - md), need - md


def _words(text: str) -> list[str]:
    """Ord för jämförelsen: bokstavsföljder och sifferföljder var för sig, så att ett
    hopskrivet värde (``Avsnitt12``) räknas som två ord."""
    return re.findall(r"[^\W\d_]+|\d+", text.replace("\\", "").lower())


def _lower(text: str) -> str:
    return "".join((c.lower() or c)[0] for c in text)


def _cover(text: str, ref: str, offset: int = 0) -> tuple[set[int], set[int]]:
    """(lika, utbytta) positioner i ``text`` enligt en difflib-justering mot ``ref``;
    utbytta = ersättning av högst två tecken mot lika många (OCR-fel)."""
    equal: set[int] = set()
    swapped: set[int] = set()
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, _lower(ref), _lower(text), autojunk=False).get_opcodes():
        if op == "equal":
            equal.update(range(offset + j1, offset + j2))
        elif op == "replace" and i2 - i1 == j2 - j1 <= 2:
            swapped.update(range(offset + j1, offset + j2))
    return equal, swapped


def _template_words(text: str, canons: list[str], tpl) -> set[int]:
    """Startpositioner för ord på raden som mallen täcker (beslut BI): minst 80 % av
    ordets tecken täcks av justeringen mot mallens etikett eller mot den tryckta
    parentes en parentes på raden motsvarar (ord för ord), och minst hälften är lika
    tecken (eller ordet är ett tecken)."""
    def norm(t):
        return " ".join(t.lower().replace("*", "").split())

    refs = []
    if canons:
        refs.append((text, max(canons, key=lambda c: difflib.SequenceMatcher(None, norm(text), norm(c)).ratio()), 0))
    printed = [p for p in tpl.printed if p.startswith("(")]
    for m in re.finditer(r"\([^()]*\)?", text):
        scored = [(difflib.SequenceMatcher(None, norm(m.group(0)), norm(p), autojunk=False).ratio(), p) for p in printed]
        ratio, ref = max(scored, default=(0.0, ""))
        if ratio >= 0.85:
            refs.append((m.group(0), ref, m.start()))
    equal: set[int] = set()
    swapped: set[int] = set()
    for part, ref, offset in refs:
        e, sw = _cover(part, ref, offset)
        equal |= e
        swapped |= sw
    out = set()
    for m in re.finditer(r"[^\W\d_]+|\d+", text):
        span = range(m.start(), m.end())
        n_eq = sum(i in equal for i in span)
        n = n_eq + sum(i in swapped for i in span)
        if n >= 0.8 * len(span) and (2 * n_eq >= len(span) or len(span) == 1):
            out.add(m.start())
    return out


def assert_label_rests_in_markdown(items, blocks):
    """Beslut BC/BF/BG: inget på en godtagen etikettrad försvinner ur Markdown.

    Raderna tas ur ``parse_blocks`` (före blankettlagret; en tabellcell är en rad).
    Varje ord på en rad som blivit ett fälts etikett ska *synas*: i värdet (eller
    ett ikryssat alternativ), i sidans lösa Markdown-text, eller i etiketten – men
    etiketten räknas bara för fält som skrivs till Markdown (värde eller ikryssad
    ruta). Undantag är bara ord som justeringen mot mallens etikett eller tryckta
    parentes täcker (``_template_words``, samma regel som modulen: ≥ 80 % av
    tecknen, minst hälften lika). Inget prefix skalas av och ingen parentes stryks
    i sin helhet. Dessutom ska ren text (``page_text``) ha varje ord från alla
    etikettrader.
    """
    trace: dict = {}
    sl.parse_page(items, trace=trace)
    if trace["template"] is None:
        return
    tpl = {t.name: t for t in fl.load_templates()}[trace["template"]]
    md = sl.page_markdown(blocks)
    loose = Counter(_words(" ".join(p for p in md.split("\n\n") if not p.startswith("- **"))))
    by_number: dict[str, list] = {}
    for b in blocks:
        for f in b.fields or ():
            if f.number:
                by_number.setdefault(f.number, []).append(f)
    numbers = _form_numbers(blocks)
    raw_blocks, _ = sl.parse_blocks(items)
    lines = []
    for b in raw_blocks:
        if b.rows is not None:
            lines += [c.text for row in b.rows for c in row]
        else:
            lines += fl.dehyphenate(b.text).split("\n")  # avstavning slås ihop före tolkningen
    all_label_words: Counter = Counter()
    for line in lines:
        line = re.sub(r"\[\s*redacted\s*\]", "[MASKAD]", line, flags=re.I).strip()
        m = re.match(r"(\d{2})(?:\s+(\d{2}))?(?:\s+(.*))?$", line)
        if not m or not _consumed_label_line(line, "", numbers):
            continue
        nums = [n for n in (m.group(1), m.group(2)) if n]
        inner = None if m.group(2) else _inner_label(m.group(3) or "", m.group(1), numbers)
        if inner is not None:
            nums.append(inner[0])
        have = Counter(nums) + loose
        for n in nums:
            group = by_number.get(n, [])
            heads = [f for f in group if f.checked is None]
            rendered = any(f.value.strip() for f in heads) or any(f.checked for f in group)
            for f in group:
                have += Counter(_words(f.value))
                if rendered or f.checked:
                    have += Counter(_words(f.label))
        text = m.group(3) or ""

        def canons_of(numbers_):
            return [c for n in numbers_ for c in [tpl.labels.get(n, ""), *tpl.aliases.get(n, ())] if c]

        if inner is not None:
            # två etiketter på raden: varje halva jämförs med sitt eget fälts etikett
            cut = inner[1]
            covered = _template_words(text[:cut], canons_of(nums[:1]), tpl) | {
                cut + i for i in _template_words(text[cut:], canons_of(nums[1:]), tpl)
            }
        else:
            covered = _template_words(text, canons_of(nums), tpl)
        missing = []
        for w in re.finditer(r"[^\W\d_]+|\d+", text.replace("\\", "")):
            word = w.group(0).lower()
            if have[word] > 0:
                have[word] -= 1
            elif w.start() not in covered:
                missing.append(word)
        all_label_words += Counter(_words(line))
        assert not missing, (line, missing)
    # beslut BI: ren text behåller varje ord från alla etikettrader, vad Markdown än gör
    lost_text = all_label_words - Counter(_words(sl.page_text(blocks)))
    assert not lost_text, lost_text


@pytest.mark.parametrize("name", FIXTURES)
def test_real_fixtures_keep_text_through_parse_page(name):
    assert_conserved_page(load_fixture(name))
    blocks, _ = sl.parse_page(load_fixture(name))
    sl.page_markdown(blocks)
    assert sl.blocks_from_json(sl.blocks_to_json(blocks)) == blocks


def test_dialog_c_keeps_every_turn_in_order():
    # Piloten (2026-10-26): förhörssida med treställig talarmarkör (FHL) och en
    # maskering mitt i en replik; alla 15 repliker kontrollerades mot sidbilden.
    assert_conserved_page(load_fixture("forhor-dialog-c"))
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-c"))
    md = sl.page_markdown(blocks)
    speakers = re.findall(r"^\*\*([A-ZÅÄÖ]{1,3}):\*\*", md, re.M)
    assert speakers == ["DK", "FHL"] * 7 + ["DK"]
    assert "[MASKAD]" in md and "[REDACTED]" not in md


def test_dialog_b_with_image_keeps_text():
    filled = _page_image((2481, 3509), (620, 1430, 720, 1480))
    assert_conserved_page(load_fixture("forhor-dialog-b"), image=filled, masked_boxes=1)


def test_unparseable_form_keeps_all_text():
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>O1 Polisdistrikt</p>", (100, 200, 500, 240)),  # oläsligt fältnummer
        ("<p>Stockholm</p>", (110, 245, 400, 285)),
        ('<p>Uppgiftslämnare <input type="checkbox"/> okänd</p>', (100, 300, 900, 340)),
        ("<table><tr><td>4 Avsnitt</td><td>Löpande</td></tr><tr><td>H</td><td>9066</td></tr></table>", (1000, 300, 1500, 400)),
    )
    items[-1]["label"] = "Form"
    blocks, _ = sl.parse_blocks(items)
    rest, fields = fl.extract_form_fields(blocks, fl.load_templates(), page_height=3500)
    assert fields == []
    assert rest == blocks
    assert_conserved_page(items)
    page, _ = sl.parse_page(items)
    md = sl.page_markdown(page)
    for word in ("O1 Polisdistrikt", "Stockholm", "okänd", "4 Avsnitt", "9066"):
        assert word in md


# --- Förhör --------------------------------------------------------------------------


def test_split_turns():
    # [MASKAD] är talare bara först i texten och före versal
    assert fl.split_turns("F: Var stod du?\nM: Vid Dekorima.") == [("F", "Var stod du?"), ("M", "Vid Dekorima.")]
    assert fl.split_turns("[MASKAD] Ja.") == [("[MASKAD]", "Ja.")]
    assert fl.split_turns("M: Vid Dekorima.\n[MASKAD] Ja.") == [("M", "Vid Dekorima.\n[MASKAD] Ja.")]
    assert fl.split_turns("[MASKAD] har den 24 oktober") == [(None, "[MASKAD] har den 24 oktober")]
    assert fl.split_turns("Han sa till mig: kom hit.") == [(None, "Han sa till mig: kom hit.")]
    assert fl.split_turns("Fråga: Var?\nSvar: Där.") == [("Fråga", "Var?"), ("Svar", "Där.")]
    assert fl.split_turns("FL: Var?\nSVENSSON: Där borta,\nvid kiosken.") == [
        ("FL", "Var?"),
        ("SVENSSON", "Där borta,\nvid kiosken."),
    ]
    assert fl.split_turns("Inledning.\nF: Var?") == [(None, "Inledning."), ("F", "Var?")]
    assert fl.split_turns("[MASKAD] höres i anledning av", allow_masked=False) == [
        (None, "[MASKAD] höres i anledning av")
    ]


def test_interrogation_header_fields():
    assert fl.interrogation_header_fields("Förhörsledare: krinsp Sören Morberg.") == [
        FormField(None, "Förhörsledare", "krinsp Sören Morberg.", None)
    ]
    assert fl.interrogation_header_fields(
        "Förhörsledare: Krinsp Temmy Andersson.\nFörhörsvittne: Krinsp Ulf Norlin."
    ) == [
        FormField(None, "Förhörsledare", "Krinsp Temmy Andersson.", None),
        FormField(None, "Förhörsvittne", "Krinsp Ulf Norlin.", None),
    ]
    assert fl.interrogation_header_fields(
        "Förhöret hållet onsdagen den 16 juni\n1993, i RKP-A2:s lokaler"
    ) == [FormField(None, "Förhöret hållet", "onsdagen den 16 juni 1993, i RKP-A2:s lokaler", None)]
    assert fl.interrogation_header_fields("Protokoll fört vid förhör med polisinspektören Avsan") == [
        FormField(None, "Protokoll fört vid förhör med", "polisinspektören Avsan", None)
    ]
    assert fl.interrogation_header_fields("Avsan uppger att han är född i Sverige.") == []


def test_interrogation_header_in_page():
    blocks, _ = sl.parse_page(load_fixture("form-v2-a"))
    md = sl.page_markdown(blocks)
    assert "- **Förhörsledare:** krinsp Sören Morberg." in md
    assert "- **Förhörsvittne:** krinsp Egon Berger." in md
    assert "- **Protokoll fört vid förhör med:** polisinspektören Avsan, Anti Harald, f.d." in md
    assert "Förhörsledare: krinsp Sören Morberg." in sl.page_text(blocks)


def test_markdown_turns_and_fields():
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-a"))
    md = sl.page_markdown(blocks)
    assert "**F:** Den här bilden, i vilken tidning var det du såg den?" in md.split("\n\n")
    assert "**M:** Ja." in md.split("\n\n")
    assert "F: Den här bilden" in sl.page_text(blocks)  # ren text oförändrad

    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    md = sl.page_markdown(blocks)
    assert "- **03 Uppgiftsmottagare:** Insp NEK Persson" in md.split("\n")
    assert "  - [x] per telefon" in md.split("\n")

    # maskerad talare bara på en sida med dialog; prosa med [MASKAD] först lämnas
    blocks, _ = sl.parse_page(load_fixture("form-v2-b"))
    assert "**[MASKAD]**" not in sl.page_markdown(blocks)
    filled = _page_image((2481, 3509))
    filled[1640:1680, 385:430] = 0  # "☐ Ja, det har jag hört." [382, 1628, 982, 1684]
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-b"), image=filled)
    assert "**[MASKAD]** Ja, det har jag hört." in sl.page_markdown(blocks).split("\n\n")


def test_parse_blocks_unchanged_by_form_layer():
    blocks, _ = sl.parse_blocks(load_fixture("form-v1-a"))
    assert not any(b.fields for b in blocks)
    assert "[REDACTED]" in sl.page_text(blocks)


# --- Granskningsrunda 1 -----------------------------------------------------------------


def _replace_html(items, start, html):
    hits = [it for it in items if it["html"].startswith(start)]
    assert len(hits) == 1, start
    hits[0]["html"] = html
    return hits[0]


def _md_paragraphs(blocks):
    return sl.page_markdown(blocks).split("\n\n")


@pytest.mark.parametrize(
    "fixture,start",
    [
        ("forhor-dialog-a", "<p>F: Ja, det har du gjort."),
        ("form-v2-a", "<p>Tjänstemässigt"),
    ],
)
def test_prose_starting_with_number_is_not_a_label(fixture, start):
    items = load_fixture(fixture)
    _replace_html(items, start, "<p>15 minuter senare ringde han igen och sa att han skulle komma.</p>")
    blocks, _ = sl.parse_page(items)
    assert "15" not in _form_numbers(blocks)
    text = sl.page_text(blocks)
    assert "15 minuter senare ringde han igen och sa att han skulle komma." in text.split("\n")
    assert "15 minuter senare ringde han igen och sa att han skulle komma." in _md_paragraphs(blocks)
    if fixture == "forhor-dialog-a":
        # läsordningen och följande repliker är orörda
        paras = _md_paragraphs(blocks)
        k = paras.index("15 minuter senare ringde han igen och sa att han skulle komma.")
        assert paras[k - 1] == "**M:** Ja, det sa jag redan nån...."
        assert paras[k + 1] == "**M:** Ja."
        assert paras[k + 2].startswith("**F:** Sen var det det här med ögonen.")
    assert_conserved_page(items)


def test_date_line_inside_br_paragraph_is_not_a_label():
    items = load_fixture("forhor-dialog-a")
    _replace_html(
        items,
        "<p>F: Den här bilden",
        "<p>F: Vad gjorde du på kvällen den<br/>28 februari 1986 när du kom hem?<br/>Berätta.</p>",
    )
    blocks, _ = sl.parse_page(items)
    assert "28" not in _form_numbers(blocks)
    assert "F: Vad gjorde du på kvällen den\n28 februari 1986 när du kom hem?\nBerätta." in sl.page_text(blocks)
    paras = _md_paragraphs(blocks)
    assert "**F:** Vad gjorde du på kvällen den\n28 februari 1986 när du kom hem?\nBerätta." in paras
    k = [i for i, p in enumerate(paras) if "kvällen den" in p][0]
    assert paras[k + 1].startswith("**M:** Aftonbladet.")
    assert_conserved_page(items)


def test_masked_speaker_only_at_block_start():
    items = load_fixture("forhor-dialog-b")
    img = _page_image((2481, 3509))
    for it in items:
        if "säger vidare" in it["html"] or "24 oktober" in it["html"]:
            x0, y0, _, _ = it["bbox"]
            for k in range(it["html"].count("checkbox")):
                img[y0 + 5 : y0 + 45, x0 + 10 + k * 120 : x0 + 90 + k * 120] = 0
    img[1640:1680, 385:430] = 0  # "☐ Ja, det har jag hört."
    blocks, _ = sl.parse_page(items, image=img)
    paras = _md_paragraphs(blocks)
    assert "[MASKAD] Han säger vidare att [MASKAD]\n[MASKAD] men han är dock lite osäker på den uppgiften." in paras
    assert [p for p in paras if "24 oktober" in p][0].startswith("[MASKAD] har den 24 oktober 1989")
    assert "**[MASKAD]** Ja, det har jag hört." in paras
    assert_conserved_page(items, image=img, masked_boxes=6)


def test_printed_captions_are_not_values():
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    fields = form_fields(blocks)
    assert field(fields, "20").value == ""
    assert field(fields, "22").value == ""
    opts = [(f.label, f.checked) for f in fields if f.number == "11" and f.checked is not None]
    assert opts == [("vid person-ligt besök", False), ("per telefon", True), ("per brev", False)]
    text = sl.page_text(blocks)
    for printed in ("Med feta linjer avgränsade avsnitt ifylls ej av uppgiftsmottagaren", "Spaningschef", "F-år"):
        assert printed in text
    md = sl.page_markdown(blocks)
    assert "Med feta linjer avgränsade avsnitt ifylls ej av uppgiftsmottagaren" in md.split("\n\n")
    assert "- **22 Registrerat" not in md  # tomt fält
    # beslut BC: "Signatur" efter etiketten på raden "22 Registrerat: Signatur" är fast
    # tryck, alltså inget värde – men den står kvar som text i stället för att försvinna
    assert field(fields, "22").label == "Registrerat"
    assert "Signatur" in md.split("\n\n")
    assert not any(f.label.endswith(":") for f in fields)


def test_two_numbers_on_one_line_are_two_labels():
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-b"))
    fields = form_fields(blocks)
    assert field(fields, "05").label == "Förhörsprot"
    assert field(fields, "06").label == "Utskriftsdatum"
    assert [f.label for f in fields if f.number == "05" and f.checked is not None] == ["Förhörsprot", "Promemoria"]


def test_dehyphenate_table_cells_and_values():
    blocks, _ = sl.parse_page(load_fixture("form-v2-a"))
    tables = [b for b in blocks if b.rows is not None]
    assert tables and tables[0].rows[0][1].text == "Fortsättningsblad"
    assert "Fortsättningsblad" in sl.page_markdown(blocks)
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt</p>", (100, 200, 500, 240)),
        ("<p>Stock-<br/>holm, sprit- eller</p>", (110, 245, 400, 285)),
        ("<p>02 Arbetsenhet</p>", (100, 300, 500, 340)),
        ("<p>03 Uppgiftsmottagare</p>", (100, 400, 500, 440)),
    )
    blocks, _ = sl.parse_page(items)
    assert form_fields(blocks)[0] == FormField("01", "Polisdistrikt", "Stockholm, sprit- eller", None)


# --- Granskningsrunda 2 -----------------------------------------------------------------

_DIALOG_A_PROBES = [
    ("<p>10 30 kom han tillbaka och sa att han skulle komma.</p>", "10 30 kom han tillbaka och sa att han skulle komma."),
    ("<p>05 06 var klockan när han ringde.</p>", "05 06 var klockan när han ringde."),
    ("<p>10 sakerna han nämnde var viktiga för utredningen.</p>", "10 sakerna han nämnde var viktiga för utredningen."),
    ("<p>Han kom klockan<br/>10<br/>och gick sen.</p>", "Han kom klockan\n10\noch gick sen."),
    ("<p>15<br/>minuter senare kom han.</p>", "15\nminuter senare kom han."),
]


@pytest.mark.parametrize("html,text", _DIALOG_A_PROBES)
def test_numbered_prose_lines_stay_text(html, text):
    items = load_fixture("forhor-dialog-a")
    _replace_html(items, "<p>F: Ja, det har du gjort.", html)
    blocks, _ = sl.parse_page(items)
    fields = form_fields(blocks)
    assert all(f.value == "" for f in fields if f.checked is None)  # inget fält svalde dialogen
    assert text in sl.page_text(blocks)
    paras = _md_paragraphs(blocks)
    assert text in paras
    k = paras.index(text)
    assert paras[k - 1] == "**M:** Ja, det sa jag redan nån...."
    assert paras[k + 1] == "**M:** Ja."
    assert paras[k + 2].startswith("**F:** Sen var det det här med ögonen.")
    assert_conserved_page(items)


def test_dialog_a_bare_numbers_take_label_from_line_below():
    blocks, _ = sl.parse_page(load_fixture("forhor-dialog-a"))
    fields = form_fields(blocks)
    assert field(fields, "05").label == "Förhörsprot"
    # "Utskriftsdatum" bredvid 06 på samma rad är mallens alias och en egen etikett (beslut BJ)
    assert [f.label for f in _heads(fields, "06")] == ["Promemoria", "Utskriftsdatum"]


@pytest.mark.parametrize("shift", [0, 5, 10, 30])
def test_rejected_template_number_is_a_barrier(shift):
    # ett avvisat mallnummer är en spärr: "10 dak" godtas inte som etikett men stoppar ändå fält 09:s värdekedja
    items = load_fixture("forhor-dialog-b")
    moved = [it for it in items if it["html"].startswith("<p>Vidare erinrar")]
    assert len(moved) == 1
    x0, y0, x1, y1 = moved[0]["bbox"]
    moved[0]["bbox"] = [x0, y0 - shift, x1, y1 - shift]
    blocks, _ = sl.parse_page(items, page_height=3509)
    assert field(form_fields(blocks), "09").value == ""
    assert "Vidare erinrar sig" in sl.page_text(blocks).split("\n")
    assert_conserved_page(items)


def test_bare_number_label_ignores_margin_stamp():
    # "21" står ensamt; marginalstämpeln "RPS 463.1.2." börjar till vänster och är ingen etikett
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    fields = form_fields(blocks)
    assert field(fields, "21").label == "Anknyter till uppslag nr"
    assert option(fields, "21", "anknyter till uppslag nr").checked is False
    assert "RPS 463.1.2." in sl.page_text(blocks).split("\n")


# --- Granskningsrunda 3 -----------------------------------------------------------------

_ROUND3 = [
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>08 Löpande räkning visade att han betalat.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>07 avsnitt i bandet saknas, sa han.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>F: Och sen?<br/>08 Löpande nummer stod på lappen.<br/>M: Ja.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>05 06 Utskriftsdatum minns jag inte.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>Han kom klockan<br/>07<br/>Avsnittet han såg var kort.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>10 i saken har jag inget mer att säga.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>09 brott med vapen hade han aldrig begått.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>14 15 kom han hem.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>28 02 1986 var han i stan.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>02 03 1986 var han i stan.</p>"),
    ("forhor-dialog-a", "<p>F: Ja, det har du gjort.", "<p>12 st patroner hittades.</p>"),
    ("form-v1-a", "<p><b>uppger att han har hört talas om en person,</b></p>", "<p>06 klockan ringde han till Götesson.</p>"),
    ("form-v1-a", "<p><b>uppger att han har hört talas om en person,</b></p>", "<p>05 datum minns han inte.</p>"),
    ("form-v1-a", "<p><b>uppger att han har hört talas om en person,</b></p>", "<p>04 telefonsamtal kom in samma dag.</p>"),
    ("form-v1-a", "<p><b>uppger att han har hört talas om en person,</b></p>", "<p>14 15 arbetsplatsen var stängd då.</p>"),
    ("form-v1-a", "<p><b>uppger att han har hört talas om en person,</b></p>", "<p>28 02 1986 var han i Stockholm.</p>"),
]


_V1_BODY = "<p><b>uppger att han har hört talas om en person,</b></p>"
_DIALOG_A_BODY = "<p>F: Ja, det har du gjort."
# Granskningsrunda 4: numrerad prosa mellan fält 18 och sidfotsbandet
_ROUND4 = [
    *(
        ("form-v1-a", _V1_BODY, h)
        for h in (
            # tidigare godtagna som sidfotsetiketter
            "<p>20 Tagit del</p>",
            "<p>20<br/>Tagit del</p>",
            "<p>20 tagit del av</p>",
            "<p>21 anknyter till uppslag</p>",
            "<p>22 registrerade vapen</p>",
            "<p>23 ärendet tilldelades</p>",
            "<p>23 Ärendet tilldelat</p>",
            "<p>24 färdigbearbetat datum</p>",
            "<p>25 Spaningschefen sa</p>",
            # redan säkra, ska förbli det
            "<p>19 personer var där.</p>",
            "<p>20 Tagit del av handlingarna har han inte.</p>",
            "<p>22 registrerade vapen fanns.</p>",
            "<p>07 Avsnitt</p>",
            "<p>Avsnitt</p>",
            "<p>19</p>",
        )
    ),
    *(
        ("forhor-dialog-a", _DIALOG_A_BODY, h)
        for h in (
            "<p>07 Avsnitt</p>",
            "<p>Avsnitt</p>",
            "<p>M: 07 Avsnitt</p>",
            "<p>08 Löpande nr</p>",
            "<p>10 I sak</p>",
            "<p>11 personer var där.</p>",
            "<p>07<br/>Avsnitt</p>",
        )
    ),
]


@pytest.mark.parametrize("fixture,start,html", _ROUND3 + _ROUND4)
def test_label_like_prose_stays_text(fixture, start, html):
    reference, _ = sl.parse_page(load_fixture(fixture))
    expected = {(f.number, f.label, f.value, f.checked) for f in form_fields(reference)}
    items = load_fixture(fixture)
    _replace_html(items, start, html)
    blocks, _ = sl.parse_page(items)
    # fälten är exakt desamma som utan den inklistrade raden
    assert {(f.number, f.label, f.value, f.checked) for f in form_fields(blocks)} == expected
    lines = [ln for ln in re.sub(r"<[^>]+>", "\n", html).split("\n") if ln.strip()]
    text = sl.page_text(blocks)
    md = sl.page_markdown(blocks).replace("**", "")  # talarmarkörens fetstil räknas inte
    for ln in lines:
        assert ln in text
        assert ln in md
    paras = _md_paragraphs(blocks)
    if fixture == "forhor-dialog-a":
        # läsordningen: den inklistrade texten står mellan sina grannrepliker, som är orörda
        before = paras.index("**M:** Ja, det sa jag redan nån....")
        after = [i for i, p in enumerate(paras) if p.startswith("**F:** Sen var det det här med ögonen.")][0]
        assert paras[after - 1] == "**M:** Ja."
        middle = "\n".join(paras[before + 1 : after - 1]).replace("**", "")
        assert all(ln in middle for ln in lines)
    else:
        k = paras.index("\n".join(lines))
        assert paras[k - 1] == "Tipset inringt från vh. befäl Jönköpingspolisen, Götesson,."
        assert paras[k + 1] == "[MASKAD]"
    assert_conserved_page(items)


def test_free_text_cutoff_and_value_limits():
    # under fritextfältet 18 godtas inga etiketter 01–18, och en kedja korsar inte 18
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    fields = form_fields(blocks)
    assert field(fields, "19", "Ovanstående uppslag")
    assert field(fields, "17").value == ""
    # en replik stoppar värdekedjan och blir aldrig ett värde
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt</p>", (100, 200, 500, 240)),
        ("<p>Stockholm</p>", (110, 245, 400, 285)),
        ("<p>F: Var var du?</p>", (110, 290, 400, 330)),
        ("<p>Hemma</p>", (110, 335, 400, 375)),
        ("<p>02 Arbetsenhet</p>", (1000, 200, 1300, 240)),
        ("<p>a</p>", (1010, 245, 1300, 285)),
        ("<p>b</p>", (1010, 290, 1300, 330)),
        ("<p>c</p>", (1010, 335, 1300, 375)),
        ("<p>d</p>", (1010, 380, 1300, 420)),
        ("<p>03 Upprättat(d) av</p>", (1800, 200, 2100, 240)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "01").value == "Stockholm"
    assert field(fields, "02").value == "a b c"
    texts = sl.page_text(blocks).split("\n")
    assert "F: Var var du?" in texts and "Hemma" in texts and "d" in texts


def test_whole_line_label_match():
    v1 = {t.name: t for t in fl.load_templates()}["spaningsuppslag-1"]
    assert fl._accepts(v1, "08", "Löpande nr")
    assert fl._accepts(v1, "01", "Polisdistrikt*")
    assert fl._accepts(v1, "01", "Poliodistrikt (där uppslaget mottagits)")
    assert fl._accepts(v1, "18", "I sak. (anteckna först känd persons yrke)")
    assert not fl._accepts(v1, "08", "Löpande räkning visade att han betalat.")
    assert not fl._accepts(v1, "06", "klockan ringde han till Götesson.")
    assert not fl._accepts(v1, "05", "datum minns han inte.")
    assert not fl._accepts(v1, "05", "data")  # kvot 0,67: räcker för långa etiketter, inte för korta (0,8)


def _v1_page(*extra):
    """En v1-sida med tre godtagna etiketter (mallen gäller) plus ``extra``."""
    return _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 200, 700, 240)),
        ("<p>Stockholm</p>", (110, 245, 400, 285)),
        ("<p>02 Arbetsenhet</p>", (100, 300, 500, 340)),
        ("<p>KCV</p>", (110, 345, 400, 385)),
        ("<p>10 Brott med vilket uppslaget hör samman</p>", (100, 600, 900, 640)),
        ("<p>Mordet på Olof Palme</p>", (110, 645, 700, 685)),
        *extra,
    )


def test_value_on_label_line_becomes_value():
    # beslut BC: "07 Avsnitt E" – värdet står på etikettraden och inget under den
    items = _v1_page(
        ("<p>07 Avsnitt E</p>", (1800, 200, 2100, 240)),
        ("<p>08 Löpande nr 1974-B</p>", (2150, 200, 2400, 240)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "07", "Avsnitt").value == "E"
    assert field(fields, "08", "Löpande nr").value == "1974-B"
    md = sl.page_markdown(blocks)
    assert "- **07 Avsnitt:** E" in md and "- **08 Löpande nr:** 1974-B" in md
    assert_conserved_page(items)


def test_label_line_rest_kept_as_text_when_not_a_value():
    items = _v1_page(
        # värde finns redan under etiketten: resten står kvar som text
        ("<p>07 Avsnitt E</p>", (1800, 200, 2100, 240)),
        ("<p>F</p>", (1810, 245, 2000, 285)),
        # ett fältnummer är inget värde ("04 telefon" vore en egen etikett, beslut BJ,
        # så resten här är ett fältnummer vars text inte är mallens etikett)
        ("<p>03 Uppgiftsmottagare 04 sekunder</p>", (100, 400, 900, 440)),
        ("<p>Krinsp A Lindahl</p>", (110, 445, 600, 485)),
        # fast tryck ("Signatur") är inget värde
        ("<p>22 Registrerat: Signatur</p>", (1800, 3000, 2100, 3040)),
        ("<p>23 Ärendet tilldelat</p>", (100, 3000, 500, 3040)),
        ("<p>24 Färdigbearbetat datum</p>", (600, 3000, 1000, 3040)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "07", "Avsnitt").value == "F"
    assert field(fields, "03", "Uppgiftsmottagare").value == "Krinsp A Lindahl"
    assert field(fields, "22", "Registrerat").value == ""
    paragraphs = _md_paragraphs(blocks)
    for rest in ("E", "04 sekunder", "Signatur"):
        assert rest in paragraphs, rest
    assert_conserved_page(items)


@pytest.mark.parametrize("length,is_value", [(60, True), (61, False)])
def test_label_line_rest_length_limit(length, is_value):
    # bara en lång malletikett godtar en så lång rad (hela raden ska likna etiketten)
    label = "Lång etikett " * 10
    rest = "v" * length
    items = _page(
        ("<p>01 Ett</p>", (100, 100, 500, 140)),
        ("<p>02 Två</p>", (100, 300, 500, 340)),
        (f"<p>03 {label.strip()} {rest}</p>", (100, 500, 2400, 540)),
    )
    blocks, _ = sl.parse_blocks(items)
    tpl = fl.FormTemplate("test", (), {"01": "Ett", "02": "Två", "03": label.strip()})
    out, fields = fl.extract_form_fields(blocks, [tpl], page_height=3500)
    assert [f.value for f in fields if f.number == "03"] == [rest if is_value else ""]
    assert ([b.text for b in out if b.fields is None] == [rest]) is not is_value


def test_label_line_rest_in_table_cell():
    # sida 5 i piloten: "07 Avsnitt<br/><b>E</b>" i en tabellcell
    table = (
        "<table><tr><td>07 Avsnitt<br/><b>E</b></td><td>08 Löpande nr<br/>1974-B</td></tr>"
        "<tr><td>09 Plats för förtursstämpel<br/>EAC 1580 B</td></tr></table>"
    )
    items = _v1_page((table, (1800, 200, 2400, 500)))
    items[-1]["label"] = "Form"
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert [(field(fields, n).label, field(fields, n).value) for n in ("07", "08", "09")] == [
        ("Avsnitt", "E"), ("Löpande nr", "1974-B"), ("Plats för förtursstämpel", "EAC 1580 B"),
    ]
    assert_conserved_page(items)


@pytest.mark.parametrize(
    "line,bbox,number,label,value",
    [
        # granskarens prober (beslut BF): hopskrivet värde och förkortad etikett
        ("07 Avsnitt12", (1800, 200, 2100, 240), "07", "Avsnitt", "12"),
        ("08 Löpande nr1974-B", (2150, 200, 2400, 240), "08", "Löpande nr", "1974-B"),
        ("12 Uppgiftslämnarens efternamn Andersson", (100, 400, 1500, 440), "12",
         "Uppgiftslämnarens efternamn och alla förnamn", "Andersson"),
        # inledande tryckt parentes hör till etiketten, inte till värdet
        ("15 Arbetsplats (arbetsgivare, postadress) Volvo AB", (100, 450, 1500, 490), "15",
         "Arbetsplats (arbetsgivare, postadress)", "Volvo AB"),
        ("12 Uppgiftslämnarens efternamn och alla förnamn (i rätt följd, tilltalsnamnet markeras) "
         "Andersson Karl", (100, 400, 2400, 440), "12",
         "Uppgiftslämnarens efternamn och alla förnamn (i rätt följd, tilltalsnamnet markeras)",
         "Andersson Karl"),
    ],
)
def test_label_line_tail_is_never_swallowed(line, bbox, number, label, value):
    items = _v1_page((f"<p>{line}</p>", bbox))
    blocks, _ = sl.parse_page(items, page_height=3500)
    f = field(form_fields(blocks), number)
    assert (f.label, f.value) == (label, value)
    assert f"- **{number} {label}:** {value}" in sl.page_markdown(blocks).split("\n")
    assert_conserved_page(items)


@pytest.mark.parametrize(
    "line,bbox,number,loose",
    [
        # granskarens prober (beslut BG): en parentes som inte är mallens tryck, i ett tomt fält
        ("15 Arbetsplats (Volvo AB)", (100, 450, 1500, 490), "15", "Volvo AB"),
        ("12 Uppgiftslämnarens efternamn och alla förnamn (Andersson Karl)", (100, 400, 1500, 440), "12",
         "Andersson Karl"),
        # ord inne i etiketten som mallen annars rättat bort
        ("12 Uppgiftslämnarens efternamn qzx alla förnamn", (100, 400, 1500, 440), "12", "qzx"),
        ("12 Uppgiftslämnarens Karlsson och alla förnamn", (100, 400, 1500, 440), "12", "Karlsson"),
        ("03 Uppgifts Borg mottagare", (100, 400, 900, 440), "03", "Borg"),
    ],
)
def test_unrendered_label_words_become_loose_text(line, bbox, number, loose):
    items = _v1_page((f"<p>{line}</p>", bbox))
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert field(form_fields(blocks), number).value == ""  # tomt fält: skrivs inte till Markdown
    assert loose in _md_paragraphs(blocks)
    assert loose in sl.page_text(blocks).split("\n")
    assert_conserved_page(items)


# granskarens prober (beslut BI): (rad, ord som måste synas i Markdown och ren text)
_BI_PROBES = [
    # förut tappade: inskjutna ord i en parentes som liknar mallens tryck, och ett ord
    # som liknar ett etikettord
    ("15 Arbetsplats (arbetsgivare, postadress och telefon arbetet Volvo)", ["Volvo"]),
    ("15 Arbetsplats (arbetsgivare, postadress och Volvo arbetet)", ["Volvo"]),
    ("15 Arbetsplats (arbetsgivare postadress och telefon arbetsgivaren)", ["arbetsgivaren"]),
    ("17 Händelse (kort rubricering t ex iakttagelse av person, fordon Nilsson, gods, skottlossning etc "
     "med angivande av tid och plats)", ["Nilsson"]),
    ("12 Uppgiftslämnarens Eftersson efternamn och alla förnamn", ["Eftersson"]),
    # fungerade redan och ska fortsätta göra det
    ("15 Arbetsplats (arbetsgivare, postadress och telefon arbetet) Volvo", ["Volvo"]),
    ("15 Arbetsplats (arbetsgivare, postadress och telefon arbetet) Volvo Persson", ["Volvo", "Persson"]),
    ("03 Uppgiftsmottagaren Persson", ["Persson"]),
    ("03 Uppgiftsmottagare Persson", ["Persson"]),
    ("03 Uppgifts Persson mottagare", ["Persson"]),
    ("03 Uppgiftsmottagare Uppgiftsmottagarn", ["Uppgiftsmottagarn"]),
    ("03 Persson Uppgiftsmottagare", ["Persson"]),
    ("20 Tagit Dell del", ["Dell"]),
    ("20 Tagit Dell", ["Dell"]),
    ("20 Tagit Karl del Dell", ["Dell", "Karl"]),
    ("20 Dell Tagit del", ["Dell"]),
    ("20 Tagit Dell del Karl", ["Dell"]),
    ("13 Yrke Titelius/titel", ["Titelius"]),
    ("12 Uppgiftslämnarens Karlsson efternamn och alla förnamn", ["Karlsson"]),
    ("15 Arbetsplats (Volvo AB) (arbetsgivare, postadress och telefon arbetet)", ["Volvo"]),
    ("15 Arbetsplats (arbetsgivare, postadress och telefon arbetet) (Volvo AB)", ["Volvo"]),
    ("12 Uppgiftslämnarens efternamn och alla förnamn (i rätt följd, tilltalsnamnet markeras) Berg Anna",
     ["Berg", "Anna"]),
    ("14 Postadress Gatan (utdelningsadress och ortsadress), telefon bostaden", ["Gatan"]),
    ("15 Arbetsplatsen Volvo", ["Volvo"]),
]


@pytest.mark.parametrize("line,names", _BI_PROBES)
def test_inserted_words_on_label_line_are_never_dropped(line, names):
    items = _v1_page((f"<p>{line}</p>", (100, 450, 1500, 490)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    md, text = sl.page_markdown(blocks), sl.page_text(blocks)
    for name in names:
        assert name in md and name in text, name
    assert_conserved_page(items)


def test_label_rest_check_uses_alignment_not_similarity():
    # kontrollen: ett ord som bara liknar ett etikettord ("Eftersson" ~ "efternamn")
    # räknas inte som etikett, och ren text måste ha den råa raden
    import dataclasses

    items = _v1_page(("<p>12 Uppgiftslämnarens Eftersson efternamn och alla förnamn</p>", (100, 450, 1500, 490)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    without_loose = [b for b in blocks if b.text != "Eftersson"]
    with pytest.raises(AssertionError, match="eftersson"):
        assert_label_rests_in_markdown(items, without_loose)
    # ett OCR-stavat etikettord får saknas i Markdown men aldrig i ren text
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 200, 1700, 260)),
        ("<p>01 Poliodistrikt (där uppslaget mottagits)</p>", (290, 193, 826, 239)),
        ("<p><b>Stockholm</b></p>", (310, 240, 568, 284)),
        *_OTHER_LABELS,
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert_label_rests_in_markdown(items, blocks)
    without_source = [
        dataclasses.replace(b, fields=tuple(dataclasses.replace(f, source="") for f in b.fields))
        if b.fields else b
        for b in blocks
    ]
    with pytest.raises(AssertionError, match="poliodistrikt"):
        assert_label_rests_in_markdown(items, without_source)


def test_page_text_keeps_raw_label_line():
    # beslut BI: ren text behåller etikettradens råa OCR-text när mallen skrivit om den
    items = _v1_page(("<p>12 Uppgiftslämnarens Eftersson efternamn och alla förnamn</p>", (100, 450, 1500, 490)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    lines = sl.page_text(blocks).split("\n")
    assert "12 Uppgiftslämnarens Eftersson efternamn och alla förnamn" in lines
    assert "12 Uppgiftslämnarens efternamn och alla förnamn: " in lines
    assert "Eftersson efternamn" not in sl.page_markdown(blocks)


def test_printed_parenthesis_is_not_loose_text():
    # mallens fasta tryck i parentes står kvar i etiketten och blir ingen lös text
    items = _v1_page(
        ("<p>15 Arbetsplats (arbetsgivare, postadress och telefon arbetet)</p>", (100, 450, 1500, 490)),
        ("<p>17 Händelse (kort rubricering t ex lakttagelse av person, fordon, gods, skottlossning etc "
         "med angivande av tid och plats)</p>", (100, 700, 2400, 740)),
    )
    trace: dict = {}
    blocks, _ = sl.parse_page(items, page_height=3500, trace=trace)
    assert trace["label_words_text"] == 0
    assert [t for t in _md_paragraphs(blocks) if not t.startswith("- **")] == ["SPANINGSUPPSLAG"]
    assert_conserved_page(items)


def test_label_rest_check_counts_label_only_for_rendered_fields():
    # kontrollen: ett ord som bara står i etiketten på ett tomt (oskrivet) fält räknas inte
    import dataclasses

    items = _v1_page(("<p>03 Uppgifts Borg mottagare</p>", (100, 400, 900, 440)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    hidden = [
        dataclasses.replace(
            b, fields=tuple(dataclasses.replace(f, label="Uppgiftsmottagare Borg") if f.number == "03" else f
                            for f in b.fields)
        ) if b.fields else b
        for b in blocks
        if b.text != "Borg"
    ]
    with pytest.raises(AssertionError, match="borg"):
        assert_label_rests_in_markdown(items, hidden)


def test_unrewritten_label_with_empty_field_stays_text():
    # beslut BF: "10 Brott med vilket Kalle" godtas (helradslikhet ≥ 0,6) men liknar
    # inte mallens etikett nog för att rättas (< 0,75). Etiketten står kvar som den
    # lästes och fältet är tomt – då får fältet inte vara textens enda bärare.
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 200, 700, 240)),
        ("<p>Stockholm</p>", (110, 245, 400, 285)),
        ("<p>02 Arbetsenhet</p>", (100, 300, 500, 340)),
        ("<p>KCV</p>", (110, 345, 400, 385)),
        ("<p>03 Uppgiftsmottagare</p>", (100, 400, 500, 440)),
        ("<p>Krinsp Borg</p>", (110, 445, 400, 485)),
        ("<p>10 Brott med vilket Kalle</p>", (100, 600, 900, 640)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    f = field(form_fields(blocks), "10")
    assert (f.label, f.value) == ("Brott med vilket Kalle", "")
    assert "10 Brott med vilket Kalle" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_label_rest_check_catches_a_lost_tail():
    # själva bevarandekontrollen: tappas värdet ur fältet (som före beslut BF) fälls den
    import dataclasses

    items = _v1_page(("<p>07 Avsnitt12</p>", (1800, 200, 2100, 240)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    lost = [
        dataclasses.replace(
            b, fields=tuple(dataclasses.replace(f, value="") if f.number == "07" else f for f in b.fields)
        ) if b.fields else b
        for b in blocks
    ]
    with pytest.raises(AssertionError, match="12"):
        assert_label_rests_in_markdown(items, lost)


def test_explanatory_parenthesis_stays_in_label():
    items = _v1_page(
        ("<p>12 Uppgiftslämnarens efternamn och alla förnamn (i rätt följd, tilltalsnamnet markeras)</p>",
         (100, 400, 1500, 440)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    f = field(form_fields(blocks), "12")
    assert f.label == "Uppgiftslämnarens efternamn och alla förnamn (i rätt följd, tilltalsnamnet markeras)"
    assert f.value == ""


@pytest.mark.parametrize(
    "line",
    ["07 avsnitt i bandet saknas, sa han.", "08 löpande räkning visade att han betalat."],
)
def test_prose_after_label_number_is_still_rejected(line):
    # beslut L: hela raden måste likna etiketten – kvarleveregeln ändrar inte det
    items = _v1_page((f"<p>{line}</p>", (100, 400, 1500, 440)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert not [f for f in fields if f.number in ("07", "08")]
    assert line in _md_paragraphs(blocks)


def test_pilot_forms_values_on_label_lines():
    # piloten (2026-10-26), sida 5 (form-v1-c): värdena i rutan för avsnitt, löpnummer
    # och förtursstämpel stod i etiketterna och saknades i Markdown (bilden: E / 1974-B /
    # EAC 1580 B). Sida 13 (form-v2-c): utskriftsdatumet stod i etiketten för 06.
    blocks, _ = sl.parse_page(load_fixture("form-v1-c"), page_height=3507)
    fields = form_fields(blocks)
    assert [(field(fields, n).label, field(fields, n).value) for n in ("03", "07", "08", "09")] == [
        ("Uppgiftsmottagare", "Krinsp A Lindahl"),
        ("Avsnitt", "E"),
        ("Löpande nr", "1974-B"),
        ("Plats för förtursstämpel", "EAC 1580 B"),
    ]
    md = sl.page_markdown(blocks)
    for line in ("- **07 Avsnitt:** E", "- **08 Löpande nr:** 1974-B", "- **09 Plats för förtursstämpel:** EAC 1580 B"):
        assert line in md.split("\n"), line
    # beslut BJ: "04 telefon" på 03:s etikettrad är nu en egen (tom) etikett, inte lös text;
    # raden finns kvar i ren text som fältraden "04 telefon: "
    assert field(fields, "04", "telefon").value == ""
    assert "04 telefon: " in sl.page_text(blocks).split("\n")

    blocks, _ = sl.parse_page(load_fixture("form-v2-c"), page_height=3509)
    fields = form_fields(blocks)
    assert field(fields, "06", "Utskriftsdatum").value == "1990-09-30 EÅ"
    assert field(fields, "03", "Upprättat(d) av").value == "Krinsp Per Gustavsson"
    assert field(fields, "04", "Fortsättningsblad").value == ""
    paragraphs = sl.page_markdown(blocks).split("\n\n")
    assert "telefon" in paragraphs and "Uppgiftslämnare (efternamn och förnamn)" in paragraphs


def test_pilot_form_without_label_rests_unchanged():
    # sida 1 i piloten (form-v1-d) har inga värden på etikettraderna: fälten är
    # desamma som före beslut BC
    fields = form_fields(sl.parse_page(load_fixture("form-v1-d"), page_height=3507)[0])
    got = {f.number: f.value for f in fields if f.checked is None and f.value}
    assert got == {
        "01": "Stockholm", "02": "KCV", "03": "Krinsp T Borg", "04": "4390", "05": "86-03-04",
        "06": "23.20", "08": "1094", "09": "EAD", "10": "Mordet på Olof Palme", "16": "person",
        "24": "B61112", "25": "A.T.",
        # beslut BJ: värdet i 17:s nedre halva och datumet till höger om "20 Tagit del"
        "17": "MISSTänkt person, iaktagelser av-.", "20": "860308 A.T.",
    }


def test_lower_number_far_below_higher_label_is_prose():
    # nummerordningen: "05 datum" långt under fält 09 är prosa, inte fält 05
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 200, 700, 240)),
        ("<p>02 Arbetsenhet</p>", (100, 300, 500, 340)),
        ("<p>09 Plats för förtursstämpel</p>", (1800, 300, 2200, 340)),
        ("<p>05 datum</p>", (100, 1200, 300, 1240)),
        ("<p>860316</p>", (110, 1245, 300, 1285)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert "05" not in _form_numbers(blocks)
    assert "05 datum\n860316" in sl.page_text(blocks) or (
        "05 datum" in sl.page_text(blocks).split("\n") and "860316" in sl.page_text(blocks).split("\n")
    )
    assert_conserved_page(items)


# --- Granskningsrunda 4 -----------------------------------------------------------------


def test_real_footer_values_survive_numbered_prose():
    # sidfoten: "23 Ärendet tilldelat" mitt i brödtexten tar inte sidfotens fält
    items = load_fixture("form-v1-a")
    _replace_html(items, _V1_BODY, "<p>23 Ärendet tilldelat</p>")
    blocks, _ = sl.parse_page(items)
    fields = form_fields(blocks)
    assert field(fields, "23").value == "Kormard"
    assert field(fields, "24").value == "860407"
    assert field(fields, "19", "Ovanstående uppslag")
    assert "[MASKAD]" in _md_paragraphs(blocks)


def test_order_resolution_never_rejects_real_fixture_labels(monkeypatch):
    # ordningsupplösningen får inte ta bort någon riktig etikett
    for name in FORM_FIXTURES:
        expected = form_fields(sl.parse_page(load_fixture(name))[0])
        with monkeypatch.context() as m:
            m.setattr(fl, "ORDER_MAX_RISE", 100.0)  # ingen inversion räknas
            m.setattr(fl, "FOOTER_MAX_RISE", 100.0)  # inte heller i sidfotsklustret
            unresolved = form_fields(sl.parse_page(load_fixture(name))[0])
        assert expected == unresolved, name


def test_stray_high_label_does_not_reject_consistent_sequence():
    # en ensam felplacerad etikett avvisas, inte hela den riktiga följden
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>10 Brott med vilket uppslaget hör samman</p>", (100, 200, 900, 240)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 800, 700, 840)),
        ("<p>Stockholm</p>", (110, 845, 400, 885)),
        ("<p>02 Arbetsenhet</p>", (100, 900, 500, 940)),
        ("<p>KCV</p>", (110, 945, 400, 985)),
        ("<p>03 Uppgiftsmottagare</p>", (100, 1000, 500, 1040)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    numbers = _form_numbers(blocks)
    assert sorted(numbers) == ["01", "02", "03"]
    fields = form_fields(blocks)
    assert field(fields, "01").value == "Stockholm"
    assert field(fields, "02").value == "KCV"
    assert "10 Brott med vilket uppslaget hör samman" in sl.page_text(blocks).split("\n")
    assert_conserved_page(items)


def _footer_page(labels):
    """Syntetisk variant 1-sida: tre huvudfält och ``labels`` = [(html, y)] med ett värde under."""
    rows = [
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 200, 700, 240)),
        ("<p>02 Arbetsenhet</p>", (100, 300, 500, 340)),
        ("<p>03 Uppgiftsmottagare</p>", (100, 400, 500, 440)),
    ]
    for k, (html, y) in enumerate(labels):
        x = 100 + k * 700
        rows += [(html, (x, y, x + 500, y + 40)), (f"<p>V{k}</p>", (x + 10, y + 45, x + 300, y + 85))]
    return _page(*rows)


def test_footer_labels_only_as_cluster():
    # sidfotsetiketter (över fritextnumret) godtas bara som ett kluster av
    # minst tre nummer inom 0,08 × sidhöjden från den lägsta kandidaten
    footer = [("<p>22 Registrerat</p>", 3000), ("<p>23 Ärendet tilldelat</p>", 3000), ("<p>24 Färdigbearbetat datum</p>", 3010)]
    blocks, _ = sl.parse_page(_footer_page(footer), page_height=3500)
    fields = form_fields(blocks)
    assert [field(fields, n).value for n in ("22", "23", "24")] == ["V0", "V1", "V2"]
    # bara två nummer: inget kluster, inga sidfotsfält
    blocks, _ = sl.parse_page(_footer_page(footer[:2]), page_height=3500)
    assert not {"22", "23"} & set(_form_numbers(blocks))
    assert "22 Registrerat" in sl.page_text(blocks).split("\n")
    # utspridda: bara den lägsta ligger i fönstret
    spread = [("<p>22 Registrerat</p>", 2000), ("<p>23 Ärendet tilldelat</p>", 2500), ("<p>24 Färdigbearbetat datum</p>", 3000)]
    blocks, _ = sl.parse_page(_footer_page(spread), page_height=3500)
    assert not {"22", "23", "24"} & set(_form_numbers(blocks))
    # strikt ordning i klustret: 24 mer än 0,01 × sidhöjden ovanför 22 avvisas
    inverted = [("<p>22 Registrerat</p>", 3050), ("<p>23 Ärendet tilldelat</p>", 3050), ("<p>24 Färdigbearbetat datum</p>", 2990),
                ("<p>25 Spaningschef sign</p>", 3050)]
    blocks, _ = sl.parse_page(_footer_page(inverted), page_height=3500)
    assert sorted(n for n in _form_numbers(blocks) if int(n) > 18) == ["22", "23", "25"]


def test_footer_value_not_long_or_turn():
    # ett sidfotsvärde är aldrig en replik eller längre än 60 tecken
    long_value = "Han sa att han hade sett mannen springa mot Tunnelgatan efter skotten."
    for value in (long_value, "F: Var var du?"):
        items = load_fixture("form-v1-a")
        _replace_html(items, "<p><b>Kormard", f"<p>{value}</p>")
        blocks, _ = sl.parse_page(items)
        fields = form_fields(blocks)
        assert field(fields, "23").value == ""
        assert field(fields, "24").value == "860407"
        assert value in sl.page_text(blocks).split("\n")
        assert_conserved_page(items)


_V1_AFTER = ". Han har tidigare"
_KALLE = "<p>Kalle Svensson ringde igen.</p>"


def _insert_after(items, inserts):
    k = [i for i, it in enumerate(items) if re.sub(r"<[^>]+>", "", it["html"]).startswith(_V1_AFTER)]
    assert len(k) == 1
    for n, (html, bbox) in enumerate(inserts):
        items.insert(k[0] + 1 + n, raw(html, "Text", 0, bbox))
    for n, it in enumerate(items):
        it["reading_order"] = n
    return items


_FOOTER_PROBE_LINES = [
    "<p>23 Ärendet tilldelat</p>",
    "<p>20 Tagit del</p>",
    "<p>24 färdigbearbetat datum</p>",
    "<p>25 Spaningschefen sa</p>",
    "<p>21 anknyter till uppslag</p>",
    "<p>22 registrerade vapen</p>",
    "<p>19 Ovanstående uppslag</p>",
    "<p>23 ärendet tilldelades</p>",
]
_ROUND5 = [
    *((f"band y={y}", None, [(h, (315, y, 1500, y + 45)), (_KALLE, (315, y + 50, 1500, y + 95))])
      for y in (2470, 2500, 2560, 2650, 2750, 2830) for h in _FOOTER_PROBE_LINES),
    *((f"nofooter y={y}", 2870, [(h, (315, y, 1500, y + 45)), (_KALLE, (315, y + 50, 1500, y + 95))])
      for y in (2560, 2900)
      for h in ("<p>20 Tagit del</p>", "<p>23 ärendet tilldelades</p>", "<p>25 Spaningschefen sa</p>", "<p>19 personer var där.</p>")),
    ("två strays 23+24", None, [("<p>23 ärendet tilldelades</p>", (315, 2560, 1500, 2605)), ("<p>Kalle ringde.</p>", (315, 2610, 1500, 2650)),
                                ("<p>24 färdigbearbetat datum</p>", (315, 2660, 1500, 2700)), ("<p>Olle sa nej.</p>", (315, 2705, 1500, 2745))]),
    ("två strays 25+25", None, [("<p>25 Spaningschefen sa</p>", (315, 2470, 1500, 2515)), ("<p>Kalle ringde.</p>", (315, 2520, 1500, 2560)),
                                ("<p>25 Spaningschefen sa</p>", (315, 2570, 1500, 2610)), ("<p>Olle sa nej.</p>", (315, 2615, 1500, 2655))]),
]


@pytest.mark.parametrize("name,drop_below,inserts", _ROUND5, ids=[f"{n}-{i[0][0]}" for n, _, i in _ROUND5])
def test_numbered_testimony_in_footer_band_stays_text(name, drop_below, inserts):
    def page():
        items = load_fixture("form-v1-a")
        return [it for it in items if drop_below is None or it["bbox"][1] < drop_below]

    reference = {(f.number, f.label, f.value, f.checked) for f in form_fields(sl.parse_page(page(), page_height=3281)[0])}
    items = _insert_after(page(), inserts)
    blocks, _ = sl.parse_page(items, page_height=3281)
    assert {(f.number, f.label, f.value, f.checked) for f in form_fields(blocks)} == reference
    lines = [ln for h, _ in inserts for ln in re.sub(r"<[^>]+>", "\n", h).split("\n") if ln.strip()]
    text = sl.page_text(blocks)
    md = sl.page_markdown(blocks)
    for ln in lines:
        assert ln in text.split("\n")
        assert ln in md.split("\n\n")
    # läsordningen: direkt efter stycket de sattes in efter
    paras = _md_paragraphs(blocks)
    k = [i for i, p in enumerate(paras) if p.startswith(_V1_AFTER)][0]
    assert paras[k + 1 : k + 1 + len(lines)] == lines
    if drop_below is None:
        fields = form_fields(blocks)
        assert field(fields, "23").value == "Kormard" and field(fields, "24").value == "860407"
    assert_conserved_page(items)


@pytest.mark.parametrize("with_03", [True, False])
def test_two_strays_above_real_labels_are_rejected(with_03):
    # vid lika många brott avvisas den etikett vars nummer ligger längst
    # från grannarnas ovanför och under, så den lokalt konsekutiva följden står kvar
    rows = [
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>10 Brott med vilket uppslaget hör samman</p>", (100, 200, 900, 240)),
        ("<p>09 Plats för förtursstämpel</p>", (100, 260, 900, 300)),
        ("<p>01 Polisdistrikt (där uppslaget mottagits)</p>", (100, 800, 700, 840)),
        ("<p>Stockholm</p>", (110, 845, 400, 885)),
        ("<p>02 Arbetsenhet</p>", (100, 900, 500, 940)),
        ("<p>KCV</p>", (110, 945, 400, 985)),
    ]
    if with_03:
        rows.append(("<p>03 Uppgiftsmottagare</p>", (100, 1000, 500, 1040)))
    items = _page(*rows)
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert sorted(_form_numbers(blocks)) == (["01", "02", "03"] if with_03 else ["01", "02"])
    fields = form_fields(blocks)
    assert field(fields, "01").value == "Stockholm" and field(fields, "02").value == "KCV"
    text = sl.page_text(blocks).split("\n")
    assert "10 Brott med vilket uppslaget hör samman" in text and "09 Plats för förtursstämpel" in text
    assert_conserved_page(items)


def test_real_v1_footer_unchanged():
    fields = form_fields(sl.parse_page(load_fixture("form-v1-a"))[0])
    footer = [(f.number, f.label, f.value, f.checked) for f in fields if int(f.number) > 18]
    assert footer == [
        ("19", "Ovanstående uppslag", "", None),
        ("19", "kan anknyta till uppslag nr", "", False),
        ("20", "Tagit del", "", None),
        ("21", "Anknyter till uppslag nr", "", None),
        ("21", "anknyter till uppslag nr", "", False),
        ("22", "Registrerat", "", None),  # "Signatur" står kvar som text (beslut BC)
        ("23", "Ärendet tilldelat", "Kormard", None),
        ("24", "Färdigbearbetat datum", "860407", None),
        ("25", "Spaningschef sign", "", None),
    ]


def test_v1_field_14_uses_printed_label():
    v1 = {t.name: t for t in fl.load_templates()}["spaningsuppslag-1"]
    assert v1.labels["14"] == "Postadress (utdelningsadress och ortsadress), telefon bostaden"
    assert fl._similarity("Postadress (utdelningsadress och ortsadress), telefon bostaden", v1.labels["14"]) == 1.0
    assert fl._similarity("Postadress (utdelnings-adress och ortsadress), telefon bostaden", v1.labels["14"]) == 1.0
    blocks, _ = sl.parse_page(load_fixture("form-v1-a"))
    assert field(form_fields(blocks), "14").label == "Postadress (utdelningsadress och ortsadress), telefon bostaden"


# --- Fältparning, andra omkörningen (beslut BJ) ----------------------------------------
# Pilotens sida 1 (form-v1-d), 3 (form-v1-e), 5 (form-v1-c) och 9 (form-v2-d); värdena
# är kontrollerade mot sidbilderna (OCR-fel som 870980 för 870904 står kvar som de lästes).


def _heads(fields, number):
    return [f for f in fields if f.number == number and f.checked is None]


def test_label_line_with_two_field_numbers_is_split():
    # sida 5: "03 Uppgiftsmottagare 04 telefon" och "05 datum 06 klockan" är två
    # etiketter var; datum och klockslag står på raden under och delas per spalt
    items = load_fixture("form-v1-c")
    blocks, _ = sl.parse_page(items, page_height=3507)
    fields = form_fields(blocks)
    assert field(fields, "03", "Uppgiftsmottagare").value == "Krinsp A Lindahl"
    assert field(fields, "04", "telefon").value == ""
    assert field(fields, "05", "datum").value == "86-03-10"
    assert field(fields, "06", "klockan").value == "1430"
    md = sl.page_markdown(blocks).split("\n")
    assert "- **05 datum:** 86-03-10" in md and "- **06 klockan:** 1430" in md
    assert_conserved_page(items)


def test_two_numbers_split_only_when_both_labels_match():
    # "05 datum 06 minuter senare" är inte två etiketter: raden blir inget fält 05/06
    items = _v1_page(
        ("<p>05 datum 06 minuter senare kom han</p>", (1300, 400, 1900, 440)),
        ("<p>86-03-10 1430</p>", (1300, 445, 1700, 485)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert not {"05", "06"} & set(_form_numbers(blocks))
    assert "05 datum 06 minuter senare kom han" in sl.page_text(blocks).split("\n")
    assert_conserved_page(items)


def test_value_spanning_two_label_columns_is_split():
    # sida 3: "Ingvar Kjellvås 08-7694169" står under 03 och 04, "87-07-17 10.00"
    # under 05 och 06 (och börjar till vänster om 05:s etikett)
    items = load_fixture("form-v1-e")
    blocks, _ = sl.parse_page(items, page_height=3507)
    fields = form_fields(blocks)
    assert [field(fields, n).value for n in ("01", "02", "03", "04", "05", "06")] == [
        "Stockholm", "KK 1", "Ingvar Kjellvås", "08-7694169", "87-07-17", "10.00",
    ]
    assert_conserved_page(items)


def _row_page(*values):
    """v1-sida med etikettraden 03–06 och ``values`` = [(text, (x0, x1))] under den
    (sidbredd 2 400 bildpunkter som i 300 dpi, via rutan för 08)."""
    return _v1_page(
        ("<p>08 Löpande nr</p>", (2100, 200, 2400, 240)),
        ("<p>03 Uppgiftsmottagare</p>", (300, 700, 600, 740)),
        ("<p>04 telefon</p>", (820, 700, 960, 740)),
        ("<p>05 datum</p>", (1330, 700, 1470, 740)),
        ("<p>06 klockan</p>", (1580, 700, 1740, 740)),
        *((f"<p>{t}</p>", (x0, 750, x1, 795)) for t, (x0, x1) in values),
    )


def test_value_is_not_split_into_neighbour_with_own_value():
    # namnet når in under 04, men 04 har ett eget värde: inget delas
    items = _row_page(("Krinsp Karl-Otto Pålstig", (300, 960)), ("4107", (1050, 1170)), ("87-04-14", (1280, 1500)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert [field(fields, n).value for n in ("03", "04", "05")] == ["Krinsp Karl-Otto Pålstig", "4107", "87-04-14"]
    assert_conserved_page(items)


def test_value_outside_every_label_column_stays_text():
    # text långt till höger om radens sista etikett hör inte till raden
    items = _row_page(("Insp Persson", (300, 640)), ("i grövre brottmål", (2100, 2400)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "03").value == "Insp Persson"
    assert field(fields, "06").value == ""
    assert "i grövre brottmål" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_same_row_label_is_no_barrier_but_printed_caption_is():
    # sida 9: handskriften "EBE 10324-1-A" står över både 07 och 08; grannens etikett
    # på samma rad spärrade tidigare. Samma text i rutan för uppgiftslämnaren (under
    # den tryckta rubriken) är inget värde för 08.
    items = load_fixture("form-v2-d")
    blocks, _ = sl.parse_page(items, page_height=3507)
    fields = form_fields(blocks)
    assert field(fields, "07").value == "EBE"
    assert field(fields, "08").value == "10324-1-A"
    assert "EBE 10324-1-A" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_alias_caption_beside_number_is_a_label():
    # sida 9: "06" tar etiketten Promemoria från raden under; "Utskriftsdatum" bredvid
    # på samma rad är mallens alias för 06, och datumet under den är fältets värde
    items = load_fixture("form-v2-d")
    blocks, _ = sl.parse_page(items, page_height=3507)
    fields = form_fields(blocks)
    labels = {f.label: f.value for f in _heads(fields, "06")}
    assert labels == {"Promemoria": "", "Utskriftsdatum": "081013"}
    assert "- **06 Utskriftsdatum:** 081013" in sl.page_markdown(blocks).split("\n")


def test_alias_caption_needs_its_number_in_the_row():
    # "Utskriftsdatum" utan 06 på samma rad blir ingen etikett
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 100, 1700, 150)),
        ("<p>01 Polisdistrikt*</p>", (300, 200, 500, 240)),
        ("<p>Stockholm</p>", (310, 245, 600, 285)),
        ("<p>02 Arbetsenhet*</p>", (300, 300, 500, 340)),
        ("<p>RK-A-2</p>", (310, 345, 500, 385)),
        ("<p>03 Upprättat(d) av*</p>", (300, 400, 540, 440)),
        ("<p>Utskriftsdatum</p>", (1800, 800, 2000, 840)),
        ("<p>881013</p>", (1850, 845, 2050, 885)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert "06" not in _form_numbers(blocks)
    assert "881013" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_value_right_of_label_in_same_row():
    # sidfoten: datumet står till höger om "20 Tagit del" (sida 1 och 3) och
    # "0 870980 TS" till höger om "23 Ärendet tilldelat" (sida 3)
    # (på sida 1 följer signaturen i rutan för biträdande spaningschef med: den hör till 20)
    fields = form_fields(sl.parse_page(load_fixture("form-v1-d"), page_height=3507)[0])
    assert field(fields, "20").value == "860308 A.T."
    items = load_fixture("form-v1-e")
    fields = form_fields(sl.parse_page(items, page_height=3507)[0])
    assert field(fields, "20").value == "870980"
    assert field(fields, "23").value == "0 870980 TS"
    assert field(fields, "01").value == "Stockholm"  # rubriken till höger blir inget värde


def test_value_right_of_label_beyond_reach_stays_text():
    items = _v1_page(
        ("<p>07 Avsnitt</p>", (1800, 700, 1950, 740)),
        ("<p>D 2775</p>", (2300, 700, 2450, 740)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert field(form_fields(blocks), "07").value == ""
    assert "D 2775" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_value_in_row_band_below_neighbour_label():
    # sida 1: 17:s etikett är två rader hög; värdet står i dess nedre halva, under
    # grannetiketten 16 i samma rad, och hör till spalten för 17
    fields = form_fields(sl.parse_page(load_fixture("form-v1-d"), page_height=3507)[0])
    assert field(fields, "17").value == "MISSTänkt person, iaktagelser av-."
    assert field(fields, "16").value == "person"


def test_printed_caption_between_box_rules():
    v1 = {t.name: t for t in fl.load_templates()}["spaningsuppslag-1"]
    assert fl._is_printed("| datum", v1)
    assert fl._is_printed("Bilt spaningschef | Registrator", v1)
    assert not fl._is_printed("860308 | Registrator", v1)


def test_printed_words_in_value_row_stay_text():
    # pilotens sida 12 (form-v2-e): "05 Förhörsprot 06 Utskriftsdatum" är två etiketter,
    # och raden under ("Förhörsprot Promemoria 920114/GT") är tryck plus datumet; bara
    # datumet är ett värde (bilden: 920114/GT i rutan Utskriftsdatum)
    items = load_fixture("form-v2-e")
    blocks, _ = sl.parse_page(items, page_height=3507)
    fields = form_fields(blocks)
    assert field(fields, "05", "Förhörsprot").value == ""
    assert field(fields, "06", "Utskriftsdatum").value == "920114/GT"
    assert "Förhörsprot Promemoria" in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_row_waits_for_bare_number_label():
    # "05 06 Utskriftsdatum 900905/GT": 05 är ett ensamt nummer vars etikett står på
    # raden under; den raden tolkas som förut och tar inte 06:s datum (pilotens sida 15)
    items = _page(
        ("<p>SPANINGSUPPSLAG</p>", (1300, 200, 1700, 250)),
        ("<p>01 Polismyndighet*</p>", (280, 200, 550, 240)),
        ("<p>Stockholm</p>", (357, 240, 590, 290)),
        ("<p>02 Arbetsenhet*</p>", (285, 295, 505, 335)),
        ("<p>Rk-A2</p>", (357, 340, 495, 385)),
        ("<p>09 Brott med vilket uppgiften hör samman*</p>", (285, 491, 845, 533)),
        ("<p>05 06 Utskriftsdatum 900905/GT</p>", (1345, 491, 2171, 579)),
        ("<p>Förhörsprot Promemoria</p>", (1345, 554, 1824, 597)),
        ("<p>08 Löpande nr</p>", (2171, 270, 2400, 309)),
    )
    blocks, _ = sl.parse_page(items, page_height=3509)
    fields = form_fields(blocks)
    assert field(fields, "06", "Utskriftsdatum").value == "900905/GT"
    assert "Förhörsprot" not in field(fields, "06").value
    assert_conserved_page(items)


def test_label_behind_box_rule_is_not_a_value():
    # sida 3: "| 25 Spanchef: sign" (rutans linje läst som |) står till höger om 24 men
    # är en etikett, inte 24:s värde
    blocks, _ = sl.parse_page(load_fixture("form-v1-e"), page_height=3507)
    assert field(form_fields(blocks), "24").value == ""
    assert "| 25 Spanchef: sign" in _md_paragraphs(blocks)


# --- Beslut BK: prosa blir inga värden via radreglerna --------------------------------
# (granskarens syntetiska prober; riktiga korta värden paras fortfarande, se ovan)


@pytest.mark.parametrize(
    "text,bbox",
    [
        ("och sen gick han", (1800, 700, 2100, 740)),  # till höger om 06
        ("nej", (980, 700, 1100, 740)),  # mellan 04 och 05
    ],
)
def test_prose_beside_empty_label_stays_text(text, bbox):
    items = _row_page() + [raw(f"<p>{text}</p>", "Text", 99, bbox)]
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert all(text not in f.value for f in fields)
    assert text in _md_paragraphs(blocks)
    assert_conserved_page(items)


def _footer_prose_page(*extra):
    return _v1_page(
        ("<p>18 I sak</p>", (100, 1000, 400, 1040)),
        ("<p>Vittnet berättade att hon gick hem.</p>", (150, 1100, 1500, 1140)),
        ("<p>19 Ovanstående uppslag</p>", (290, 2956, 613, 2998)),
        ("<p>20 Tagit del</p>", (1573, 2953, 1752, 2991)),
        ("<p>21 anknyter till uppslag nr</p>", (290, 3058, 648, 3100)),
        ("<p>22 Registrerat</p>", (290, 3139, 500, 3180)),
        ("<p>23 Ärendet tilldelat</p>", (538, 3139, 796, 3184)),
        ("<p>24 Färdigbearbetat datum</p>", (1573, 3132, 1938, 3170)),
        *extra,
    )


@pytest.mark.parametrize(
    "text,bbox",
    [("sa hon till slut.", (700, 3058, 1000, 3100)), ("Hon gick sedan hem.", (650, 2950, 1000, 2995))],
)
def test_prose_beside_footer_label_stays_text(text, bbox):
    items = _footer_prose_page((f"<p>{text}</p>", bbox))
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert all(text not in f.value for f in form_fields(blocks))
    assert text in _md_paragraphs(blocks)
    assert_conserved_page(items)


def test_value_beside_footer_label_still_pairs():
    items = _footer_prose_page(("<p>860308</p>", (1836, 2953, 2079, 3000)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert field(form_fields(blocks), "20").value == "860308"


def test_prose_under_label_row_is_not_split_across_columns():
    # "springa mot Tunnelgatan" når in under 04 men är prosa: raden delas inte
    items = _row_page(("Han sa att han sett mannen", (300, 1200)))
    items.append(raw("<p>springa mot Tunnelgatan</p>", "Text", 99, (300, 805, 1100, 845)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "04").value == ""
    assert "springa mot Tunnelgatan" not in field(fields, "03").value
    paras = _md_paragraphs(blocks)
    assert "springa mot Tunnelgatan" in paras
    assert_conserved_page(items)


def test_prose_line_alone_under_label_row_stays_text_in_order():
    # beslut BL: en prosarad som når över 03 och 04 blir varken delad eller 03:s värde
    items = _row_page()
    items.append(raw("<p>springa mot Tunnelgatan</p>", "Text", 99, (300, 805, 1100, 845)))
    items.append(raw("<p>Han kom hem sent.</p>", "Text", 100, (300, 900, 1100, 940)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "03").value == "" and field(fields, "04").value == ""
    paras = _md_paragraphs(blocks)
    k = paras.index("springa mot Tunnelgatan")
    assert paras[k + 1] == "Han kom hem sent."
    assert_conserved_page(items)


def test_title_before_phone_is_split():
    # "krinsp 08-7694169": ett ensamt gement titelord före ett nummer delas ändå
    items = _row_page(("krinsp 08-7694169", (300, 1050)))
    blocks, _ = sl.parse_page(items, page_height=3500)
    fields = form_fields(blocks)
    assert field(fields, "03").value == "krinsp"
    assert field(fields, "04").value == "08-7694169"
    assert_conserved_page(items)


def test_value_like():
    for text in ("87-07-17", "0 870980 TS", "EBE", "Ingvar Kjellvås", "krinsp Lennart Gustafsson", "A.T.",
                 "Leif Marklund, krinsp.", "Knubb, Lars"):
        assert fl._value_like(text), text
    for text in ("nej", "springa mot", "sa hon till slut.", "Hon gick sedan hem.", "och sen gick han",
                 "Han kom. sedan gick han"):
        assert not fl._value_like(text), text


def test_long_prose_on_label_row_still_blocks():
    # beslut BK: undantaget för samma rad gäller grannetiketter, inte löptext
    long = "Han berättade " + "att han hade gått hem " * 10
    items = _v1_page(
        ("<p>03 Uppgiftsmottagare</p>", (300, 700, 600, 740)),
        (f"<p>{long}</p>", (300, 715, 2300, 755)),
        ("<p>Insp Persson</p>", (300, 760, 600, 800)),
    )
    blocks, _ = sl.parse_page(items, page_height=3500)
    assert field(form_fields(blocks), "03").value == ""
    assert "Insp Persson" in _md_paragraphs(blocks)
    assert_conserved_page(items)
