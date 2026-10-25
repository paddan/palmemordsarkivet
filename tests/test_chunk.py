"""Tester för chunk_text från rag/ingest.py."""

from __future__ import annotations

from ingest import chunk_text


def test_short_text_one_chunk() -> None:
    chunks = chunk_text("Kort text.", size=800, overlap=150)
    assert len(chunks) == 1
    assert chunks[0][2] == "Kort text."


def test_long_text_multiple_chunks() -> None:
    text = ("Mening ett. " * 200).strip()
    chunks = chunk_text(text, size=800, overlap=150)
    assert len(chunks) >= 2
    # alla chunks ska vara icke-tomma
    for _, _, c in chunks:
        assert c.strip()


def test_no_formfeed_in_chunks() -> None:
    text = "Sida ett.\fSida två."
    chunks = chunk_text(text, size=800, overlap=150)
    # chunkern får aldrig spotta ut \f
    for _, _, c in chunks:
        assert "\f" not in c


def test_empty_text() -> None:
    assert chunk_text("", 800, 150) == []
    assert chunk_text("   \n  ", 800, 150) == []


# ---------------------------------------------------------------------------
# chunk_markdown — strukturmedveten chunkning av Surya-layoutsidor (Task 6)
# ---------------------------------------------------------------------------

import re  # noqa: E402

import pytest  # noqa: E402
from ingest import chunk_markdown, is_useful  # noqa: E402
from test_surya_layout import load_fixture  # noqa: E402

import surya_layout as sl  # noqa: E402


def _fixture_md(name: str) -> str:
    blocks, _ = sl.parse_page(load_fixture(name))
    return sl.page_markdown(blocks)


def _table(rows: int) -> str:
    lines = ["| Datum | Plats | Iakttagelse |", "| --- | --- | --- |"]
    lines += [
        f"| 1986-02-{rows % 28 + 1:02d} | Sveavägen {i} | Mannen gick norrut mot Tunnelgatan rad {i} |"
        for i in range(rows)
    ]
    return "\n".join(lines)


def test_chunk_markdown_never_splits_table_row() -> None:
    table = _table(30)
    rows = table.split("\n")[2:]
    chunks = chunk_markdown(table, size=400, overlap=100)
    assert len(chunks) > 1
    for _, _, c in chunks:
        assert len(c) <= 400
        for line in c.split("\n"):
            # varje rad i en chunk är en hel tabellrad (rubrik, avdelare eller datarad)
            assert line in table.split("\n"), line
    # alla datarader finns med exakt en gång (ingen överlapp inuti tabellen)
    seen = [ln for _, _, c in chunks for ln in c.split("\n")[2:]]
    assert seen == rows


def test_chunk_markdown_repeats_table_header() -> None:
    table = _table(30)
    header, sep = table.split("\n")[:2]
    chunks = chunk_markdown("Inledande stycke om tabellen.\n\n" + table, size=400, overlap=100)
    table_chunks = [c for _, _, c in chunks if "| --- |" in c]
    assert len(table_chunks) > 1
    for c in table_chunks:
        lines = c.split("\n")
        i = lines.index(header)
        assert lines[i + 1] == sep


def test_chunk_markdown_splits_at_blank_lines() -> None:
    paras = [f"Stycke {n}. " + ("ord " * 70).strip() for n in range(3)]
    md = "\n\n".join(paras)
    chunks = [c for _, _, c in chunk_markdown(md, size=800, overlap=150)]
    assert chunks == [paras[0] + "\n\n" + paras[1], paras[2]]
    for c in chunks:
        assert len(c) <= 800


def test_chunk_markdown_keeps_short_page_whole() -> None:
    md = "# Rubrik\n\nKort stycke."
    assert chunk_markdown(md, 800, 150) == [(0, len(md), md)]
    assert chunk_markdown("   \n\n  ", 800, 150) == []


def test_page_header_prefixed_to_every_chunk() -> None:
    md = _fixture_md("forhor-dialog-a")
    header = md.split("\n")[0]
    assert header.startswith("> Sidhuvud:")
    chunks = chunk_markdown(md, size=500, overlap=100)
    assert len(chunks) > 2
    for _, _, c in chunks:
        assert c.split("\n")[0] == header
        assert c.count("> Sidhuvud:") == 1
        assert len(c) <= 500


