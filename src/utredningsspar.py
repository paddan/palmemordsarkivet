"""Konversationens spårlista för utredningsläget.

Ren logik utan Streamlit-beroenden och utan sidoeffekter utöver sina argument:
listan ägs av anroparen (sidan i ``session_state["leads"]``) och skickas in.

Ett spår har formen som ``mcp_server.normalize_leads`` returnerar
(``title``, ``why``, ``question``, ``sources``) plus ``id`` (löpnummer), ``turn``
(index i ``chat_history``) och ``status``. Sidan loggar själv vad som filtreras
bort — den här modulen loggar inget.
"""

from __future__ import annotations

STATUS_FORESLAGEN = "föreslagen"
STATUS_GRAVD = "grävd"

SUMMARY_HEADER = "[Spår hittills i konversationen]"

# Tecknen som avslutar en rubrik men inte hör till dess identitet.
_TRAILING_PUNCTUATION = ".?!:;"


def title_key(title: str) -> str:
    """Normalisera en rubrik så att samma spår får samma nyckel.

    Gemener, kollapsade blanksteg och utan avslutande ``.?!:;`` — så räknas
    "Vittnet vid Tunnelgatan." och "  vittnet vid  TUNNELGATAN " som samma spår.
    """
    normaliserad = " ".join(str(title or "").split()).lower()
    return normaliserad.rstrip(_TRAILING_PUNCTUATION).strip()


def _source_is_known(source: str, known_sources: set[str]) -> bool:
    """Avgör om ett beläggs ``source`` har setts i ett verktygssvar.

    Verktygssvarens ``source`` kan bära ``.txt`` eller inte beroende på väg, så
    båda formerna jämförs mot ``known_sources``.
    """
    if source in known_sources:
        return True
    if source.endswith(".txt"):
        return source[: -len(".txt")] in known_sources
    return f"{source}.txt" in known_sources


def _known_evidence(sources: list[dict] | None, known_sources: set[str]) -> list[dict]:
    """Behåll bara belägg vars ``source`` faktiskt lästs i konversationen."""
    behallna: list[dict] = []
    for belagg in sources or []:
        source = str((belagg or {}).get("source") or "")
        if source and _source_is_known(source, known_sources):
            behallna.append(dict(belagg))
    return behallna


def get_lead(store: list[dict], lead_id: int) -> dict | None:
    """Hämta spåret med ``lead_id``, eller ``None`` om det inte finns."""
    for spar in store:
        if spar.get("id") == lead_id:
            return spar
    return None


def register_leads(
    store: list[dict],
    proposed: list[dict],
    turn: int,
    known_sources: set[str],
) -> list[int]:
    """Lägg turens föreslagna spår i ``store`` och returnera deras id:n.

    Belägg som inte setts i konversationen tas bort; ett spår som då saknar
    belägg visas inte alls. En rubrik som redan finns i listan räknas som samma
    spår: id:t återanvänds och statusen rörs inte. Nya spår får status
    ``föreslagen``. Id:na returneras i turens ordning, utan dubbletter.
    """
    keys_i_store = {title_key(spar.get("title", "")): spar for spar in store}
    sedda_id: set[int] = set()
    ids: list[int] = []

    for spar in proposed or []:
        belagg = _known_evidence(spar.get("sources"), known_sources)
        if not belagg:
            continue

        nyckel = title_key(spar.get("title", ""))
        befintligt = keys_i_store.get(nyckel)
        if befintligt is not None:
            lead_id = int(befintligt["id"])
        else:
            lead_id = len(store) + 1
            post = {"id": lead_id, "turn": turn, "status": STATUS_FORESLAGEN}
            post.update(spar)
            post["sources"] = belagg
            store.append(post)
            keys_i_store[nyckel] = post

        if lead_id not in sedda_id:
            sedda_id.add(lead_id)
            ids.append(lead_id)

    return ids


def mark_dug(store: list[dict], lead_id: int) -> dict | None:
    """Markera spåret som grävt och returnera det."""
    spar = get_lead(store, lead_id)
    if spar is not None:
        spar["status"] = STATUS_GRAVD
    return spar


def summary(store: list[dict]) -> str:
    """Kort sammanfattning av listan som skickas med nästa fråga.

    Bara rubrik och status — den går till modellen, inte till chatten. Tom
    sträng när listan är tom, så anroparen kan utelämna den helt.
    """
    if not store:
        return ""
    rader = [SUMMARY_HEADER]
    for nummer, spar in enumerate(store, start=1):
        rader.append(f"{nummer}. ({spar.get('status', '')}) {spar.get('title', '')}")
    return "\n".join(rader)
