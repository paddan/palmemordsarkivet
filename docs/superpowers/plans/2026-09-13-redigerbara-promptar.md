# Redigerbara systempromptar via Admin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gör RAG-flikens och Utredningslägets (MCP) systempromptar redigerbara från Admin → Inställningar, med lagring i `generated/prompts.json` och fallback till dagens hårdkodade default.

**Architecture:** Ny Streamlit-fri modul `src/prompts.py` äger default-texterna och fil-lagringen (load/save + `rag_prompt()`/`mcp_prompt()`-accessorer). `src/rag/ask.py` behåller `SYSTEM_PROMPT`/`MCP_SYSTEM_PROMPT` som alias (bakåtkompatibla importer) men anropar accessorerna vid frågetillfället. Adminsidans Inställningar-flik får en sektion "Promptar" med två textfält, Spara och Återställ.

**Tech Stack:** Python 3, Streamlit, pytest.

**Design (godkänd i chatten 2026-09-13):** Båda promptarna redigerbara som två fält; separat `prompts.json` (inte `admin_settings.json`, vars `save_settings` skriver om hela filen); saknad/trasig fil eller tom nyckel → default.

## Global Constraints

- Alla kommentarer, docstrings och UI-texter på svenska.
- Kör **aldrig** `git commit`/`git push` — användaren committar själv via `/cap`. Planens steg utelämnar därför commits medvetet.
- Testkommando: `.venv/bin/pytest tests/<fil> -v` (eller `.venv/bin/python scripts/test.py` för hela sviten).
- `src/prompts.py` får **inte** importera `rag.ask` (drar in `sentence_transformers` — tung import i adminsidan). Default-texterna flyttas dit; `ask.py` importerar dem.
- `generated/prompts.json` är redan täckt av gitignore via `generated/`.
- UI-mönstret följer `admin_ui.py`: ren logik i testbara hjälpare, Streamlit endast i render-funktioner.

---

### Task 1: `src/prompts.py` — lagring och accessorer

**Files:**
- Create: `src/prompts.py`
- Test: `tests/test_prompts.py`

**Interfaces:**
- Produces (används av Task 2 och 3):
  - `PROMPTS_FILE: Path` — `ROOT / "generated" / "prompts.json"`
  - `SYSTEM_PROMPT: str`, `MCP_SYSTEM_PROMPT: str` — default-texterna (flyttade ordagrant från `src/rag/ask.py`)
  - `load_prompts() -> dict[str, str]` — endast giltiga (icke-tomma sträng) overrides, nycklar `rag`/`mcp`
  - `save_prompts(prompts: Mapping[str, str]) -> None` — skriver bara nycklarna `rag`/`mcp`, hoppar över tomma
  - `rag_prompt() -> str`, `mcp_prompt() -> str` — override eller default

- [ ] **Step 1: Skriv det misslyckade testet**

```python
# tests/test_prompts.py
"""Tester för redigerbara systempromptar (src/prompts.py)."""

from __future__ import annotations

import json

import prompts


def test_default_nar_fil_saknas(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "saknas.json")
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT
    assert prompts.load_prompts() == {}


def test_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "Ny RAG-prompt", "mcp": "Ny MCP-prompt"})
    assert prompts.rag_prompt() == "Ny RAG-prompt"
    assert prompts.mcp_prompt() == "Ny MCP-prompt"


def test_partiell_fil_faller_tillbaka_per_nyckel(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text(json.dumps({"rag": "Bara RAG"}), encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.rag_prompt() == "Bara RAG"
    assert prompts.mcp_prompt() == prompts.MCP_SYSTEM_PROMPT


def test_trasig_fil_faller_tillbaka(tmp_path, monkeypatch) -> None:
    fil = tmp_path / "prompts.json"
    fil.write_text("{inte json", encoding="utf-8")
    monkeypatch.setattr(prompts, "PROMPTS_FILE", fil)
    assert prompts.load_prompts() == {}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT


def test_tomt_varde_raknas_inte(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    prompts.save_prompts({"rag": "  ", "mcp": "OK"})
    lagrat = json.loads((tmp_path / "prompts.json").read_text(encoding="utf-8"))
    assert lagrat == {"mcp": "OK"}
    assert prompts.rag_prompt() == prompts.SYSTEM_PROMPT
```

