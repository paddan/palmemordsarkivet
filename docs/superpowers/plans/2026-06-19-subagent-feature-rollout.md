# Subagent Feature Rollout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Koordinera fyra medium-effort feature-agenter i separata worktree-branches.

**Architecture:** Varje feature har en egen planfil, egen branch och egen Codex worktree-tråd. Koordinatorn startar trådarna, väntar in rapporter, gör review/integration i huvudtråden och ber användaren om nästa steg.

**Tech Stack:** Codex worktree threads via `codex_app.create_thread`, Python/Streamlit/pytest enligt varje plan.

## Global Constraints

- Starta inte implementation förrän användaren har godkänt planerna.
- Varje agent ska använda thinking `medium`.
- Varje agent ska arbeta i egen worktree-branch.
- Agenterna får inte commit:a eller pusha; de ska lämna ändringar i sina worktrees och rapportera.
- Koordinatorn ska review:a varje worktree innan eventuell merge/cherry-pick.
- Om två worktrees ändrar samma dokumentation ska koordinatorn integrera docs sist.

---

## Feature Agents

| Agent | Branch | Plan | Primärt ägarskap |
|---|---|---|---|
| Källor-bläddrare | `codex/kallor-bladdrare` | `docs/superpowers/plans/2026-06-19-kallor-bladdrare.md` | `src/archive_browser.py`, `src/pages/1_Källor.py`, `tests/test_archive_browser.py` |
| Sökverkstad | `codex/sokverkstad` | `docs/superpowers/plans/2026-06-19-sokverkstad.md` | `src/search_workbench.py`, `src/pages/4_Sökverkstad.py`, `tests/test_search_workbench.py` |
| Tidslinje | `codex/tidslinje` | `docs/superpowers/plans/2026-06-19-tidslinje.md` | `src/timeline.py`, `src/pages/5_Tidslinje.py`, `tests/test_timeline.py` |
| Pärm-export | `codex/parm-export` | `docs/superpowers/plans/2026-06-19-parm-export.md` | `src/casebook_export.py`, `src/casebook_ui.py`, `tests/test_casebook_export.py` |

## Execution Order

All four implementation threads can run in parallel after approval. They are mostly independent. Expected integration friction:

- Documentation files overlap across all agents. Resolve docs in coordinator review after code is accepted.
- Page ordering may need final polish after all new pages exist.
- `Pärm-export` touches `src/casebook_ui.py`; other plans should only consume `casebook_ui` and not modify it.

## Coordinator Steps

- [ ] **Step 1: Confirm clean base**

Run:

```bash
git status --short --branch
.venv/bin/pytest tests/ -q
```

Expected: branch `main...origin/main`, clean worktree, test suite green.

- [ ] **Step 2: Spawn worktree threads**

Use `codex_app.create_thread` once per plan, with the JSON in each plan's **Worktree Thread Start** section.

- [ ] **Step 3: Track returned thread ids**

Record pending/created thread ids in this checklist:

- `codex/kallor-bladdrare`: pending
- `codex/sokverkstad`: pending
- `codex/tidslinje`: pending
- `codex/parm-export`: pending

- [ ] **Step 4: Review each agent result**

For each completed worktree:

```bash
git -C <worktree-path> status --short --branch
git -C <worktree-path> diff --stat
git -C <worktree-path> diff --check
```

Read changed files, compare to that agent's plan, and run that plan's verification commands.

- [ ] **Step 5: Integration decision**

After all four are reviewed, present one concise integration summary to the user:

- which branches are ready
- which need fixes
- expected merge conflicts
- recommended merge order

Do not merge, commit or push until the user explicitly asks.
