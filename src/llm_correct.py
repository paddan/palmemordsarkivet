"""LLM-baserad post-korrektion av dåliga OCR-sidor.

Hämtar dåliga sidor från ``quality_pages``-tabellen (db.get_bad_pages),
skickar sidtexten till vald LLM (Claude eller OpenAI-kompatibel) för
rättning och skriver tillbaka via ``db.record_page(engine='llm', ...)``.
Efter en fil är klar kör ``merge_pages.merge_one`` som läser senaste
text per sida ur ``pdf_pages`` och uppdaterar ``text/<stem>.txt``.

Idempotent: sidor som finns i ``llm_corrections``-tabellen hoppas över.

Kör:
    python llm_correct.py [--threshold 50] [--provider claude|openai]
"""
from __future__ import annotations

import asyncio
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
import db as state_db  # noqa: E402

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
    # Custom base_url: lokala servrar (Ollama/LM Studio) körs utan nyckel, men
    # kända fjärr-providers kräver sin egen env-nyckel. Matcha på host.
    host = base_url.lower()
    for needle, env in (("deepseek.com", "DEEPSEEK_API_KEY"),
                        ("openai.com", "OPENAI_API_KEY")):
        if needle in host:
            key = os.environ.get(env, "")
            if not key:
                print(f"Sätt {env}.", file=sys.stderr)
                sys.exit(1)
            return key
    return ""  # lokal/okänd server → ingen validering


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
    async with AsyncOpenAI(
        api_key=api_key or "local",
        base_url=base_url or None,
    ) as client:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=4096,
        )
    return response.choices[0].message.content or text


async def _correct_text(text: str, provider_cfg: dict) -> str:
    if provider_cfg["provider"] == "claude":
        return await _claude(text, provider_cfg["model"])
    return await _openai(
        text,
        provider_cfg["model"],
        provider_cfg["base_url"],
        provider_cfg["api_key"],
    )


async def _test_mode(txt_path: Path, provider_cfg: dict, threshold: float) -> None:
    """Test-läge: rätta en textfil och jämför quality-poäng sida för sida."""
    from quality import has_hunspell_swe, score_text  # noqa: PLC0415

    raw = txt_path.read_text(encoding='utf-8', errors='replace')
    pages = raw.split('\f')
    use_hunspell = has_hunspell_swe()

    print(f'Test: {txt_path} ({len(pages)} sida{"r" if len(pages) != 1 else ""})'
          f' — {provider_cfg["provider"]}/{provider_cfg["model"]}'
          + (' + hunspell' if use_hunspell else ''))
    print(f'Tröskel: {threshold:.0f}\n')

    before: list[float] = []
    corrected_pages: list[str] = []
    for i, page in enumerate(pages, 1):
        norm = normalize(page)
        s = score_text(norm, use_hunspell).get('score', 0.0)
        before.append(s)
        if not norm.strip() or s >= threshold:
            corrected_pages.append(norm)
            continue
        print(f'  Sida {i}: score {s:.0f} → rättar…')
        corrected_pages.append(normalize(await _correct_text(norm, provider_cfg)))

    after = [score_text(p, use_hunspell).get('score', 0.0) for p in corrected_pages]

    tmp_dir = ROOT / 'tmp'
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f'llm_test_{txt_path.stem}.txt'
    tmp.write_text('\f'.join(corrected_pages), encoding='utf-8')

    print(f'\n{"Sida":>5}  {"Före":>6}  {"Efter":>6}  {"Δ":>6}')
    print('─' * 30)
    for i, (b, a) in enumerate(zip(before, after), 1):
        delta = a - b
        marker = ' ↑' if delta > 1 else (' ↓' if delta < -1 else '')
        print(f'{i:>5}  {b:>6.1f}  {a:>6.1f}  {delta:>+6.1f}{marker}')
    if len(before) > 1:
        avg_b = sum(before) / len(before)
        avg_a = sum(after) / len(after)
        print('─' * 30)
        print(f'{"Snitt":>5}  {avg_b:>6.1f}  {avg_a:>6.1f}  {avg_a - avg_b:>+6.1f}')
    print(f'\nSparat: {tmp}')


