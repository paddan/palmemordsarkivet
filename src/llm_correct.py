"""LLM-baserad post-korrektion av dåliga OCR-sidor.

Läser quality_pages.jsonl, identifierar sidor under score-tröskeln,
skickar sidtexten till vald LLM (Claude eller OpenAI-kompatibel) för
rättning och slår ihop resultatet via merge_pages.merge_one.

Idempotent: sidor med en .llm-markörfil i text_pages/<stem>/ hoppas över.

Kör:
    python llm_correct.py [--threshold 50] [--provider claude|openai]
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

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_text import normalize  # noqa: E402
import config as _llm_config  # noqa: E402

HAIKU_MODEL = "claude-haiku-4-5-20251001"

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def _resolve_api_key(provider: str, base_url: str, explicit_key: str) -> str:
    if explicit_key:
        return explicit_key
    if provider == "claude":
        key = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
        if not key:
            print("Sätt CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY.", file=sys.stderr)
            sys.exit(1)
        return key
    # openai
    if not base_url:
        key = os.environ.get("OPENAI_API_KEY") or ""
        if not key:
            print("Sätt OPENAI_API_KEY.", file=sys.stderr)
            sys.exit(1)
        return key
    return ""  # custom base_url → ingen validering


_SYSTEM = """\
Du rättar OCR-fel i skannade svenska dokument.
Returnera ENBART den rättade texten – inga kommentarer eller förklaringar.
Bevara struktur: radbrytningar, stycken, indragningar, tabeller.
Rätta bara uppenbara OCR-fel (fellästa tecken, trasiga ord, skräptecken).
Ändra inte meningsinnehåll. Om du är osäker på ett ord, lämna det oförändrat.
Bevara egennamn, förkortningar, ärendenummer och liknande exakt som de är."""


async def _claude(text: str, model: str) -> str:
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


async def _openai(text: str, model: str, base_url: str, api_key: str) -> str:
    if AsyncOpenAI is None:
        raise RuntimeError("openai-paketet saknas — kör: pip install openai")
    client = AsyncOpenAI(
        api_key=api_key or "local",
        base_url=base_url or None,
    )
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
        max_tokens=4096,
    )
    return response.choices[0].message.content or text


async def _correct_all(
    bad: dict[str, list[int]],
    txt_dir: Path,
    pages_dir: Path,
    provider_cfg: dict,
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
        pending_markers: list[Path] = []
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

            if provider_cfg["provider"] == "claude":
                corrected = await _claude(page_text, provider_cfg["model"])
            else:
                corrected = await _openai(
                    page_text,
                    provider_cfg["model"],
                    provider_cfg["base_url"],
                    provider_cfg["api_key"],
                )
            (stem_dir / f'page-{p:03d}.txt').write_text(
                normalize(corrected), encoding='utf-8'
            )
            pending_markers.append(stem_dir / f'page-{p:03d}.llm')
            file_changed = True

        if file_changed and not dry_run:
            try:
                merge_one(stem, txt_dir, pages_dir)
                for _m in pending_markers:
                    _m.touch()
            except Exception as e:  # noqa: BLE001
                print(f'  [merge-fel] {stem}: {e}', file=sys.stderr)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description='LLM-korrektion av dåliga OCR-sidor.'
    )
    ap.add_argument('--threshold', type=float, default=50.0,
                    help='score-tröskel (default: 50)')
    ap.add_argument('--provider', default='',
                    help='LLM-provider: claude eller openai (default: från llm_config.json)')
    ap.add_argument('--model', default='',
                    help='modellnamn (default: från llm_config.json)')
    ap.add_argument('--base-url', default='',
                    help='override API-URL för OpenAI-kompatibla providers (Ollama, DeepSeek, ...)')
    ap.add_argument('--api-key', default='',
                    help='override API-nyckel (annars läses från env)')
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
    jsonl = Path(args.pages_jsonl) if args.pages_jsonl else root / 'generated' / 'quality_pages.jsonl'
    txt_dir = Path(args.txt) if args.txt else root / 'generated' / 'text'
    pages_dir = Path(args.pages_out) if args.pages_out else root / 'generated' / 'text_pages'

    if not jsonl.exists():
        print(f'Saknar {jsonl} — kör ./quality.sh --per-page först.', file=sys.stderr)
        sys.exit(1)

    saved_cfg = _llm_config.load()
    provider = args.provider or saved_cfg.get("provider", "claude")
    if provider not in ("claude", "openai"):
        provider = "claude"
    base_url = args.base_url or saved_cfg.get("base_url", "")
    saved_model = saved_cfg.get("model", "") if not args.provider else ""
    default_model = HAIKU_MODEL if provider == 'claude' else OPENAI_DEFAULT_MODEL
    model = args.model or saved_model or default_model
    api_key = _resolve_api_key(provider, base_url, args.api_key)

    provider_cfg = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    }

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
          + (f' ({skipped} redan rättade hoppas över)' if skipped else '')
          + f' med {provider}/{model}.')
    if args.dry_run:
        print('[dry-run — inga filer skrivs]')

    asyncio.run(_correct_all(bad, txt_dir, pages_dir, provider_cfg, args.dry_run))

    if not args.dry_run:
        print('\nKlart. Kör ./quality.sh för att se förbättringen.')
        print('Kör ./ingest.sh för att re-indexera ändrade filer.')


if __name__ == '__main__':
    main()
