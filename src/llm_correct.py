"""LLM-baserad post-korrektion av dåliga OCR-sidor med Claude Haiku.

Läser quality_pages.jsonl, identifierar sidor under score-tröskeln,
skickar sidtexten till Claude Haiku för rättning och slår ihop resultatet
via merge_pages.merge_one. Kräver CLAUDE_CODE_OAUTH_TOKEN eller
ANTHROPIC_API_KEY i miljön.

Idempotent: sidor med en .llm-markörfil i text_pages/<stem>/ hoppas över.

Kör:
    python llm_correct.py [--threshold 50] [--pages-jsonl quality_pages.jsonl]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_text import normalize  # noqa: E402

HAIKU_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = """\
Du rättar OCR-fel i skannade svenska dokument.
Returnera ENBART den rättade texten – inga kommentarer eller förklaringar.
Bevara struktur: radbrytningar, stycken, indragningar, tabeller.
Rätta bara uppenbara OCR-fel (fellästa tecken, trasiga ord, skräptecken).
Ändra inte meningsinnehåll. Om du är osäker på ett ord, lämna det oförändrat.
Bevara egennamn, förkortningar, ärendenummer och liknande exakt som de är."""


async def _haiku(text: str, model: str) -> str:
    options = ClaudeAgentOptions(
        system_prompt=_SYSTEM,
        model=model,
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
    return ''.join(parts) or text  # fallback till original om tomt svar


async def _correct_all(
    bad: dict[str, list[int]],
    txt_dir: Path,
    pages_dir: Path,
    model: str,
    dry_run: bool,
) -> None:
    from merge_pages import merge_one  # noqa: PLC0415

    total = sum(len(v) for v in bad.values())
    done = 0
    t0 = time.monotonic()

    for txt_name, pages in bad.items():
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        txt_path = txt_dir / f'{stem}.txt'
        if not txt_path.exists():
            print(f'  SAKNAS: {txt_path}', file=sys.stderr)
            continue

        full_text = txt_path.read_text(encoding='utf-8', errors='replace')
        page_texts = full_text.split('\f')
        stem_dir = pages_dir / stem
        stem_dir.mkdir(parents=True, exist_ok=True)

        file_changed = False
        for p in sorted(set(pages)):
            idx = p - 1
            if idx < 0 or idx >= len(page_texts):
                print(f'  [skip] {stem} sida {p}: utanför range '
                      f'(dokumentet har {len(page_texts)} sidor)')
                continue

            done += 1
            page_text = normalize(page_texts[idx])
            elapsed = time.monotonic() - t0
            rate = done / elapsed if elapsed else 0
            eta = int((total - done) / rate) if rate else 0
            eta_s = f'{eta // 60}m{eta % 60:02d}s'
            if not page_text.strip():
                print(f'  [{done}/{total}] {stem} sida {p}: tom — hoppar  eta {eta_s}')
                (stem_dir / f'page-{p:03d}.llm').touch()
                continue

            print(f'  [{done}/{total}] {stem} sida {p} ({len(page_text)} tecken)  eta {eta_s}')
            if dry_run:
                continue

            corrected = await _haiku(page_text, model)
            (stem_dir / f'page-{p:03d}.txt').write_text(
                normalize(corrected), encoding='utf-8'
            )
            (stem_dir / f'page-{p:03d}.llm').touch()
            file_changed = True

        if file_changed and not dry_run:
            try:
                merge_one(stem, txt_dir, pages_dir)
            except Exception as e:  # noqa: BLE001
                print(f'  [merge-fel] {stem}: {e}', file=sys.stderr)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description='LLM-korrektion av dåliga OCR-sidor med Claude Haiku.'
    )
    ap.add_argument('--threshold', type=float, default=50.0,
                    help='score-tröskel (default: 50)')
    ap.add_argument('--model', default=HAIKU_MODEL,
                    help=f'Claude-modell (default: {HAIKU_MODEL})')
    ap.add_argument('--pages-jsonl', default='',
                    help='quality_pages.jsonl (default: <root>/quality_pages.jsonl)')
    ap.add_argument('--txt', default='',
                    help='text-katalog (default: <root>/text)')
    ap.add_argument('--pages-out', default='',
                    help='text_pages-katalog (default: <root>/text_pages)')
    ap.add_argument('--root', default='', help='projektrot')
    ap.add_argument('--dry-run', action='store_true',
                    help='visa vad som skulle rättas utan att göra det')
    args = ap.parse_args()

    root = Path(args.root) if args.root else ROOT
    jsonl = Path(args.pages_jsonl) if args.pages_jsonl else root / 'quality_pages.jsonl'
    txt_dir = Path(args.txt) if args.txt else root / 'text'
    pages_dir = Path(args.pages_out) if args.pages_out else root / 'text_pages'

    if not jsonl.exists():
        print(f'Saknar {jsonl} — kör ./quality.sh --per-page först.', file=sys.stderr)
        sys.exit(1)
    if not (os.environ.get('CLAUDE_CODE_OAUTH_TOKEN') or
            os.environ.get('ANTHROPIC_API_KEY')):
        print('Sätt CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY.', file=sys.stderr)
        sys.exit(1)

    # Samla dåliga sidor ur JSONL
    raw: dict[str, list[int]] = defaultdict(list)
    with open(jsonl, encoding='utf-8') as f:
        for line in f:
            try:
                row = json.loads(line)
            except Exception:
                continue
            score = float(row.get('score') or 0.0)
            if score < args.threshold:
                raw[row['file']].append(int(row['page']))

    # Filtrera bort sidor som redan är rättade (.llm-markör)
    bad: dict[str, list[int]] = defaultdict(list)
    skipped = 0
    for txt_name, pages in raw.items():
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        stem_dir = pages_dir / stem
        for p in pages:
            if (stem_dir / f'page-{p:03d}.llm').exists():
                skipped += 1
            else:
                bad[txt_name].append(p)

    total = sum(len(v) for v in bad.values())
    if not bad:
        print(f'Inga nya sidor att rätta ({skipped} redan rättade).')
        return

    print(f'Rättar {total} sidor i {len(bad)} filer'
          + (f' ({skipped} redan rättade hoppas över)' if skipped else '') + '.')
    if args.dry_run:
        print('[dry-run — inga filer skrivs]')

    asyncio.run(_correct_all(bad, txt_dir, pages_dir, args.model, args.dry_run))

    if not args.dry_run:
        print('\nKlart. Kör ./quality.sh för att se förbättringen.')
        print('Kör ./ingest.sh för att re-indexera ändrade filer.')


if __name__ == '__main__':
    main()
