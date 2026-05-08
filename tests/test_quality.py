"""Tester för quality.score_text."""

from __future__ import annotations

from quality import score_text


GOOD_TEXT = (
    "Detta är en helt vanlig svensk text som ska få en hög poäng. "
    "Den innehåller meningar med rimliga ord, kommatecken och punkter. "
    "Förhöret hölls i Stockholm den tjugoåttonde februari nittonhundraåttiosex. "
    "Vittnet berättade lugnt och sammanhängande om vad som hade hänt. "
    "Polisen antecknade noggrant alla detaljer som framkom under samtalet."
)

JUNK_TEXT = (
    "@#$%^&*() ~~~ |||| 1a 2b 3c 4d 5e 6f 7g 8h 9i 0j x y z q w "
    "ababab1 cd2 ef3 gh4 ij5 kl6 mn7 op8 qr9 st0 §§§ ¤¤¤ ¶¶¶"
)


def test_perfect_text_high_score() -> None:
    s = score_text(GOOD_TEXT, use_hunspell=False)
    assert s["score"] > 80, s


def test_junk_text_low_score() -> None:
    s = score_text(JUNK_TEXT, use_hunspell=False)
    assert s["score"] < 20, s


def test_empty_text() -> None:
    s = score_text("", use_hunspell=False)
    assert s["score"] == 0.0