async def _correct_all(
    bad: dict[str, list[int]],
    txt_dir: Path,
    provider_cfg: dict,
    dry_run: bool,
    jobs: int = 1,
) -> None:
    """Rätta alla dåliga sidor. ``jobs`` styr hur många LLM-anrop som körs
    samtidigt via en delad semafor; sidorna är oberoende av varandra och
    DB-skrivningarna sker synkront i event loop-tråden, så delad sqlite-conn
    är säker. ``merge_one`` körs per fil efter att filens sidor är klara."""
    from merge_pages import merge_one  # noqa: PLC0415

    total = sum(len(v) for v in bad.values())
    done = 0
    t0 = time.monotonic()

    conn = state_db.connect()
    state_db.init_schema(conn)

    semaphore = asyncio.Semaphore(max(1, jobs))

    def progress(stem: str, p: int, suffix: str) -> None:
        nonlocal done
        done += 1
        elapsed = time.monotonic() - t0
        rate = done / elapsed if elapsed else 0
        eta = int((total - done) / rate) if rate else 0
        eta_s = f'{eta // 60}m{eta % 60:02d}s'
        print(f'  [{done}/{total}] {stem} sida {p}{suffix}  eta {eta_s}', flush=True)

    async def handle_file(txt_name: str, pages: list[int]) -> None:
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        txt_path = txt_dir / f'{stem}.txt'
        if not txt_path.exists():
            print(f'  SAKNAS: {txt_path}', file=sys.stderr)
            return

        full_text = txt_path.read_text(encoding='utf-8', errors='replace')
        page_texts = full_text.split('\f')
        file_changed = False

        async def handle_page(p: int) -> None:
            nonlocal file_changed
            idx = p - 1
            if idx < 0 or idx >= len(page_texts):
                print(f'  [skip] {stem} sida {p}: utanför range '
                      f'(dokumentet har {len(page_texts)} sidor)')
                return

            page_text = normalize(page_texts[idx])
            if not page_text.strip():
                progress(stem, p, ': tom — hoppar')
                if not dry_run:
                    state_db.mark_llm_corrected(conn, stem, p)
                return

            if dry_run:
                progress(stem, p, f' ({len(page_text)} tecken)')
                return

            async with semaphore:
                try:
                    corrected = normalize(await _correct_text(page_text, provider_cfg))
                except Exception as e:  # noqa: BLE001
                    print(f'  [fel] {stem} sida {p}: {e}', file=sys.stderr)
                    return
            state_db.record_page(
                conn,
                pdf_stem=stem,
                page_num=p,
                engine='llm',
                text=corrected,
                score=None,
            )
            state_db.mark_llm_corrected(conn, stem, p)
            file_changed = True
            progress(stem, p, f' ({len(page_text)} tecken)')

        await asyncio.gather(*(handle_page(p) for p in sorted(set(pages))))

        if file_changed and not dry_run:
            try:
                merge_one(stem, txt_dir)
            except Exception as e:  # noqa: BLE001
                print(f'  [merge-fel] {stem}: {e}', file=sys.stderr)

    await asyncio.gather(
        *(handle_file(name, pages) for name, pages in bad.items())
    )


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
    ap.add_argument('--txt', default='',
                    help='text-katalog (default: <root>/text)')
    ap.add_argument('--root', default='', help='projektrot')
    ap.add_argument('--jobs', type=int, default=int(os.environ.get('JOBS', '4')),
                    help='antal parallella LLM-anrop (default: env JOBS eller 4)')
    ap.add_argument('--dry-run', action='store_true',
                    help='visa vad som skulle rättas utan att göra det')
    ap.add_argument('--test', metavar='FIL',
                    help='test-läge: rätta en enskild .txt-fil och jämför quality-poäng')
    args = ap.parse_args()

    root = Path(args.root) if args.root else ROOT
    txt_dir = Path(args.txt) if args.txt else root / 'generated' / 'text'

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

    if args.test:
        txt_path = Path(args.test)
        if not txt_path.is_file():
            print(f'Filen finns inte: {txt_path}', file=sys.stderr)
            sys.exit(1)
        asyncio.run(_test_mode(txt_path, provider_cfg, args.threshold))
        return

    conn = state_db.connect()
    state_db.init_schema(conn)

    raw: dict[str, list[int]] = defaultdict(list)
    for row in state_db.get_bad_pages(conn, threshold=args.threshold):
        raw[row["pdf_stem"] + ".txt"].append(row["page_num"])
    if not raw:
        print(f'Inga sidor under threshold {args.threshold} i quality_pages '
              '— kör ./quality.sh --per-page först.')
        return

    # Filtrera bort sidor som redan är rättade (llm_corrections-tabellen)
    bad: dict[str, list[int]] = defaultdict(list)
    skipped = 0
    for txt_name, pages in raw.items():
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        for p in pages:
            if state_db.llm_corrected(conn, stem, p):
                skipped += 1
            else:
                bad[txt_name].append(p)

    total = sum(len(v) for v in bad.values())
    if not bad:
        print(f'Inga nya sidor att rätta ({skipped} redan rättade).')
        return

    print(f'Rättar {total} sidor i {len(bad)} filer'
          + (f' ({skipped} redan rättade hoppas över)' if skipped else '')
          + f' med {provider}/{model} ({max(1, args.jobs)} parallella).')
    if args.dry_run:
        print('[dry-run — inga filer skrivs]')

    asyncio.run(_correct_all(bad, txt_dir, provider_cfg, args.dry_run, jobs=args.jobs))

    if not args.dry_run:
        print('\nKlart. Kör ./quality.sh --per-page för att se förbättringen.')
        print('Kör ./ingest.sh för att re-indexera ändrade filer.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        # Idempotent: korrigerade sidor skrivs löpande, så ett avbrott är säkert
        # och behöver inget felspår.
        print('\nAvbrutet.', file=sys.stderr)
        sys.exit(130)