def test_page_header_truncated_when_long() -> None:
    header = "> Sidhuvud: " + "Förhör med vittnet " * 20
    md = header + "\n\n" + "\n\n".join(f"Stycke {n} " + "text " * 30 for n in range(6))
    chunks = chunk_markdown(md, size=400, overlap=50)
    for _, _, c in chunks:
        first = c.split("\n")[0]
        assert first == header[:100]
        assert len(c) <= 400


def test_chunk_keeps_question_with_answer() -> None:
    # Fyllnad gör att frågan ryms sist i första chunken men inte svaret:
    # då ska frågan flytta med till svarets chunk.
    filler = "Inledning. " + "ord " * 150
    q = "**F:** Var stod du när skotten föll?"
    a = "**S:** Vid trappan till Tunnelgatan. " + "ord " * 40
    md = "\n\n".join([filler.strip(), q, a.strip()])
    assert len(filler.strip()) + 2 + len(q) <= 800
    chunks = [c for _, _, c in chunk_markdown(md, size=800, overlap=150)]
    assert any(q in c and a.strip() in c for c in chunks)
    assert not any(q in c and a.strip() not in c for c in chunks)


def test_chunk_keeps_question_with_answer_in_fixture() -> None:
    md = _fixture_md("forhor-dialog-a")
    chunks = [c for _, _, c in chunk_markdown(md, size=500, overlap=100)]
    body = md.split("\n\n")[1:]
    turn = re.compile(r"^\*\*[A-ZÅÄÖ]{1,3}:\*\*")
    for i, block in enumerate(body):
        if not block.startswith("**F:**"):
            continue
        j = next((k for k in range(i + 1, len(body)) if turn.match(body[k])), None)
        if j is None:
            continue
        pair = "\n\n".join(body[i:j + 1])
        if len(pair) + len(md.split("\n")[0]) + 1 > 500:
            continue
        assert any(pair in c for c in chunks), block


def test_chunk_markdown_splits_before_turn_inside_block() -> None:
    # Repliker som bara skiljs av radbrott delas ändå vid replikgränsen.
    turns = [f"**{'F' if n % 2 == 0 else 'S'}:** " + f"replik {n} " + "ord " * 40 for n in range(8)]
    md = "\n".join(t.strip() for t in turns)
    chunks = [c for _, _, c in chunk_markdown(md, size=500, overlap=100)]
    assert len(chunks) > 1
    for c in chunks:
        assert re.match(r"^\*\*[FS]:\*\*", c), c[:40]
        assert len(c) <= 500


def test_form_block_split_between_fields() -> None:
    md = _fixture_md("form-v1-a")
    form = md.split("\n\n")[0]
    assert form.startswith("- **01 ")
    chunks = [c for _, _, c in chunk_markdown(form, size=200, overlap=50)]
    assert len(chunks) > 1
    for prev, c in zip([""] + chunks, chunks, strict=False):
        assert len(c) <= 200
        # varje chunk börjar på ett fält — utom när en för kort chunk fått
        # föregående chunks slut som överlapp (regel AJ)
        if not c.startswith("- **"):
            overlap = c.split("\n\n")[0]
            assert prev.endswith(overlap), c[:30]
    # alternativen står kvar under sitt fält
    hit = next(c for c in chunks if "**11 Spaningsuppslaget lämnat:**" in c)
    assert "  - [x] per telefon" in hit
    assert "  - [ ] per brev" in hit
    # inga fält tappas
    for line in form.split("\n"):
        assert any(line in c for c in chunks), line


def test_form_block_kept_whole_when_it_fits() -> None:
    md = _fixture_md("form-v1-a")
    form = md.split("\n\n")[0]
    chunks = [c for _, _, c in chunk_markdown(md, size=800, overlap=150)]
    assert chunks[0].startswith(form)


def test_sparse_table_chunk_is_useful() -> None:
    table = "\n".join(
        ["| Nr | Fält | Värde |", "| --- | --- | --- |"]
        + [f"| {n:02d} | f{n} | ja |" for n in range(1, 9)]
    )
    assert len(table) >= 80
    assert is_useful(table) is False          # textvägen oförändrad
    assert is_useful(table, markdown=True) is True


def test_markdown_is_useful_still_rejects_junk() -> None:
    junk = "| --- | --- |\n" + "| ☐ | ☐ | ☐ | ., | ;; |\n" * 6
    assert is_useful(junk, markdown=True) is False
    assert is_useful("**F:** Ja.", markdown=True) is False  # för kort


