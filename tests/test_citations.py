"""Tester för citations — uppslag och länkning av [Nr X, sida Y]-citat."""

from __future__ import annotations

from pathlib import Path

from citations import (
    CITE_RE,
    build_nr_to_pdf,
    extract_cited_sources,
    linkify_citations,
    resolve_nr_all,
)


def _mapping(tmp_path: Path, names: list[str]) -> dict[str, Path]:
    """Bygg en nr→PDF-mapping från riktiga filer i en tmp-katalog."""
    d = tmp_path / "downloaded" / "files"
    d.mkdir(parents=True, exist_ok=True)
    for name in names:
        (d / f"{name}.pdf").write_bytes(b"%PDF")
    return build_nr_to_pdf(tmp_path)


# ---------------------------------------------------------------------------
# CITE_RE
# ---------------------------------------------------------------------------

def test_cite_re_matches_simple_and_dotted_nr() -> None:
    assert CITE_RE.search("[Nr 281, sida 4]").group(1) == "281"
    assert CITE_RE.search("Nr 1322.7, sida 12").group(1) == "1322.7"
    assert CITE_RE.search("[Nr 281,10, sida 1]").group(1) == "281,10"


# ---------------------------------------------------------------------------
# build_nr_to_pdf / resolve_nr_all
# ---------------------------------------------------------------------------

def test_build_nr_to_pdf_uses_first_name_field(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Förhör — 2022 — D-1 — 2"])
    assert "281" in mapping


def test_build_nr_to_pdf_registers_wpu_prefix_alias(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["Pol-1986_-_total_version_nr_0 — x"])
    assert "Pol-1986" in mapping
    assert "Pol-1986_-_total_version_nr_0" in mapping


def test_resolve_exact_match(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Titel"])
    assert [p.stem for p in resolve_nr_all("281", mapping)] == ["281 — Titel"]


def test_resolve_prefix_fallback_for_truncated_nr(tmp_path: Path) -> None:
    """Modellen klipper ofta titelsuffixet — prefix-matchning ska hitta filen."""
    mapping = _mapping(tmp_path, ["Pol-1986-EA9982-00-B_Förhör-Jan — x"])
    hits = resolve_nr_all("Pol-1986-EA9982-00-B", mapping)
    assert len(hits) == 1


def test_resolve_prefix_does_not_match_longer_nr(tmp_path: Path) -> None:
    """'28' får inte prefix-matcha '281' (nästa tecken är alfanumeriskt)."""
    mapping = _mapping(tmp_path, ["281 — Titel"])
    assert resolve_nr_all("28", mapping) == []


def test_resolve_ambiguous_returns_all_sorted(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, [
        "EA9982-00-B_Förhör-B — x",
        "EA9982-00-B_Förhör-A — x",
    ])
    hits = resolve_nr_all("EA9982-00-B", mapping)
    assert [p.stem for p in hits] == [
        "EA9982-00-B_Förhör-A — x",
        "EA9982-00-B_Förhör-B — x",
    ]


# ---------------------------------------------------------------------------
# linkify_citations
# ---------------------------------------------------------------------------

def test_linkify_single_match_renders_anchor(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Titel"])
    out = linkify_citations("Se [Nr 281, sida 4].", mapping)
    assert "<a href=" in out
    assert "[Nr 281, sida 4]" in out


def test_linkify_unknown_nr_left_untouched(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Titel"])
    text = "Se [Nr 999, sida 1]."
    assert linkify_citations(text, mapping) == text


def test_linkify_known_sources_narrows_ambiguous(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, [
        "EA9982-00-B_Förhör-A — x",
        "EA9982-00-B_Förhör-B — x",
    ])
    out = linkify_citations(
        "[Nr EA9982-00-B, sida 1]",
        mapping,
        known_sources={"EA9982-00-B_Förhör-A — x.txt"},
    )
    # Smalnat till en kandidat → exakt en länk
    assert out.count("<a href=") == 1
    assert "Förhör-A" not in out  # entydig länk märks inte med suffix


def test_linkify_ambiguous_renders_one_link_per_candidate(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, [
        "EA9982-00-B_Förhör-A — x",
        "EA9982-00-B_Förhör-B — x",
    ])
    out = linkify_citations("[Nr EA9982-00-B, sida 1]", mapping)
    assert out.count("<a href=") == 2


# ---------------------------------------------------------------------------
# extract_cited_sources
# ---------------------------------------------------------------------------

def test_extract_cited_sources_dedups_per_stem(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Titel"])
    answer = "Först [Nr 281, sida 1], sen [Nr 281, sida 2]."
    srcs = extract_cited_sources(answer, mapping)
    assert len(srcs) == 1
    assert srcs[0]["source"] == "281 — Titel.txt"
    assert srcs[0]["titel"] == "Titel"


def test_extract_cited_sources_empty_for_no_citations(tmp_path: Path) -> None:
    mapping = _mapping(tmp_path, ["281 — Titel"])
    assert extract_cited_sources("Inget citat här.", mapping) == []
