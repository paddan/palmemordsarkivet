"""Vittnesjämförelse — låt en LLM lyfta fram motstridiga uppgifter.

Vanlig RAG syntetiserar bort konflikter; det här är ett "korsförhörsläge" som
i stället letar efter var källorna säger emot varandra om ett ämne. Modulen
bygger prompten deterministiskt ur sökträffarna (testbart); själva LLM-anropet
sker i ``pages/6_Jämförelse.py``.
"""

from __future__ import annotations

COMPARE_SYSTEM_PROMPT = """Du jämför olika källor i Palmemordsarkivet och letar efter var de säger emot varandra.

Regler:
- Svara på svenska.
- Strukturera svaret i två delar: först **Motstridiga uppgifter**, sedan **Överensstämmande uppgifter**.
- För varje punkt: beskriv kort vad respektive källa säger och hänvisa med [Nr X, sida Y].
- Hitta inte på konflikter — om källorna inte motsäger varandra, säg det tydligt.
- Om en uppgift bara framgår av en enda källa, notera att den är obekräftad.
- OCR-fel kan förekomma; flagga om en passage verkar skadad."""


def group_hits_by_source(hits: list[dict]) -> list[dict]:
    """Gruppera träffar per källdokument, bevarad första-sedd-ordning."""
    groups: dict[str, dict] = {}
    for h in hits:
        source = str(h.get("source") or "")
        g = groups.get(source)
        if g is None:
            g = {
                "source": source,
                "nr": h.get("nr"),
                "titel": h.get("titel"),
                "hits": [],
            }
            groups[source] = g
        g["hits"].append(h)
    return list(groups.values())


def build_compare_prompt(topic: str, hits: list[dict]) -> str:
    """Bygg användarmeddelandet: ämne + alla utdrag med källetiketter."""
    blocks = []
    for h in hits:
        nr = h.get("nr", "?")
        page = h.get("page", "?")
        titel = str(h.get("titel") or "")[:60]
        blocks.append(f'[Nr {nr}, sida {page}, "{titel}"]\n{h.get("text", "")}')
    context = "\n\n---\n\n".join(blocks)
    return (
        f"Ämne att jämföra källorna kring: {topic}\n\n"
        f"Utdrag ur arkivet:\n\n{context}"
    )
