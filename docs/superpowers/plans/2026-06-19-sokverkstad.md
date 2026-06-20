# Sökverkstad Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bygg en fristående sökverkstad där användaren kan köra manuella vektor-/hybridsökningar, granska träffar och bokmärka källor innan AI-svaret formuleras.

**Architecture:** Lägg träffnormalisering och utdragsformatering i `src/search_workbench.py`. Streamlit-sidan `src/pages/4_Sökverkstad.py` återanvänder befintliga RAG-funktioner från `src/rag/ask.py`, `casebook_ui.render_source_cards` och samma LanceDB/embedding-modell som Utredning.

**Tech Stack:** Python 3.11+, Streamlit, LanceDB, sentence-transformers, befintliga `search`, `search_hybrid`, `rerank`, pytest.

## Global Constraints

- Branch/worktree: `codex/sokverkstad`, egen Codex worktree-tråd, thinking `medium`.
- Commit/push: gör inga commits och pusha inte; lämna ändringar i worktree:t och rapportera.
- Kommentarer och docstrings i produktionskod ska vara på svenska.
- Håll ändringen avgränsad till ny sökverkstadsmodul/sida, tester och dokumentation.
- Undvik att ändra `src/Utredning.py`; detta ska vara en fristående flik.
- Uppdatera `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md` och `CLAUDE.md` om sidan/beteendet ändras.
- Kör minst `.venv/bin/pytest tests/test_search_workbench.py -q`, `python3 -m compileall src/search_workbench.py src/pages/4_Sökverkstad.py` och `git diff --check`.

---

## Worktree Thread Start

Coordinator creates the worker thread with:

```json
{
  "target": {
    "type": "project",
    "projectId": "/Users/patrik/projects/palmemordsarkivet",
    "environment": {
      "type": "worktree",
      "startingState": {
        "type": "branch",
        "branchName": "codex/sokverkstad"
      }
    }
  },
  "thinking": "medium",
  "prompt": "Implementera planen docs/superpowers/plans/2026-06-19-sokverkstad.md. Arbeta i din worktree-branch codex/sokverkstad. Gör inga commits och pusha inte. Följ TDD-stegen, uppdatera dokumentation, kör angivna tester och rapportera ändrade filer samt testresultat."
}
```

## File Structure

- Create: `src/search_workbench.py` — ren logik för träffrubriker, utdrag och stabila valnycklar.
- Create: `src/pages/4_Sökverkstad.py` — Streamlit-sida för manuell sökning och källgranskning.
- Create: `tests/test_search_workbench.py` — pytest för formattering och urval.
- Modify: `README.md`, `docs/kom-igang.md`, `docs/teknisk-referens.md`, `AGENTS.md`, `CLAUDE.md`.

## Task 1: Träffformatterare

**Files:**
- Create: `src/search_workbench.py`
- Test: `tests/test_search_workbench.py`

**Interfaces:**
- Produces:
  - `hit_key(hit: dict) -> str`
  - `hit_title(hit: dict) -> str`
  - `hit_excerpt(hit: dict, max_chars: int = 500) -> str`

- [ ] **Step 1: Write failing tests**

Create `tests/test_search_workbench.py`:

```python
from search_workbench import hit_excerpt, hit_key, hit_title


def test_hit_key_is_stable_for_source_page_chunk() -> None:
    hit = {"source": "100 — Skandia.txt", "page": 28, "chunk_idx": 3}
    assert hit_key(hit) == "100 — Skandia.txt:28:3"


def test_hit_title_prefers_nr_title_and_page() -> None:
    hit = {"nr": "100", "titel": "Skandiaförhör", "page": 28, "source": "100 — Skandia.txt"}
    assert hit_title(hit) == "Nr 100, sida 28 — Skandiaförhör"


def test_hit_excerpt_collapses_whitespace_and_truncates() -> None:
    hit = {"text": "Rad 1\n\nRad 2 " + ("ord " * 200)}
    excerpt = hit_excerpt(hit, max_chars=45)
    assert excerpt.startswith("Rad 1 Rad 2")
    assert excerpt.endswith("...")
    assert len(excerpt) <= 45
```

- [ ] **Step 2: Verify red**

Run: `.venv/bin/pytest tests/test_search_workbench.py -q`

Expected: fails because module does not exist.

- [ ] **Step 3: Implement module**

Create `src/search_workbench.py`. Keep it Streamlit-free and deterministic. Use fallback title from `source` stem when `nr`/`titel` are missing.

- [ ] **Step 4: Verify green**

Run: `.venv/bin/pytest tests/test_search_workbench.py -q`

Expected: all tests pass.

## Task 2: Sökverkstad page

**Files:**
- Create: `src/pages/4_Sökverkstad.py`

**Interfaces:**
- Consumes: `search`, `search_hybrid`, `rerank`, `TABLE`, `EMBED_MODEL` from `src/rag/ask.py`.
- Consumes: `hit_key`, `hit_title`, `hit_excerpt`.
- Consumes: `casebook_ui.render_source_cards`.

- [ ] **Step 1: Create page skeleton**

Create a Streamlit page with:

- title `Sökverkstad`
- caption `Granska träffar innan du låter AI formulera ett svar.`
- sidebar controls:
  - `Sökfråga`
  - `Hybrid/BM25`
  - `Reranka`
  - `Top-K` 5-50 default 20
  - `Top-N` 1-15 default 8

- [ ] **Step 2: Load index**

Copy the same resource pattern as `src/Utredning.py`: connect to `generated/lancedb`, open table `TABLE`, load `SentenceTransformer(EMBED_MODEL)`.

- [ ] **Step 3: Run search on button**

On `st.button("Sök", type="primary")`:

- If hybrid toggle is on, call `search_hybrid(table, embed_model, query, top_k)`.
- Else call `search(table, embed_model, query, top_k)`.
- If rerank toggle is on, call `rerank(query, hits, top_n)`.
- Else slice `hits[:top_n]`.
- Store hits in `st.session_state["search_workbench_hits"]`.

- [ ] **Step 4: Render hit cards**

For each hit:

- show `hit_title(hit)` as heading
- show `hit_excerpt(hit)`
- call `casebook_ui.render_source_cards(ROOT, [hit], conn, key_prefix=f"workbench_{i}_{hit_key(hit)}")`

Do not call an LLM in this feature.

## Task 3: Docs and verification

**Files:**
- Modify docs listed in Global Constraints.

- [ ] **Step 1: Update docs**

Document that **Sökverkstad** is for manual retrieval inspection: useful for testing search terms, comparing vector/hybrid/rerank and bookmarking sources.

- [ ] **Step 2: Run verification**

Run:

```bash
.venv/bin/pytest tests/test_search_workbench.py -q
python3 -m compileall src/search_workbench.py src/pages/4_Sökverkstad.py
git diff --check
```

Expected: tests pass, compileall exits 0, diff-check prints nothing.

- [ ] **Step 3: Report**

Report changed files and test output. Do not commit or push.
