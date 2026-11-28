"""Tester för dokumentmeta: tolkning av arkivets filnamnsstammar."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dokumentmeta import (  # noqa: E402 — kräver src på sys.path ovan
    lance_name_fields,
    parse_stem,
    uppslag_key_str,
)


def _legacy(stem):  # kopia av dagens ingest.parse_filename, bara nr/titel
    parts = [p.strip() for p in stem.split(" — ")]
    nr = parts[0] or stem[:40]
    return {"nr": nr, "titel": parts[1] if len(parts) > 1 else ""}


def test_two_dates_are_ordered_then_published():
    m = parse_stem("280.8 — Om vykort till Regeringskansliet — 2020-08-15 — 2020-10-21 — HI-7504-10 — 2")
    assert (m.arkiv, m.nr, m.bestallt, m.upplagt, m.anmarkning, m.antal_sidor) == \
        ("palme", "280.8", "2020-08-15", "2020-10-21", "HI-7504-10", 2)
    assert m.uppslag == ("HI7504-10",)


def test_single_date_is_published_not_ordered():
    m = parse_stem("1361 — Iakttagelser av tre män på Västerlånggatan under veckan innan mordet — 2021-12-11 — Z-8624 — 8")
    assert (m.bestallt, m.upplagt, m.anmarkning, m.antal_sidor) == (None, "2021-12-11", "Z-8624", 8)


def test_date_and_pages_without_remark():
    m = parse_stem("1295.347 — Ledningsgruppen diverse dokument 14 — 2022-03-14 — 19")
    assert (m.upplagt, m.anmarkning, m.antal_sidor) == ("2022-03-14", None, 19)


def test_date_and_remark_without_pages():
    m = parse_stem("2060 — Grandbesökare — 2023-08-30 — L-1172-00")
    assert (m.anmarkning, m.antal_sidor, m.uppslag) == ("L-1172-00", None, ("L1172-00",))


def test_dash_placeholder_means_unknown_pages():
    m = parse_stem("667.1 — Förhör med statsminister Ingvar Carlsson, annan maskning — 2021-10-19 — T-8517-00, -A, se 1181 — -")
    assert (m.anmarkning, m.antal_sidor, m.hanvisningar) == ("T-8517-00, -A, se 1181", None, ("1181",))


def test_references_split_on_comma_space_only():
    m = parse_stem("29.2 — Utlåtande från NFC — Se 29, 564, 281,10, 678.1 — 7")
    assert (m.upplagt, m.hanvisningar, m.antal_sidor) == (None, ("29", "564", "281,10", "678.1"), 7)


def test_comma_in_number_is_kept_verbatim():
    assert parse_stem("281,10 — Titel — 2021-01-01 — 3").nr == "281,10"


def test_truncated_stem_drops_half_date():
    head, tail = "120 — ", " — 2020-09-15 — 2020-09-"
    stem = head + "x" * (180 - len(head) - len(tail)) + tail
    assert len(stem) == 180
    m = parse_stem(stem)
    assert m.avkortat and m.anmarkning is None and m.upplagt == "2020-09-15" and m.varningar


def test_truncated_stem_does_not_trust_trailing_digits():
    stem = "32 — " + "y" * 150 + " — 2020-06-22 — 2020-07-14 — Alla fantombilder — 4"
    m = parse_stem(stem)
    assert len(stem) >= 180 and m.antal_sidor is None and m.varningar


def test_lance_fields_match_legacy_parser():
    for stem in ("1 — PM — 2020-06-12 — 2020-07-14 — E-63-1, se 531.1 — 10",
                 "2208 — Presskonferens — 2025-04-30", "281,10 — T",
                 "Pol-1986-03-04_E9979-00-A_VITTNESFÖRHÖR-Hans-Johansson", "ABC_Oxen_skiss"):
        assert lance_name_fields(stem) == _legacy(stem)


def test_uppslag_key_str():
    assert uppslag_key_str(("E", 63, 1, "")) == "E63-01"
    assert uppslag_key_str(("EDE", 9980, 0, "A")) == "EDE9980-00-A"


def test_wpu_full_date_code_and_title():
    m = parse_stem("Pol-1986-03-04_E9979-00-A_VITTNESFÖRHÖR-Hans-Johansson")
    assert (m.arkiv, m.nr, m.dokumentdatum, m.uppslag, m.titel) == \
        ("wpu", None, "1986-03-04", ("E9979-00-A",), "VITTNESFÖRHÖR-Hans-Johansson")
    assert (m.bestallt, m.upplagt, m.anmarkning, m.antal_sidor, m.hanvisningar) == \
        (None, None, None, None, ())


def test_wpu_time_token_stays_in_title():
    m = parse_stem("Pol-1989-06-14_1245_KD11596-27_Telförhör_med_Jimmy_Lindström_om_Algot_Åsell")
    assert m.uppslag == ("KD11596-27",)
    assert m.titel == "1245 Telförhör med Jimmy Lindström om Algot Åsell"


def test_wpu_unknown_date_and_partial_date():
    placeholder = parse_stem("Pol-YYYY-MM-DD_E167-01_Per_Vallin")
    assert placeholder.dokumentdatum is None and not placeholder.varningar
    assert parse_stem("Pol-1986-MM-DD_Mötesprotokoll").dokumentdatum == "1986"
    assert parse_stem("Pol-1986-03-DD_Mötesprotokoll").dokumentdatum == "1986-03"


def test_wpu_invalid_date_is_none_with_warning():
    m = parse_stem("Pol-1986-02-30_Mötesprotokoll")
    assert m.dokumentdatum is None and m.varningar


def test_wpu_unknown_code_prefix_is_removed():
    m = parse_stem("Pol-YYYY-MM-DD_OKÄND_UPPSLAGSKOD_VITTNESFÖRHÖR-Nicola-Fauzzi")
    assert (m.uppslag, m.titel) == ((), "VITTNESFÖRHÖR-Nicola-Fauzzi")


def test_wpu_without_code():
    m = parse_stem("Pol-1986-11-24_Mötesprotokoll-Ledningsgruppen")
    assert (m.uppslag, m.titel) == ((), "Mötesprotokoll-Ledningsgruppen")


def test_wpu_code_last_gives_empty_title():
    m = parse_stem("Pol-1986-03-04_E9979-00-A")
    assert (m.uppslag, m.titel) == (("E9979-00-A",), "")


def test_non_pol_stems():
    m = parse_stem("1986-03-01_Aftonbladet")
    assert (m.dokumentdatum, m.titel) == ("1986-03-01", "Aftonbladet")
    m = parse_stem("Leif_Wilhelm_Berg_890405")
    assert (m.dokumentdatum, m.titel, m.uppslag) == (None, "Leif Wilhelm Berg 890405", ())


def test_truncated_stem_drops_cut_last_reference():
    head, tail = "32 — Titel — 2020-06-12 — 2020-07-14 — ", " — se 156.1, 15"
    stem = head + "x" * (180 - len(head) - len(tail)) + tail
    assert len(stem) == 180
    m = parse_stem(stem)
    assert m.avkortat and m.antal_sidor is None
    assert m.hanvisningar == ("156.1",)
    assert any("hänvisning" in w for w in m.varningar)


def test_untruncated_stem_keeps_last_reference():
    m = parse_stem("32 — Titel — 2020-06-12 — se 156.1, 15")
    assert m.hanvisningar == ("156.1", "15")


def test_wpu_full_date_with_dash_separator():
    m = parse_stem("Pol-1992-11-03-EH14879-03-B_Titel")
    assert (m.dokumentdatum, m.uppslag, m.titel) == ("1992-11-03", ("EH14879-03-B",), "Titel")


def test_wpu_dash_separator_with_dash_time():
    m = parse_stem("Pol-1986-03-13-12-00_EAE340-07_Titel")
    assert (m.dokumentdatum, m.uppslag, m.titel) == ("1986-03-13", ("EAE340-07",), "12-00 Titel")


def test_wpu_uppercase_prefix():
    m = parse_stem("POL-1986-03-04_E9979-00-A_X")
    assert (m.dokumentdatum, m.uppslag, m.titel) == ("1986-03-04", ("E9979-00-A",), "X")