- [ ] **Step 2: Kör testet och verifiera att det misslyckas**

Run: `.venv/bin/pytest tests/test_prompts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'prompts'`

- [ ] **Step 3: Implementera `src/prompts.py`**

Flytta de två prompt-texterna **ordagrant** från `src/rag/ask.py` (raderna 51–68) hit:

```python
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
... (ordagrant samma innehåll som i src/rag/ask.py i dag) ..."""

MCP_SYSTEM_PROMPT = """Du är en utredningsassistent med tillgång till Palmemordsarkivet via verktyg.
... (ordagrant samma innehåll som i src/rag/ask.py i dag) ..."""

_DEFAULTS = {"rag": SYSTEM_PROMPT, "mcp": MCP_SYSTEM_PROMPT}


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


def save_prompts(prompts: Mapping[str, str]) -> None:
    """Spara overrides. Tomma värden sparas inte (de betyder 'använd default')."""
    giltiga = {
        key: prompts[key].strip()
        for key in ("rag", "mcp")
        if isinstance(prompts.get(key), str) and prompts[key].strip()
    }
    PROMPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROMPTS_FILE.write_text(
        json.dumps(giltiga, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def rag_prompt() -> str:
    """Aktuell RAG-prompt: sparad override eller default."""
    return load_prompts().get("rag", SYSTEM_PROMPT)


def mcp_prompt() -> str:
    """Aktuell utredningsläges-prompt (MCP): sparad override eller default."""
    return load_prompts().get("mcp", MCP_SYSTEM_PROMPT)
```

(OBS: kopiera prompt-texterna exakt från `src/rag/ask.py` rader 51–68 — planen förkortar dem med `...` men koden ska innehålla hela texterna oförändrade.)

- [ ] **Step 4: Kör testet och verifiera att det passerar**

Run: `.venv/bin/pytest tests/test_prompts.py -v`
Expected: PASS (5 tester)

---

### Task 2: Använd accessorerna i `ask.py` och `Utredning.py`

**Files:**
- Modify: `src/rag/ask.py` (rader 51–68, 195, 249 + import)
- Modify: `src/Utredning.py` (rader 45–46, 423, 461, 498, 1095 + import)
- Test: `tests/test_prompts.py` (lägg till alias-test)

**Interfaces:**
- Consumes: `SYSTEM_PROMPT`, `MCP_SYSTEM_PROMPT`, `rag_prompt()`, `mcp_prompt()` från Task 1.
- Produces: `ask.SYSTEM_PROMPT` och `ask.MCP_SYSTEM_PROMPT` finns kvar som alias (oförändrat värde) — inga externa konsumenter bryts.

- [ ] **Step 1: Skriv det misslyckade testet**

Lägg till i `tests/test_prompts.py`:

```python
def test_ask_exporterar_alias_och_anvander_override(tmp_path, monkeypatch) -> None:
    """ask.py ska hämta prompten vid anropstillfället, inte vid import."""
    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    import rag.ask as ask

    assert ask.SYSTEM_PROMPT == prompts.SYSTEM_PROMPT
    assert ask.MCP_SYSTEM_PROMPT == prompts.MCP_SYSTEM_PROMPT
    prompts.save_prompts({"rag": "Overridd RAG"})
    assert ask.rag_prompt() == "Overridd RAG"
```

- [ ] **Step 2: Kör testet och verifiera att det misslyckas**

Run: `.venv/bin/pytest tests/test_prompts.py::test_ask_exporterar_alias_och_anvander_override -v`
Expected: FAIL — `AttributeError: module 'rag.ask' has no attribute 'rag_prompt'`

- [ ] **Step 3: Ändra `src/rag/ask.py`**

Ersätt prompt-konstanterna (rader 51–68) med import + alias:

```python
from prompts import MCP_SYSTEM_PROMPT, SYSTEM_PROMPT, mcp_prompt, rag_prompt
```

