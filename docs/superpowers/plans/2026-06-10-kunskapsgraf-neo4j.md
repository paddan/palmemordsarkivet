# Kunskapsgraf (Neo4j) — Implementationsplan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extrahera personer/platser/organisationer + relationer ur arkivtexten med Claude Haiku och ladda dem som en utforskningsbar kunskapsgraf i Neo4j — utan att röra LanceDB-RAG:en.

**Architecture:** Tvålagersmodell. Nytt extraktionssteg (`src/graph/extract_entities.py`) kör per-sida LLM-extraktion (samma SDK-mönster som `llm_correct.py`), lagrar JSON-payloads idempotent i `state.db`-tabellen `doc_entities`. En separat loader (`src/graph/load_neo4j.py`) normaliserar entiteter och MERGE:ar batchvis till Neo4j (Docker). Grafen delar nyckel (`pdf_stem`) med LanceDB så lagren kan korsas i Python senare.

**Tech Stack:** Python 3.11, `claude_agent_sdk` (Haiku `claude-haiku-4-5-20251001`), SQLite (`src/db.py`), `neo4j`-drivern (officiell), Neo4j 5 i Docker, pytest.

**Kostnadsuppskattning:** ~47 000 sidor, varav bildsidor (<100 alfanumeriska tecken) hoppas över → ~40 000 Haiku-anrop à ~800 in-tokens / ~200 ut-tokens ≈ $60–120. Körs inkrementellt och kan avbrytas/återupptas.

**Projektregler (CLAUDE.md):** ingen `git commit`/`git push` utan att användaren kör `/cap` — commit-stegen nedan betyder "stanna och låt användaren köra /cap", inte att köra git själv. README + CLAUDE.md uppdateras i samma ändring.

---

## Neo4j-schema (referens för alla tasks)

```
(:Dokument {stem, nr, titel})
(:Person {norm, namn})        (:Plats {norm, namn})        (:Organisation {norm, namn})

(:Dokument)-[:NÄMNER {sida}]->(:Person|:Plats|:Organisation)
(:Person|:Plats|:Organisation)-[:RELATERAR {typ, stem, sida}]->(:Person|:Plats|:Organisation)
```

- `norm` = normaliserat namn (gemener, kollapsad whitespace) — unikhetsnyckel per label.
- `namn` = första observerade skrivningen (visningsnamn).
- Constraints: UNIQUE på `Dokument.stem` samt `norm` per entitetslabel.

## Extraktionsformat (LLM-svar, per sida)

```json
{"entiteter": [{"typ": "person", "namn": "Stig Engström"}],
 "relationer": [{"fran": "Stig Engström", "typ": "förhördes av", "till": "Thomas Pettersson"}]}
```

---

### Task 1: `doc_entities`-tabell i state.db

**Files:**
- Modify: `src/db.py` (SCHEMA_SQL ~rad 115 + nya funktioner efter `wpu_decided`)
- Test: `tests/test_db.py` (append)

- [ ] **Step 1: Skriv failande test**

Lägg sist i `tests/test_db.py`:

```python
def test_doc_entities_roundtrip(tmp_path):
    from db import record_doc_entities, doc_entities_extracted, iter_doc_entities
    conn = _fresh(tmp_path)
    payload = {"entiteter": [{"typ": "person", "namn": "Stig Engström"}],
               "relationer": []}
    assert not doc_entities_extracted(conn, "doc", 1)
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload=payload, model="haiku-test")
    assert doc_entities_extracted(conn, "doc", 1)
    rows = list(iter_doc_entities(conn))
    assert len(rows) == 1
    assert rows[0]["pdf_stem"] == "doc"
    assert rows[0]["payload"]["entiteter"][0]["namn"] == "Stig Engström"


def test_record_doc_entities_is_upsert(tmp_path):
    from db import record_doc_entities, iter_doc_entities
    conn = _fresh(tmp_path)
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload={"entiteter": [], "relationer": []}, model="a")
    record_doc_entities(conn, pdf_stem="doc", page_num=1,
                        payload={"entiteter": [], "relationer": []}, model="b")
    rows = list(iter_doc_entities(conn))
    assert len(rows) == 1
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_db.py -q -k doc_entities`
Förväntat: FAIL med `ImportError: cannot import name 'record_doc_entities'`

