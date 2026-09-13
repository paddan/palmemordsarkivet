"""Redigerbara systempromptar för Utredning-sidans två lägen.

Admin → Inställningar kan spara overrides i generated/prompts.json. Saknas
filen, är den trasig eller saknar en nyckel används default-texterna nedan.
Modulen är avsiktligt beroendefri (ingen Streamlit, inga tunga imports) så att
både rag/ask.py och admin_ui.py kan importera den billigt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPTS_FILE = ROOT / "generated" / "prompts.json"

SYSTEM_PROMPT = """Du svarar på frågor om Palmemordsarkivet baserat på de utdrag användaren ger dig.

Regler:
- Svara på svenska.
- Stötta varje påstående med en källhänvisning på formen [Nr X, sida Y].
- Om svaret inte framgår av utdragen, säg "framgår inte av materialet" — gissa aldrig.
- Citera ordagrant när det är klargörande, men håll citaten korta.
- OCR-fel kan förekomma. Säg till om en passage verkar vara skadad eller obegriplig."""

MCP_SYSTEM_PROMPT = """Du är en utredningsassistent med tillgång till Palmemordsarkivet via verktyg.

Regler:
- Svara på svenska.
- Anropa verktygen direkt — beskriv inte bara att du *tänker* söka och avsluta sedan. Säger du att du ska söka eller kolla något, gör det i samma svar.
- Använd search_archive för att hitta relevant material. Sök gärna flera gånger med olika termer.
- Använd get_page för att läsa mer kontext kring ett intressant stycke.
- Stötta varje påstående med [Nr X, sida Y].
- Om du inte hittar svar efter rimliga sökningar, säg det — gissa aldrig.
- OCR-fel kan förekomma i materialet."""


def load_prompts() -> dict[str, str]:
    """Läs sparade overrides. Tolerant mot saknad/trasig fil och konstiga värden."""
    try:
        stored = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        key: stored[key]
        for key in ("rag", "mcp")
        if isinstance(stored.get(key), str) and stored[key].strip()
    }


def save_prompts(varden: Mapping[str, str]) -> None:
    """Spara overrides. Tomma värden sparas inte (de betyder 'använd default')."""
    giltiga = {
        nyckel: varden[nyckel]
        for nyckel in ("rag", "mcp")
        if isinstance(varden.get(nyckel), str) and varden[nyckel].strip()
    }
    PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Skriv via tempfil + replace: en krasch eller två samtidiga adminsessioner får
    # aldrig lämna en halv fil — load_prompts skulle då falla tillbaka på default
    # och tyst tappa båda promptarna.
    tmp = PROMPTS_FILE.with_name(PROMPTS_FILE.name + ".tmp")
    tmp.write_text(json.dumps(giltiga, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(PROMPTS_FILE)


def rag_prompt() -> str:
    """Aktuell RAG-prompt: sparad override eller default."""
    return load_prompts().get("rag", SYSTEM_PROMPT)


def mcp_prompt() -> str:
    """Aktuell utredningsläges-prompt (MCP): sparad override eller default."""
    return load_prompts().get("mcp", MCP_SYSTEM_PROMPT)