(Ta bort de två `"""..."""`-literalerna — de bor nu i `src/prompts.py`. Placera importen bland övriga projektimports enligt filens befintliga sortering.)

Ändra rad 195 i `ask_claude`:

```python
        system_prompt=rag_prompt(),
```

Ändra rad 249 i `run_mcp`:

```python
        system_prompt=mcp_prompt(),
```

- [ ] **Step 4: Ändra `src/Utredning.py`**

I importblocket (rader 45–46): ta bort `MCP_SYSTEM_PROMPT,` och `SYSTEM_PROMPT,` ur `from rag.ask import (...)` och lägg i stället:

```python
from prompts import mcp_prompt, rag_prompt
```

Ändra rad 423: `system_prompt=MCP_SYSTEM_PROMPT,` → `system_prompt=mcp_prompt(),`

Ändra rad 461: `system_prompt=SYSTEM_PROMPT,` → `system_prompt=rag_prompt(),`

Ändra rad 498: `{"role": "system", "content": SYSTEM_PROMPT},` → `{"role": "system", "content": rag_prompt()},`

Ändra rad 1095: `{"role": "system", "content": MCP_SYSTEM_PROMPT}` → `{"role": "system", "content": mcp_prompt()}`

- [ ] **Step 5: Kör testet och verifiera att det passerar**

Run: `.venv/bin/pytest tests/test_prompts.py -v`
Expected: PASS (6 tester)

- [ ] **Step 6: Kör hela testsviten (import-regression)**

Run: `.venv/bin/pytest tests/ -x -q`
Expected: PASS — särskilt att inga andra tester importerar `SYSTEM_PROMPT` från `rag.ask` och bryts.

---

### Task 3: Admin-GUI — sektion "Promptar" i Inställningar-fliken

**Files:**
- Modify: `src/admin_ui.py` (`render_settings_tab`, slutet av funktionen ~rad 528)
- Test: `tests/test_admin_ui.py` (Streamlit AppTest)

**Interfaces:**
- Consumes: `load_prompts`, `save_prompts`, `SYSTEM_PROMPT`, `MCP_SYSTEM_PROMPT` från `src/prompts.py`.
- Produces: UI i Inställningar-fliken; inga nya publika helpers utöver `prompts.py`:s.

- [ ] **Step 1: Skriv det misslyckade testet**

Lägg till i `tests/test_admin_ui.py` (följ filens befintliga AppTest-mönster — se `_run_admin_app`-helpern om den finns, annars testa den rena lagringslogiken):

```python
def test_prompts_roundtrip_via_helpers(tmp_path, monkeypatch) -> None:
    """Admin sparar/läser via samma helpers som Utredning använder."""
    import prompts

    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    admin_ui.save_prompts_form({"rag": "Ny RAG", "mcp": "Ny MCP"})
    assert admin_ui.load_prompts_form() == {"rag": "Ny RAG", "mcp": "Ny MCP"}


def test_prompts_form_reset_ger_tomma_overrides(tmp_path, monkeypatch) -> None:
    import prompts

    monkeypatch.setattr(prompts, "PROMPTS_FILE", tmp_path / "prompts.json")
    admin_ui.save_prompts_form({"rag": "Ny RAG", "mcp": "Ny MCP"})
    admin_ui.reset_prompts_form()
    assert admin_ui.load_prompts_form() == {}
```

- [ ] **Step 2: Kör testet och verifiera att det misslyckas**

Run: `.venv/bin/pytest tests/test_admin_ui.py -k prompts -v`
Expected: FAIL — `AttributeError: module 'admin_ui' has no attribute 'save_prompts_form'`

- [ ] **Step 3: Implementera helpers + UI i `src/admin_ui.py`**

Nya rena helpers (nära övriga settings-helpers, ~efter `apply_debug_logging`):

```python
import prompts


def load_prompts_form() -> dict[str, str]:
    """Sparade prompt-overrides (tomma dict om inget sparats)."""
    return prompts.load_prompts()


def save_prompts_form(varden: Mapping[str, str]) -> None:
    """Spara prompt-overrides från Inställningar-formuläret."""
    prompts.save_prompts(varden)


def reset_prompts_form() -> None:
    """Återställ båda promptarna till default genom att rensa alla overrides."""
    prompts.save_prompts({})
```