- [ ] **Step 3: Implementera**

I `src/db.py`, lägg sist i `SCHEMA_SQL` (före den avslutande `"""`):

```sql
CREATE TABLE IF NOT EXISTS doc_entities (
    pdf_stem     TEXT NOT NULL,
    page_num     INTEGER NOT NULL,
    payload      TEXT NOT NULL,
    model        TEXT,
    extracted_at TEXT NOT NULL,
    PRIMARY KEY (pdf_stem, page_num)
);
```

Lägg nya funktioner efter `wpu_decided` (följ befintlig sektionsstil):

```python
# --- doc_entities (kunskapsgraf) ---------------------------------------

def record_doc_entities(
    conn: sqlite3.Connection, *,
    pdf_stem: str, page_num: int, payload: dict, model: str,
) -> None:
    """UPSERT av extraherade entiteter/relationer för en sida. payload som JSON."""
    conn.execute(
        """INSERT INTO doc_entities(pdf_stem, page_num, payload, model, extracted_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(pdf_stem, page_num) DO UPDATE SET
               payload=excluded.payload, model=excluded.model,
               extracted_at=excluded.extracted_at""",
        (pdf_stem, page_num, json.dumps(payload, ensure_ascii=False), model, now()),
    )
    conn.commit()


def doc_entities_extracted(
    conn: sqlite3.Connection, pdf_stem: str, page_num: int
) -> bool:
    """Sann om sidan redan entitetsextraherats."""
    return conn.execute(
        "SELECT 1 FROM doc_entities WHERE pdf_stem=? AND page_num=?",
        (pdf_stem, page_num),
    ).fetchone() is not None


def iter_doc_entities(conn: sqlite3.Connection):
    """Generator över alla extraktioner: dictar med pdf_stem, page_num, payload (parsad)."""
    for row in conn.execute(
        "SELECT pdf_stem, page_num, payload FROM doc_entities ORDER BY pdf_stem, page_num"
    ):
        yield {"pdf_stem": row["pdf_stem"], "page_num": row["page_num"],
               "payload": json.loads(row["payload"])}
```

(`json` och `now` är redan importerade/definierade i db.py.)

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_db.py -q`
Förväntat: alla PASS

- [ ] **Step 5: Checkpoint** — klart för commit (användaren kör `/cap` när hen vill).

---

### Task 2: `parse_extraction` — robust JSON-parsning av LLM-svar

**Files:**
- Create: `src/graph/__init__.py` (tom fil)
- Create: `src/graph/extract_entities.py`
- Test: `tests/test_extract_entities.py`

- [ ] **Step 1: Skriv failande test**

Skapa `tests/test_extract_entities.py`:

```python
"""Tester för extract_entities — parsning och sidurval."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.extract_entities import parse_extraction


def test_parse_valid_json() -> None:
    raw = ('{"entiteter": [{"typ": "person", "namn": "Stig Engström"}],'
           ' "relationer": [{"fran": "Stig Engström", "typ": "sågs vid",'
           ' "till": "Dekorima"}]}')
    out = parse_extraction(raw)
    assert out["entiteter"][0]["namn"] == "Stig Engström"
    assert out["relationer"][0]["typ"] == "sågs vid"


def test_parse_json_in_markdown_fence() -> None:
    raw = 'Här är resultatet:\n```json\n{"entiteter": [], "relationer": []}\n```'
    assert parse_extraction(raw) == {"entiteter": [], "relationer": []}


def test_parse_garbage_returns_empty() -> None:
    assert parse_extraction("ingen json här") == {"entiteter": [], "relationer": []}
    assert parse_extraction("") == {"entiteter": [], "relationer": []}
    assert parse_extraction('{"entiteter": [trasig') == {"entiteter": [], "relationer": []}


def test_parse_filters_invalid_entries() -> None:
    raw = ('{"entiteter": [{"typ": "person", "namn": "OK"},'
           ' {"typ": "djur", "namn": "Hund"},'
           ' {"typ": "person", "namn": ""}],'
           ' "relationer": [{"fran": "OK", "typ": "", "till": "X"},'
           ' {"fran": "OK", "typ": "känner", "till": "X"}]}')
    out = parse_extraction(raw)
    assert [e["namn"] for e in out["entiteter"]] == ["OK"]
    assert len(out["relationer"]) == 1
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_extract_entities.py -q`
Förväntat: FAIL med `ModuleNotFoundError: No module named 'graph'`

- [ ] **Step 3: Implementera**

Skapa tom `src/graph/__init__.py` samt `src/graph/extract_entities.py`:

```python
"""LLM-baserad entitets- och relationsextraktion för kunskapsgrafen.

