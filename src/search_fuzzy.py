"""OCR-tolerant fuzzy-sökning över chunk-texter.

Den vanliga hybridsökningen behöver tantivy för exakt BM25-matchning; saknas
det faller den tillbaka på ren vektorsökning, och exakta namn som OCR:en
felstavat (``Paine`` för ``Palme``) blir svåra att hitta. Den här modulen ger
en skiftlägesokänslig, edit-distance-baserad sökning som fångar sådana
felstavningar.

Den bygger ett token-index en gång (cachas av UI:t — korpusen är ~160k chunks)
och matchar frågans tokens mot indexets vokabulär med ``difflib``. Ren logik,
ingen Streamlit, inga tunga beroenden.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher, get_close_matches

# Bokstäver (inkl. svenska) och siffror; allt annat är avgränsare.
_TOKEN_RE = re.compile(r"[0-9a-zåäöéèüA-ZÅÄÖÉÈÜ]+")

# Hur många vokabulärträffar per frågetoken vi tar med (recall vs. fart).
_MAX_VOCAB_MATCHES = 200


def tokenize(text: str | None) -> list[str]:
    """Dela upp text i gemena tokens av bokstäver/siffror."""
    if not text:
        return []
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text)]


def build_index(rows: list[dict]) -> dict:
    """Bygg token-index över ``rows`` (dictar med ``text``).

    Returnerar ``{"vocab": [...], "postings": {token: [radpositioner]}}``.
    Positionerna pekar in i samma ``rows``-lista som skickas till
    :func:`fuzzy_search`."""
    postings: dict[str, list[int]] = {}
    for pos, row in enumerate(rows):
        seen: set[str] = set()
        for tok in tokenize(row.get("text")):
            if tok in seen:
                continue
            seen.add(tok)
            postings.setdefault(tok, []).append(pos)
    return {"vocab": list(postings.keys()), "postings": postings}


def fuzzy_search(
    rows: list[dict],
    query: str,
    *,
    index: dict,
    top_k: int = 20,
    threshold: float = 0.75,
) -> list[dict]:
    """Sök ``rows`` fuzzy efter ``query``; returnera top_k bäst matchande.

    Varje token i frågan matchas mot indexets vokabulär; en rad får poängen
    från den bästa token-likheten (exakt matchning = 1.0 hamnar överst). Rader
    under ``threshold`` tas inte med. Träffarna får ett ``_fuzzy``-fält."""
    vocab = index["vocab"]
    postings = index["postings"]
    scores: dict[int, float] = {}
    for qt in dict.fromkeys(tokenize(query)):  # unika, bevarad ordning
        for vt in get_close_matches(qt, vocab, n=_MAX_VOCAB_MATCHES, cutoff=threshold):
            ratio = SequenceMatcher(None, qt, vt).ratio()
            for pos in postings.get(vt, ()):
                if ratio > scores.get(pos, 0.0):
                    scores[pos] = ratio
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    out: list[dict] = []
    for pos, score in ranked[:top_k]:
        out.append({**rows[pos], "_fuzzy": round(score, 3)})
    return out