I `render_settings_tab()`, efter befintlig "Spara inställningar"-knapp:

```python
    st.divider()
    st.subheader("Promptar")
    st.caption(
        "Systempromptar för Utredning-sidans två lägen. Lämnas ett fält tomt "
        "används standardtexten. Ändringar gäller direkt vid nästa fråga."
    )
    sparade = load_prompts_form()
    rag_text = st.text_area(
        "Fråga arkivet (RAG)",
        value=sparade.get("rag", prompts.SYSTEM_PROMPT),
        height=200,
        key="prompt_rag",
        help="Systemprompten som styr svaren i RAG-fliken.",
    )
    mcp_text = st.text_area(
        "Utredningsläge (MCP)",
        value=sparade.get("mcp", prompts.MCP_SYSTEM_PROMPT),
        height=200,
        key="prompt_mcp",
        help="Systemprompten som styr utredningsassistenten i MCP-fliken.",
    )
    col_save, col_reset = st.columns(2)
    if col_save.button("Spara promptar", key="prompts_save"):
        save_prompts_form({"rag": rag_text, "mcp": mcp_text})
        st.success("Promptar sparade.")
    if col_reset.button("Återställ till standard", key="prompts_reset"):
        reset_prompts_form()
        st.rerun()
```

(OBS: `reset` → `st.rerun()` så att textfälten ritas om med defaultvärdena; ett tomt sparat fält visar default eftersom `sparade.get(...)` faller tillbaka.)

- [ ] **Step 4: Kör testet och verifiera att det passerar**

Run: `.venv/bin/pytest tests/test_admin_ui.py -k prompts -v`
Expected: PASS (2 tester)

- [ ] **Step 5: Manuell rökning**

Run: `.venv/bin/python scripts/web.py`
Verifiera: Admin → Inställningar visar "Promptar"-sektionen; ändra RAG-prompten, spara, ställ en fråga i Utredning — det nya beteendet syns; Återställ ger tillbaka standard.

---

### Task 4: Dokumentation i samma ändring

**Files:**
- Modify: `docs/teknisk-referens.md`
- Modify: `AGENTS.md`

**Interfaces:** Inga (ren dokumentation).

- [ ] **Step 1: Uppdatera `docs/teknisk-referens.md`**

Lägg till i lämpligt avsnitt (UI/admin):

```markdown
## Redigerbara promptar

Systempromptarna för Utredning-sidans båda lägen (RAG och Utredningsläge/MCP)
kan ändras under Admin → Inställningar → Promptar. Overrides sparas i
`generated/prompts.json` (nycklarna `rag` och `mcp`); saknas filen eller en
nyckel används standardtexterna i `src/prompts.py` (`SYSTEM_PROMPT` /
`MCP_SYSTEM_PROMPT`). Ändringar gäller direkt vid nästa fråga — ingen omstart
krävs. Tomt fält eller **Återställ till standard** tar bort overriden.
```

- [ ] **Step 2: Uppdatera `AGENTS.md`**

I filöversikten under `src/`, lägg till raden:

```
  prompts.py           # Redigerbara systempromptar (RAG/MCP) med overrides i generated/prompts.json
```

I "Non-obvious Design Decisions", lägg till ett kort stycke:

```markdown
**Redigerbara promptar (`src/prompts.py`)**: Systempromptarna för RAG- och
MCP-läget kan ändras i Admin → Inställningar och lagras i
`generated/prompts.json`. `rag_prompt()`/`mcp_prompt()` anropas vid
frågetillfället (aldrig cachat vid import), så ändringar gäller direkt.
`SYSTEM_PROMPT`/`MCP_SYSTEM_PROMPT` i `prompts.py` är default och fallback;
modulen får inte importera `rag.ask` (tung import för adminsidan).
```

- [ ] **Step 3: Verifiera helheten**

Run: `.venv/bin/python scripts/test.py --static`
Expected: PASS (pytest + ruff/mypy)