Läser generated/text/<stem>.txt sida för sida (\f-separerade), skickar varje
sida till Claude Haiku och lagrar parsad JSON i state.db-tabellen
``doc_entities`` (idempotent per pdf_stem+sida). Ladda till Neo4j med
``load_neo4j.py`` efteråt.

Kör:
    python -m graph.extract_entities [--limit 5] [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402

MODEL = "claude-haiku-4-5-20251001"
MIN_PAGE_ALNUM = 100  # sidor med färre alfanumeriska tecken hoppas över (bildsidor)

_SYSTEM = """\
Du extraherar entiteter och relationer ur svenska polis- och utredningsdokument
(Palmeutredningen). Returnera ENBART giltig JSON på exakt denna form:
{"entiteter": [{"typ": "person|plats|organisation", "namn": "..."}],
 "relationer": [{"fran": "...", "typ": "...", "till": "..."}]}
Regler:
- "namn" skrivs som i texten men utan uppenbart OCR-skräp.
- "relationer" bara mellan namn som finns i entitetslistan; "typ" är en kort
  svensk fras, t.ex. "förhördes av", "sågs vid", "arbetade på".
- Hoppa över oläsliga/osäkra namn. Hittar du inget: {"entiteter": [], "relationer": []}."""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_VALID_TYPES = ("person", "plats", "organisation")


def parse_extraction(raw: str) -> dict:
    """Plocka ut och validera JSON ur ett LLM-svar.

    Returnerar alltid {"entiteter": [...], "relationer": [...]} — trasig eller
    saknad JSON ger tomma listor (sidan räknas ändå som extraherad)."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return {"entiteter": [], "relationer": []}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"entiteter": [], "relationer": []}
    ents = [
        {"typ": e["typ"], "namn": e["namn"].strip()}
        for e in data.get("entiteter", [])
        if isinstance(e, dict) and e.get("typ") in _VALID_TYPES
        and isinstance(e.get("namn"), str) and e["namn"].strip()
    ]
    rels = [
        {"fran": r["fran"].strip(), "typ": r["typ"].strip(), "till": r["till"].strip()}
        for r in data.get("relationer", [])
        if isinstance(r, dict)
        and all(isinstance(r.get(k), str) and r[k].strip() for k in ("fran", "typ", "till"))
    ]
    return {"entiteter": ents, "relationer": rels}
```

(Resten av modulen fylls på i Task 3–4.)

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_extract_entities.py -q`
Förväntat: 4 PASS

---

### Task 3: Sidurval — `select_pages` (idempotens + bildsidefilter)

**Files:**
- Modify: `src/graph/extract_entities.py`
- Test: `tests/test_extract_entities.py` (append)

- [ ] **Step 1: Skriv failande test**

Lägg sist i `tests/test_extract_entities.py`:

```python
def _conn(tmp_path):
    import db
    conn = db.connect(tmp_path / "state.db")
    db.init_schema(conn)
    return conn


def test_select_pages_skips_extracted_and_image_pages(tmp_path) -> None:
    import db
    from graph.extract_entities import select_pages
    text = "Förhör med Stig Engström hölls på Norrmalmspolisen " * 5
    pages = [text, "x", text, text]  # sida 2 = bildsida (<100 alnum)
    conn = _conn(tmp_path)
    db.record_doc_entities(conn, pdf_stem="doc", page_num=3,
                           payload={"entiteter": [], "relationer": []}, model="t")
    out = select_pages(conn, "doc", pages)
    assert [p for p, _ in out] == [1, 4]


def test_select_pages_empty_doc(tmp_path) -> None:
    from graph.extract_entities import select_pages
    assert select_pages(_conn(tmp_path), "doc", [""]) == []
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_extract_entities.py -q -k select_pages`
Förväntat: FAIL med `ImportError: cannot import name 'select_pages'`

- [ ] **Step 3: Implementera**

Lägg i `src/graph/extract_entities.py` efter `parse_extraction`:

```python
def select_pages(
    conn, pdf_stem: str, pages: list[str]
) -> list[tuple[int, str]]:
    """Returnera (sidnr, text) för sidor som ska extraheras.

    Hoppar: redan extraherade (doc_entities) och bildsidor
    (< MIN_PAGE_ALNUM alfanumeriska tecken)."""
    out: list[tuple[int, str]] = []
    for idx, text in enumerate(pages, start=1):
        if sum(1 for c in text if c.isalnum()) < MIN_PAGE_ALNUM:
            continue
        if state_db.doc_entities_extracted(conn, pdf_stem, idx):
            continue
        out.append((idx, text))
    return out
```

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_extract_entities.py -q`
Förväntat: alla PASS

- [ ] **Step 5: Checkpoint** — lämpligt /cap-läge.

---

### Task 4: LLM-anrop + main-loop

**Files:**
- Modify: `src/graph/extract_entities.py`
- Test: `tests/test_extract_entities.py` (append)

- [ ] **Step 1: Skriv failande test** (mockat LLM-anrop, mönster från `test_llm_correct.py`)

```python
def test_extract_doc_stores_payload(tmp_path, monkeypatch) -> None:
    import asyncio
    import db
    from graph import extract_entities as ee

    async def fake_llm(text: str) -> str:
        return '{"entiteter": [{"typ": "person", "namn": "Lisbeth Palme"}], "relationer": []}'

    monkeypatch.setattr(ee, "_call_llm", fake_llm)
    txt_dir = tmp_path / "text"
    txt_dir.mkdir()
    body = "Vittnesförhör med Lisbeth Palme angående händelsen vid Dekorima " * 4
    (txt_dir / "doc.txt").write_text(body + "\f" + body, encoding="utf-8")
    conn = _conn(tmp_path)

    n = asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", dry_run=False))
    assert n == 2
    rows = list(db.iter_doc_entities(conn))
    assert len(rows) == 2
    assert rows[0]["payload"]["entiteter"][0]["namn"] == "Lisbeth Palme"
    # Idempotens: andra körningen gör inget
    assert asyncio.run(ee.extract_doc(conn, txt_dir / "doc.txt", dry_run=False)) == 0
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_extract_entities.py -q -k stores_payload`
Förväntat: FAIL med `AttributeError: ... has no attribute '_call_llm'`

- [ ] **Step 3: Implementera**

Lägg i `src/graph/extract_entities.py`:

```python
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage, ClaudeAgentOptions, TextBlock, query,
)


async def _call_llm(text: str) -> str:
    """Skicka en sidtext till Haiku, returnera råsvaret."""
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=MODEL,
        allowed_tools=[],
        max_turns=1,
        setting_sources=[],
    )
    parts: list[str] = []
    async for msg in query(prompt=text, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
    return "".join(parts)


async def extract_doc(conn, txt_path: Path, *, dry_run: bool) -> int:
    """Extrahera alla kvarvarande sidor i ett dokument. Returnerar antal sidor."""
    stem = txt_path.stem
    pages = txt_path.read_text(encoding="utf-8", errors="replace").split("\f")
    todo = select_pages(conn, stem, pages)
    for page_num, text in todo:
        if dry_run:
            continue
        payload = parse_extraction(await _call_llm(text[:6000]))
        state_db.record_doc_entities(
            conn, pdf_stem=stem, page_num=page_num, payload=payload, model=MODEL,
        )
    return len(todo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-dir", default=os.environ.get(
        "TEXT_DIR", str(ROOT / "generated" / "text")))
    ap.add_argument("--limit", type=int, help="max antal dokument (testkörning)")
    ap.add_argument("--dry-run", action="store_true",
                    help="visa antal sidor utan att anropa LLM")
    args = ap.parse_args()

    if not args.dry_run and not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
                                 or os.environ.get("ANTHROPIC_API_KEY")):
        print("Sätt CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY.", file=sys.stderr)
        return 1

    text_dir = Path(args.text_dir)
    files = sorted(text_dir.glob("*.txt"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"Inga .txt-filer i {text_dir}/", file=sys.stderr)
        return 1

    conn = state_db.connect()
    state_db.init_schema(conn)
    t0 = time.monotonic()
    total_pages = 0
    for i, f in enumerate(files, 1):
        n = asyncio.run(extract_doc(conn, f, dry_run=args.dry_run))
        total_pages += n
        if n:
            elapsed = time.monotonic() - t0
            rate = total_pages / elapsed if elapsed else 0
            print(f"  [{i}/{len(files)}] {f.stem[:60]:60s} +{n} sidor "
                  f"({rate:.1f} sidor/s)", flush=True)
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}Klart: {total_pages} sidor i {len(files)} dokument.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_extract_entities.py tests/test_db.py -q`
Förväntat: alla PASS

- [ ] **Step 5: Checkpoint** — lämpligt /cap-läge.

---

### Task 5: Wrapper `extract_entities.sh` + dry-run-rök

**Files:**
- Create: `extract_entities.sh`

- [ ] **Step 1: Skapa wrappern** (samma stil som `llm_correct.sh`)

```bash
#!/bin/bash
# Entitets-/relationsextraktion för kunskapsgrafen (Claude Haiku).
# Flaggor vidarebefordras till src/graph/extract_entities.py (--limit, --dry-run, ...).
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m graph.extract_entities "$@"
```

Kör: `chmod +x extract_entities.sh`

- [ ] **Step 2: Rök-test (ingen LLM-kostnad)**

Kör: `./extract_entities.sh --dry-run --limit 3`
Förväntat: `[dry-run] Klart: N sidor i 3 dokument.` (N > 0)

---

### Task 6: Neo4j i Docker

**Files:**
- Create: `neo4j/docker-compose.yml`
- Modify: `.gitignore` (lägg till `neo4j/data/`)

- [ ] **Step 1: Skapa compose-filen**

```yaml
services:
  neo4j:
    image: neo4j:5
    container_name: palme-neo4j
    restart: unless-stopped
    ports:
      - "7474:7474"   # Browser-UI
      - "7687:7687"   # Bolt
    environment:
      NEO4J_AUTH: "neo4j/${NEO4J_PASSWORD:?sätt NEO4J_PASSWORD}"
      NEO4J_server_memory_heap_max__size: "2G"
    volumes:
      - ./data:/data
```

- [ ] **Step 2: Lägg till i `.gitignore`**

```
neo4j/data/
```

- [ ] **Step 3: Starta och verifiera** (manuellt steg)

```bash
cd neo4j && NEO4J_PASSWORD=välj-ett-lösenord docker compose up -d
curl -s http://localhost:7474 | head -3   # ska ge JSON med neo4j-version
```

Browser-UI: http://localhost:7474 (login `neo4j` / lösenordet).

- [ ] **Step 4: Installera Python-drivern**

Kör: `.venv/bin/pip install neo4j` och lägg till `neo4j` i `pyproject.toml` under en ny optional-extra `graph` (samma mönster som `webui`-extran).

---

### Task 7: `normalize_name` + `build_rows` (ren transformlogik)

**Files:**
- Create: `src/graph/load_neo4j.py`
- Test: `tests/test_load_neo4j.py`

- [ ] **Step 1: Skriv failande test**

Skapa `tests/test_load_neo4j.py`:

```python
"""Tester för load_neo4j — normalisering och radbygge (ingen Neo4j krävs)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from graph.load_neo4j import build_rows, normalize_name


def test_normalize_name_collapses_case_and_whitespace() -> None:
    assert normalize_name("  Stig   ENGSTRÖM ") == "stig engström"


def test_build_rows_mentions_and_relations() -> None:
    payload = {
        "entiteter": [
            {"typ": "person", "namn": "Stig Engström"},
            {"typ": "plats", "namn": "Dekorima"},
        ],
        "relationer": [
            {"fran": "Stig Engström", "typ": "sågs vid", "till": "Dekorima"},
            {"fran": "Okänd Person", "typ": "känner", "till": "Dekorima"},  # endpoint saknas
        ],
    }
    mentions, relations = build_rows(
        "281 — Förhör — 2022 — D-1 — 2", 4, payload)
    assert {(m["label"], m["norm"]) for m in mentions} == {
        ("Person", "stig engström"), ("Plats", "dekorima")}
    assert mentions[0]["stem"] == "281 — Förhör — 2022 — D-1 — 2"
    assert mentions[0]["nr"] == "281"
    assert mentions[0]["sida"] == 4
    # Relationen med okänd endpoint filtreras bort
    assert len(relations) == 1
    assert relations[0]["fran_label"] == "Person"
    assert relations[0]["till_label"] == "Plats"
    assert relations[0]["typ"] == "sågs vid"
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_load_neo4j.py -q`
Förväntat: FAIL med `ModuleNotFoundError`/`ImportError`

- [ ] **Step 3: Implementera**

Skapa `src/graph/load_neo4j.py`:

```python
"""Ladda doc_entities-payloads från state.db till Neo4j.

Idempotent: allt skrivs med MERGE, så omkörning ger samma graf.

Kör (kräver Neo4j igång, se neo4j/docker-compose.yml):
    NEO4J_PASSWORD=... python -m graph.load_neo4j [--batch 1000]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import db as state_db  # noqa: E402

LABELS = {"person": "Person", "plats": "Plats", "organisation": "Organisation"}

_WS_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """Unikhetsnyckel: gemener + kollapsad whitespace."""
    return _WS_RE.sub(" ", name).strip().lower()


def _doc_meta(stem: str) -> dict:
    parts = [p.strip() for p in stem.split(" — ")]
    return {"stem": stem, "nr": parts[0] if parts else stem,
            "titel": parts[1] if len(parts) > 1 else ""}


def build_rows(stem: str, page_num: int, payload: dict) -> tuple[list[dict], list[dict]]:
    """Översätt en sid-payload till mention- och relationsrader för Cypher.

    Relationer vars endpoints inte finns bland sidans entiteter hoppas över
    (LLM:en hittade på eller stavade annorlunda)."""
    meta = _doc_meta(stem)
    norm_to_label: dict[str, str] = {}
    mentions: list[dict] = []
    for e in payload.get("entiteter", []):
        label = LABELS.get(e.get("typ", ""))
        if not label:
            continue
        norm = normalize_name(e["namn"])
        if not norm:
            continue
        norm_to_label[norm] = label
        mentions.append({**meta, "sida": page_num, "label": label,
                         "norm": norm, "namn": e["namn"].strip()})

    relations: list[dict] = []
    for r in payload.get("relationer", []):
        fn, tn = normalize_name(r["fran"]), normalize_name(r["till"])
        if fn not in norm_to_label or tn not in norm_to_label:
            continue
        relations.append({"stem": stem, "sida": page_num, "typ": r["typ"],
                          "fran_norm": fn, "fran_label": norm_to_label[fn],
                          "till_norm": tn, "till_label": norm_to_label[tn]})
    return mentions, relations
```

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_load_neo4j.py -q`
Förväntat: PASS

---

### Task 8: Neo4j-skrivning (constraints + batchad MERGE)

**Files:**
- Modify: `src/graph/load_neo4j.py`
- Test: `tests/test_load_neo4j.py` (append)

- [ ] **Step 1: Skriv failande test** (mockad session — verifierar gruppering & Cypher-anrop)

```python
def test_write_batches_groups_by_label() -> None:
    from unittest.mock import MagicMock
    from graph.load_neo4j import write_mentions

    session = MagicMock()
    mentions = [
        {"stem": "a", "nr": "1", "titel": "t", "sida": 1,
         "label": "Person", "norm": "x", "namn": "X"},
        {"stem": "a", "nr": "1", "titel": "t", "sida": 1,
         "label": "Plats", "norm": "y", "namn": "Y"},
        {"stem": "a", "nr": "1", "titel": "t", "sida": 2,
         "label": "Person", "norm": "z", "namn": "Z"},
    ]
    write_mentions(session, mentions)
    # En run per label
    assert session.run.call_count == 2
    queries = [c.args[0] for c in session.run.call_args_list]
    assert any(":Person" in q for q in queries)
    assert any(":Plats" in q for q in queries)
    # Person-batchen innehåller båda person-raderna
    person_call = next(c for c in session.run.call_args_list if ":Person" in c.args[0])
    assert len(person_call.kwargs["rows"]) == 2
```

- [ ] **Step 2: Verifiera RED**

Kör: `.venv/bin/pytest tests/test_load_neo4j.py -q -k batches`
Förväntat: FAIL med `ImportError: cannot import name 'write_mentions'`

- [ ] **Step 3: Implementera**

Lägg i `src/graph/load_neo4j.py`:

```python
CONSTRAINTS = [
    "CREATE CONSTRAINT dokument_stem IF NOT EXISTS "
    "FOR (d:Dokument) REQUIRE d.stem IS UNIQUE",
    *(f"CREATE CONSTRAINT {lbl.lower()}_norm IF NOT EXISTS "
      f"FOR (n:{lbl}) REQUIRE n.norm IS UNIQUE" for lbl in LABELS.values()),
]

_MENTION_CYPHER = """
UNWIND $rows AS row
MERGE (d:Dokument {{stem: row.stem}})
  ON CREATE SET d.nr = row.nr, d.titel = row.titel
MERGE (e:{label} {{norm: row.norm}})
  ON CREATE SET e.namn = row.namn
MERGE (d)-[:NÄMNER {{sida: row.sida}}]->(e)
"""

_RELATION_CYPHER = """
UNWIND $rows AS row
MATCH (a:{fl} {{norm: row.fran_norm}})
MATCH (b:{tl} {{norm: row.till_norm}})
MERGE (a)-[:RELATERAR {{typ: row.typ, stem: row.stem, sida: row.sida}}]->(b)
"""


def write_mentions(session, mentions: list[dict]) -> None:
    """Skriv mention-rader, en batch per entitetslabel (Cypher kan inte
    parameterisera labels)."""
    by_label: dict[str, list[dict]] = {}
    for m in mentions:
        by_label.setdefault(m["label"], []).append(m)
    for label, rows in by_label.items():
        session.run(_MENTION_CYPHER.format(label=label), rows=rows)


def write_relations(session, relations: list[dict]) -> None:
    """Skriv relationsrader, en batch per (från-label, till-label)-par."""
    by_pair: dict[tuple[str, str], list[dict]] = {}
    for r in relations:
        by_pair.setdefault((r["fran_label"], r["till_label"]), []).append(r)
    for (fl, tl), rows in by_pair.items():
        session.run(_RELATION_CYPHER.format(fl=fl, tl=tl), rows=rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uri", default=os.environ.get("NEO4J_URI", "bolt://localhost:7687"))
    ap.add_argument("--user", default=os.environ.get("NEO4J_USER", "neo4j"))
    ap.add_argument("--batch", type=int, default=1000,
                    help="antal sidor per transaktion (default: 1000)")
    args = ap.parse_args()

    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        print("Sätt NEO4J_PASSWORD.", file=sys.stderr)
        return 1

    from neo4j import GraphDatabase  # noqa: PLC0415 — kräver extras [graph]

    conn = state_db.connect()
    state_db.init_schema(conn)

    driver = GraphDatabase.driver(args.uri, auth=(args.user, password))
    n_pages = n_mentions = n_relations = 0
    with driver.session() as session:
        for stmt in CONSTRAINTS:
            session.run(stmt)
        batch_m: list[dict] = []
        batch_r: list[dict] = []
        for row in state_db.iter_doc_entities(conn):
            mentions, relations = build_rows(
                row["pdf_stem"], row["page_num"], row["payload"])
            batch_m.extend(mentions)
            batch_r.extend(relations)
            n_pages += 1
            if n_pages % args.batch == 0:
                write_mentions(session, batch_m)
                write_relations(session, batch_r)
                n_mentions += len(batch_m); n_relations += len(batch_r)
                batch_m, batch_r = [], []
                print(f"  {n_pages} sidor laddade…", flush=True)
        write_mentions(session, batch_m)
        write_relations(session, batch_r)
        n_mentions += len(batch_m); n_relations += len(batch_r)
    driver.close()
    print(f"Klart: {n_pages} sidor → {n_mentions} omnämnanden, "
          f"{n_relations} relationer.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Verifiera GREEN**

Kör: `.venv/bin/pytest tests/test_load_neo4j.py -q`
Förväntat: alla PASS

- [ ] **Step 5: Checkpoint** — lämpligt /cap-läge.

---

### Task 9: Wrapper `load_graph.sh`

**Files:**
- Create: `load_graph.sh`

- [ ] **Step 1: Skapa wrappern**

```bash
#!/bin/bash
# Ladda extraherade entiteter/relationer från state.db till Neo4j.
# Kräver NEO4J_PASSWORD i miljön och Neo4j igång (neo4j/docker-compose.yml).
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)
cd "$ROOT"
source .venv/bin/activate
exec python -m graph.load_neo4j "$@"
```

Kör: `chmod +x load_graph.sh`

---

### Task 10: Dokumentation (README + CLAUDE.md)

**Files:**
- Modify: `README.md` (nytt avsnitt efter "4. Ställ frågor" + Filer-tabellen)
- Modify: `CLAUDE.md` (Directory Structure + Commands)

- [ ] **Step 1: README — nytt avsnitt "5. Kunskapsgraf (valfritt)"**

Innehåll: vad grafen är (entiteter/relationer ur texten, kompletterar RAG:en — ersätter den inte), kommandona:

```bash
./extract_entities.sh --dry-run        # se omfång utan kostnad
./extract_entities.sh --limit 20       # provkörning
./extract_entities.sh                  # hela arkivet (~$60–120, Haiku)
cd neo4j && NEO4J_PASSWORD=... docker compose up -d
NEO4J_PASSWORD=... ./load_graph.sh     # ladda grafen
```

Plus schema-diagrammet (Dokument/Person/Plats/Organisation, NÄMNER/RELATERAR), exempel-Cypher (`MATCH (p:Person {norm:"stig engström"})<-[:NÄMNER]-(d) RETURN d.titel`), och Browser-URL:en http://localhost:7474 för visualisering. Lägg `extract_entities.sh`, `load_graph.sh`, `src/graph/*` och `neo4j/docker-compose.yml` i Filer-tabellen.

- [ ] **Step 2: CLAUDE.md** — lägg `graph/extract_entities.py`, `graph/load_neo4j.py` i Directory Structure, kommandona i Commands, och en rad i "Non-obvious Design Decisions": *"Kunskapsgrafen är ett separat lager — LanceDB sköter retrieval, Neo4j relationer/visualisering; de delar nyckeln pdf_stem. doc_entities i state.db är källan; Neo4j kan alltid byggas om därifrån med load_graph.sh."*

- [ ] **Step 3: Kör hela testsviten**

Kör: `.venv/bin/pytest tests/ -q`
Förväntat: alla PASS

- [ ] **Step 4: Checkpoint** — /cap.

---

### Task 11: Skarp provkörning end-to-end

- [ ] **Step 1:** `./extract_entities.sh --limit 10` (kräver CLAUDE_CODE_OAUTH_TOKEN; kostnad ~öresnivå)
- [ ] **Step 2:** Verifiera i SQLite: `sqlite3 generated/db/state.db "SELECT COUNT(*) FROM doc_entities"` — > 0 rader, stickprova payload-JSON.
- [ ] **Step 3:** `NEO4J_PASSWORD=... ./load_graph.sh` — förväntat: "Klart: N sidor → M omnämnanden, K relationer."
- [ ] **Step 4:** Öppna http://localhost:7474, kör `MATCH (n) RETURN n LIMIT 100` — grafen ska visa Dokument-noder kopplade till Person/Plats/Organisation.
- [ ] **Step 5:** Justera extraktionsprompten vid behov (vanligast: för generösa "organisationer" eller OCR-skräp som namn) innan fullkörning. Fullkörningen (~40 000 sidor) startas av användaren när hen är nöjd med stickprovet.

---

## Utanför scope (medvetet)

- **MCP-verktyg `explore_graph`** för utredningsläget — naturligt nästa steg när grafen finns, egen plan.
- **Entity resolution över stavningsvarianter** (OCR: "Engström"/"Engstrom") — börja med exakt norm-match; utvärdera efter fullkörning.
- **Flytt av chunks/embeddings till Neo4j** — bara aktuellt om tvålagersmodellen visar sig begränsande.