def _assert_conserved(md: str, size: int, overlap: int = 150) -> None:
    """Regel AJ: varje brödtexttoken finns i minst en chunk som indexeras."""
    from normalize_text import normalize_markdown

    md = normalize_markdown(md).strip()
    body = md.partition("\n")[2] if md.startswith("> Sidhuvud:") else md
    chunks = [c for _, _, c in chunk_markdown(md, size=size, overlap=overlap)]
    for c in chunks:
        assert len(c) <= size
    useful = [c for c in chunks if is_useful(c, markdown=True)]
    missing = [tok for tok in body.split() if not any(tok in c for c in useful)]
    assert not missing, missing[:10]


FIXTURE_NAMES = ["form-v1-a", "form-v2-a", "form-v2-b", "forhor-dialog-a", "forhor-dialog-b"]


@pytest.mark.parametrize("size", [200, 400, 800])
@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_chunks_respect_size_and_keep_text(name: str, size: int) -> None:
    _assert_conserved(_fixture_md(name), size)


def test_short_turn_after_long_paragraph_not_lost() -> None:
    long = "Lång " + "x" * 787
    assert len(long) == 792
    md = long + "\n\n**M:** Ja."
    _assert_conserved(md, 800)


def test_short_unit_between_long_paragraphs_not_lost() -> None:
    long1 = "Första stycket " + "ord " * 190
    long2 = "Andra stycket " + "ord " * 190
    md = "\n\n".join([long1.strip(), "**M:** Ja, det stämmer.", long2.strip()])
    _assert_conserved(md, 800)


def test_short_chunk_merged_into_previous_when_it_fits() -> None:
    # Ett kort ensamt stycke efter en full chunk ska hamna i en indexerbar chunk.
    a = "Stycke " + "a" * 690
    b = "Stycke " + "b" * 690
    md = "\n\n".join([a, b, "**M:** Ja."])
    chunks = [c for _, _, c in chunk_markdown(md, 800, 150)]
    hit = [c for c in chunks if "**M:** Ja." in c]
    assert hit and all(is_useful(c, markdown=True) for c in hit)


def test_header_does_not_make_short_chunk_useful() -> None:
    header = "> Sidhuvud: Förhör med vittnet Andersson om iakttagelser på Sveavägen · Sid 3(5)"
    assert is_useful(header + "\n**M:** Ja.", markdown=True) is False
    assert is_useful(header + "\n" + "Vittnet uppger att hon gick hem. " * 3, markdown=True) is True




def test_short_first_chunk_borrows_head_of_next() -> None:
    # Kort inledande replik före ett fullt stycke: inget föregående att låna från.
    md = "**M:** Ja.\n\n" + "Stycke " + "ord " * 190
    _assert_conserved(md.strip(), 800)


def test_random_dialog_pages_lose_no_unit() -> None:
    import random

    rng = random.Random(7)
    words = ["vittnet", "uppger", "att", "hon", "gick", "längs", "Sveavägen", "mot", "Tunnelgatan", "kvällen"]
    for n in range(300):
        size = rng.choice([200, 400, 800])
        parts = ["> Sidhuvud: Förhör med X · Sid 2(5)"] if rng.random() < 0.5 else []
        for k in range(rng.randint(1, 12)):
            r = rng.random()
            if r < 0.3:
                parts.append("**F:** " + " ".join(rng.choices(words, k=rng.randint(1, 30))))
            elif r < 0.6:
                parts.append("**M:** " + rng.choice(
                    ["Ja.", "Nej.", "Ja, det sa jag.", " ".join(rng.choices(words, k=rng.randint(1, 120)))]
                ))
            else:
                parts.append(" ".join(rng.choices(words, k=rng.randint(1, 140))) + f" s{n}k{k}")
        md = "\n\n".join(parts)
        body = md.partition("\n")[2] if parts[0].startswith(">") else md
        if len(body) < 80:
            continue  # för kort sida faller bort även i textvägen
        _assert_conserved(md, size)


