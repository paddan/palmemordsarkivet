#!/usr/bin/env python3
"""
Ställ frågor till det indexerade arkivet.

Använder Claude Agent SDK med OAuth-token (Pro/Max-abonnemang) — räknas mot
abonnemangets timgränser, inte mot API-credits.

Kör:
    export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
    python ask.py "Vad sa Annett Kohut om kvällen den 28 februari?"
    python ask.py --top-k 30 --rerank "din fråga"
    python ask.py            # interaktiv repl
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import lancedb
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingConfigAdaptive,
    query,
)
from sentence_transformers import SentenceTransformer

DB_DIR = Path(__file__).resolve().parent / "lancedb"
TABLE = "chunks"
EMBED_MODEL = "intfloat/multilingual-e5-large"
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
CLAUDE_MODEL = "claude-opus-4-7"

SYSTEM_PROMPT = """Du svarar på frågor om Palmemordsarkivet baserat på de utdrag användaren ger dig.

Regler:
- Svara på svenska.
- Stötta varje påstående med en källhänvisning på formen [Nr X, sida Y].
- Om svaret inte framgår av utdragen, säg "framgår inte av materialet" — gissa aldrig.
- Citera ordagrant när det är klargörande, men håll citaten korta.
- OCR-fel kan förekomma. Säg till om en passage verkar vara skadad eller obegriplig."""


def search(table, model, q: str, top_k: int) -> list[dict]:
    qv = model.encode(
        [f"query: {q}"],
        normalize_embeddings=True,
        convert_to_numpy=True,
    )[0]
    return (
        table.search(qv.tolist())
        .limit(top_k)
        .select(["text", "source", "page", "nr", "titel", "anmarkning"])
        .to_list()
    )


def rerank(q: str, hits: list[dict], top_n: int) -> list[dict]:
    from sentence_transformers import CrossEncoder

    ce = CrossEncoder(RERANK_MODEL)
    pairs = [(q, h["text"]) for h in hits]
    scores = ce.predict(pairs, show_progress_bar=False)
    ranked = sorted(zip(scores, hits), key=lambda x: -float(x[0]))
    return [h for _, h in ranked[:top_n]]


def format_context(hits: list[dict]) -> str:
    blocks = []
    for h in hits:
        header = f"[Nr {h['nr']}, sida {h['page']}, \"{h['titel'][:60]}\"]"
        blocks.append(f"{header}\n{h['text']}")
    return "\n\n---\n\n".join(blocks)


async def ask_claude(q: str, context: str) -> None:
    user_msg = f"Utdrag ur arkivet:\n\n{context}\n\n---\n\nFråga: {q}"
    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=CLAUDE_MODEL,
        allowed_tools=[],          # ren Q&A — inga verktyg
        thinking=ThinkingConfigAdaptive(type="adaptive"),
        effort="high",
        max_turns=1,
        setting_sources=[],        # ignorera lokal CLAUDE.md / settings
    )
    async for message in query(prompt=user_msg, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
        elif isinstance(message, ResultMessage):
            print()
            if getattr(message, "is_error", False):
                print(f"\n(fel: {message})", file=sys.stderr)


async def run_query(table, embed_model, q: str, top_k: int, top_n: int, do_rerank: bool):
    print(f"\n→ söker top-{top_k} chunks…", flush=True)
    hits = search(table, embed_model, q, top_k)
    if not hits:
        print("Inga träffar.")
        return
    if do_rerank:
        print(f"→ omrankar med cross-encoder, behåller top-{top_n}…", flush=True)
        hits = rerank(q, hits, top_n)
    else:
        hits = hits[:top_n]

    print("\nKällor som skickas till Claude:")
    for h in hits:
        print(f"  Nr {h['nr']}, sida {h['page']}: {h['titel'][:70]}")
    print("\n— Svar —\n")
    await ask_claude(q, format_context(hits))


async def main_async(args) -> int:
    if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
        print(
            "Sätt CLAUDE_CODE_OAUTH_TOKEN (Pro/Max) eller ANTHROPIC_API_KEY först.",
            file=sys.stderr,
        )
        return 1
    if not DB_DIR.exists():
        print(f"Saknar {DB_DIR}/ — kör ingest.py först.", file=sys.stderr)
        return 1

    db = lancedb.connect(str(DB_DIR))
    if TABLE not in db.table_names():
        print(f"Tabell '{TABLE}' finns inte — kör ingest.py först.", file=sys.stderr)
        return 1
    table = db.open_table(TABLE)
    print(f"Index: {table.count_rows()} chunks. Laddar embedding-modell…")
    embed_model = SentenceTransformer(EMBED_MODEL)

    if args.query:
        await run_query(table, embed_model, " ".join(args.query),
                        args.top_k, args.top_n, args.rerank)
        return 0

    print("Interaktiv repl — tom rad eller Ctrl-D avslutar.\n")
    try:
        while True:
            q = input("frågan> ").strip()
            if not q:
                break
            await run_query(table, embed_model, q,
                            args.top_k, args.top_n, args.rerank)
    except (EOFError, KeyboardInterrupt):
        print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*", help="frågan; lämna tom för repl")
    ap.add_argument("--top-k", type=int, default=20, help="antal kandidater från vektor-DB")
    ap.add_argument("--top-n", type=int, default=6, help="antal som skickas till Claude")
    ap.add_argument("--rerank", action="store_true", help="omranka med cross-encoder")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