def test_rescued_tail_from_table_carries_header() -> None:
    rows = ["| Tid | Plats | Iakttagelse |", "| --- | --- | --- |"] + [
        f"| 23.{i:02d} | Sveavägen | mannen gick norrut mot Tunnelgatan |" for i in range(20)
    ]
    md = (
        "> Sidhuvud: Förhör med X · Sid 2(5)\n\n" + "\n".join(rows)
        + "\n\nEfter detta sade vittnet att hon gick hem."
    )
    chunks = chunk_markdown(md, 400, 150)
    assert all(len(c) <= 400 for _, _, c in chunks)
    last = chunks[-1][2]
    assert "Efter detta sade vittnet" in last
    for _, _, c in chunks:
        if "| 23." in c:
            assert "| Tid | Plats | Iakttagelse |" in c and "| --- | --- | --- |" in c
    assert is_useful(last, markdown=True)


def test_question_followed_by_question_is_not_paired() -> None:
    q1 = "**F:** " + "Var befann du dig under kvällen då det hände, berätta i detalj? " * 1
    q2 = "**F:** Och hur dags gick du hemifrån den kvällen egentligen?"
    ans = "**S:** Jag gick hemifrån vid elva ungefär och sedan gick jag ut med hunden."
    chunks = [c for _, _, c in chunk_markdown(f"{q1.strip()}\n\n{q2}\n\n{ans}", 140, 30)]
    # den andra frågan paras med sitt svar, den första står för sig
    assert any(q2 in c and ans in c for c in chunks)
    assert not any(q1.strip() in c and q2 in c for c in chunks)
    assert all(len(c) <= 140 for c in chunks)


def _tbl(n: int, row: str = "| 23.{i:02d} | Sveavägen | mannen gick norrut mot Tunnelgatan |") -> str:
    head = ["| Tid | Plats | Iakttagelse |", "| --- | --- | --- |"]
    return "\n".join(head + [row.format(i=i) for i in range(n)])


def test_short_tail_after_small_table_stays_useful() -> None:
    rows = ["| Tid | Plats | Iakttagelse |", "| --- | --- | --- |"] + [
        f"| 23.0{i} | Sveavägen | mannen gick norrut mot Tunnelgatan och vidare upp |" for i in range(3)
    ]
    sentence = "Vittnet hörde ett skott och sprang hem."
    md = "> Sidhuvud: Förhör med Anna Andersson 1986-03-02 · Sid 2(5)\n\n" + "\n".join(rows) + "\n\n" + sentence
    chunks = chunk_markdown(md, 200, 150)
    assert all(len(c) <= 200 for _, _, c in chunks)
    assert any(sentence in c and is_useful(c, markdown=True) for _, _, c in chunks)


@pytest.mark.parametrize("size", [200, 400, 800])
@pytest.mark.parametrize("rows", [1, 3, 8, 25])
def test_short_units_around_table_are_conserved(size: int, rows: int) -> None:
    after = "Vittnet hörde ett skott och sprang hem."
    before = "Ja."
    for md in (
        f"{_tbl(rows)}\n\n{after}",
        f"{before}\n\n{_tbl(rows)}\n\n{after}",
        f"> Sidhuvud: Förhör X · Sid 2(5)\n\n{before}\n\n{_tbl(rows)}\n\n{after}",
    ):
        chunks = chunk_markdown(md, size, 150)
        assert all(len(c) <= size for _, _, c in chunks)
        useful = [c for _, _, c in chunks if is_useful(c, markdown=True)]
        for needle in (after, before) if before in md else (after,):
            assert any(needle in c for c in useful), (size, rows, needle)


# --- Breda tabeller (granskarens probe5: wide/multi) ------------------------
# Generatorerna speglar probe4/probe5 men med unika ord (ord + löpnummer), så att
# en token som faller bort inte döljs av att samma ord står i en annan chunk.

_ORD = [
    "polisen", "vittnet", "bilen", "gatan", "kvällen", "mannen",
    "kvinnan", "sade", "hörde", "såg", "rockar", "mörk",
]
_SIDHUVUD_PROBE = "> Sidhuvud: Förhör med Anna Andersson 1986-03-02 · Sid 2(5)"


class _Sidgen:
    def __init__(self, seed: int) -> None:
        import random

        self.rng = random.Random(seed)
        self.n = 0

    def ord(self) -> str:
        self.n += 1
        return f"{self.rng.choice(_ORD)}{self.n}"

    def mening(self, k: int) -> str:
        return " ".join(self.ord() for _ in range(k)).capitalize() + "."

    def kort(self) -> str:
        while True:
            s = self.mening(self.rng.randint(1, 8))
            if len(s) < 78:
                return s

    def tabell(self, bred: bool, size: int) -> str:
        rng = self.rng
        if bred:
            hlen = rng.randint(int(size * 0.3), int(size * 0.55))
            cols = rng.randint(3, 6)
            namn = [
                f"Kolumn{self.n + c}" + "x" * max(0, hlen // cols - 14) for c in range(cols)
            ]
            self.n += cols
            rows = ["| " + " | ".join(namn) + " |", "|" + "|".join([" --- "] * cols) + "|"]
            for _ in range(rng.randint(1, 12)):
                rows.append("| " + " | ".join(
                    self.mening(rng.randint(1, 8 if rng.random() < 0.5 else 25)) for _ in range(cols)
                ) + " |")
        else:
            rows = ["| Tid | Plats | Iakttagelse |", "| --- | --- | --- |"] + [
                f"| 23.{i:02d} | {self.mening(2)} | {self.mening(rng.randint(1, 15))} |"
                for i in range(rng.randint(1, 25))
            ]
        return "\n".join(rows)

    def sida(self, kind: str, size: int) -> str:
        rng = self.rng
        p = [_SIDHUVUD_PROBE]
        if kind == "table_then_short":
            p += [self.tabell(False, size), self.kort()]
        elif kind == "short_table_short":
            p += [self.kort(), self.tabell(False, size), self.kort()]
        elif kind == "wide":
            p += [self.kort() if rng.random() < 0.5 else self.mening(20), self.tabell(True, size), self.kort()]
        else:  # multi
            for _ in range(rng.randint(2, 5)):
                p += [self.tabell(rng.random() < 0.3, size), self.kort()]
        return "\n\n".join(p)


def _forlorade_token(md: str, size: int) -> list[str]:
    chunks = [c for _, _, c in chunk_markdown(md, size, 150)]
    assert all(len(c) <= size for c in chunks), [len(c) for c in chunks]
    have = {
        t for c in chunks if is_useful(c, markdown=True)
        for t in re.findall(r"\w+", c.partition("\n")[2] if c.startswith(">") else c)
    }
    return [t for t in re.findall(r"\w+", md.partition("\n")[2]) if t not in have]


@pytest.mark.parametrize(
    "kind,size",
    [
        ("table_then_short", 200), ("table_then_short", 400), ("table_then_short", 800),
        ("short_table_short", 200), ("short_table_short", 400), ("short_table_short", 800),
        ("wide", 200), ("wide", 400), ("wide", 800),
        ("multi", 200), ("multi", 400), ("multi", 800),
    ],
)
def test_wide_and_multi_tables_lose_no_token(kind: str, size: int) -> None:
    gen = _Sidgen(seed=len(kind) * 1000 + size)
    lost = {}
    for n in range(40):
        md = gen.sida(kind, size)
        miss = _forlorade_token(md, size)
        if miss:
            lost[n] = miss[:5]
    assert not lost, (len(lost), list(lost.items())[:3])


def test_wide_table_header_is_indexed_when_no_row_fits_beside_it() -> None:
    # Rubrik + avdelare ryms men ingen rad ryms bredvid: rubriken får inte försvinna.
    header = "| " + " | ".join(f"Kolumnnamn{c}" for c in range(5)) + " |"
    sep = "|" + "|".join([" --- "] * 5) + "|"
    row = "| " + " | ".join(f"cell{c} " + "ord " * 16 for c in range(5)) + " |"
    md = "\n".join([header, sep, row, row.replace("cell", "andra")])
    chunks = [c for _, _, c in chunk_markdown(md, 400, 150)]
    assert all(len(c) <= 400 for c in chunks)
    assert any(header in c and is_useful(c, markdown=True) for c in chunks)


def test_overlong_table_row_split_at_cell_boundary() -> None:
    cells = [f"cell{c} " + " ".join(f"ord{c}x{k}" for k in range(20)) for c in range(4)]
    row = "| " + " | ".join(cells) + " |"
    md = "\n".join(["| A | B | C | D |", "| --- | --- | --- | --- |", row])
    chunks = [c for _, _, c in chunk_markdown(md, 300, 150)]
    assert all(len(c) <= 300 for c in chunks)
    text = "\n".join(chunks)
    # varje cell hamnar hel i någon chunk (delning vid |, inte mitt i en cell)
    for cell in cells:
        assert cell in text, cell
