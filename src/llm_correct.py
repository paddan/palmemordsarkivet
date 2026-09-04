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
from collections import defaultdict
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as _llm_config  # noqa: E402
import db as state_db  # noqa: E402
from normalize_text import normalize  # noqa: E402
from operations.exceptions import OperationFailed  # noqa: E402

HAIKU_MODEL = "claude-haiku-4-5-20251001"

OPENAI_DEFAULT_MODEL = "gpt-4o-mini"


def _resolve_api_key(
    provider: str, base_url: str, explicit_key: str, *, profile_env: str = ""
) -> str:
    """Lös API-nyckeln: explicit värde, profilens ``api_key_env`` eller klassiska env.

    Kastar ``OperationFailed`` med vägledning när en förväntad nyckel saknas —
    undantaget hamnar i jobbstatus/loggen i stället för en tyst ``SystemExit``.
    """
    if explicit_key:
        return explicit_key
    if profile_env:
        key = os.environ.get(profile_env, "")
        if not key:
            raise OperationFailed(f"Sätt miljövariabeln {profile_env}.")
        return key
    if provider == "claude":
        key = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
               os.environ.get("ANTHROPIC_API_KEY") or "")
        if not key:
            raise OperationFailed(
                "Sätt CLAUDE_CODE_OAUTH_TOKEN eller ANTHROPIC_API_KEY."
            )
        return key
    # openai
    if not base_url:
        key = os.environ.get("OPENAI_API_KEY") or ""
        if not key:
            raise OperationFailed("Sätt OPENAI_API_KEY.")
        return key
    # Custom base_url: lokala servrar (Ollama/LM Studio) körs utan nyckel, men
    # kända fjärr-providers kräver sin egen env-nyckel. Matcha på host.
    host = base_url.lower()
    for needle, env in (("deepseek.com", "DEEPSEEK_API_KEY"),
                        ("openai.com", "OPENAI_API_KEY")):
        if needle in host:
            key = os.environ.get(env, "")
            if not key:
                raise OperationFailed(f"Sätt {env}.")
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


async def _test_mode(txt_path: Path, provider_cfg: dict, threshold: float,
                     ctx=None) -> None:
    """Test-läge: rätta en textfil och jämför quality-poäng sida för sida."""
    from quality import has_hunspell_swe, score_text  # noqa: PLC0415

    ctx = _ctx(ctx)
    raw = txt_path.read_text(encoding='utf-8', errors='replace')
    pages = raw.split('\f')
    use_hunspell = has_hunspell_swe()

    ctx.log(f'Test: {txt_path} ({len(pages)} sida{"r" if len(pages) != 1 else ""})'
            f' — {provider_cfg["provider"]}/{provider_cfg["model"]}'
            + (' + hunspell' if use_hunspell else ''))
    ctx.log(f'Tröskel: {threshold:.0f}')

    before: list[float] = []
    corrected_pages: list[str] = []
    for i, page in enumerate(pages, 1):
        ctx.check_cancelled()
        norm = normalize(page)
        s = score_text(norm, use_hunspell).get('score', 0.0)
        before.append(s)
        if not norm.strip() or s >= threshold:
            corrected_pages.append(norm)
            continue
        ctx.log(f'  Sida {i}: score {s:.0f} → rättar…')
        corrected_pages.append(normalize(await _correct_text(norm, provider_cfg)))

    after = [score_text(p, use_hunspell).get('score', 0.0) for p in corrected_pages]

    tmp_dir = ROOT / 'tmp'
    tmp_dir.mkdir(exist_ok=True)
    tmp = tmp_dir / f'llm_test_{txt_path.stem}.txt'
    tmp.write_text('\f'.join(corrected_pages), encoding='utf-8')

    ctx.log(f'\n{"Sida":>5}  {"Före":>6}  {"Efter":>6}  {"Δ":>6}')
    ctx.log('─' * 30)
    for i, (b, a) in enumerate(zip(before, after, strict=False), 1):
        delta = a - b
        marker = ' ↑' if delta > 1 else (' ↓' if delta < -1 else '')
        ctx.log(f'{i:>5}  {b:>6.1f}  {a:>6.1f}  {delta:>+6.1f}{marker}')
    if len(before) > 1:
        avg_b = sum(before) / len(before)
        avg_a = sum(after) / len(after)
        ctx.log('─' * 30)
        ctx.log(f'{"Snitt":>5}  {avg_b:>6.1f}  {avg_a:>6.1f}  {avg_a - avg_b:>+6.1f}')
    ctx.log(f'\nSparat: {tmp}')


async def _correct_all(
    bad: dict[str, list[int]],
    txt_dir: Path,
    provider_cfg: dict,
    dry_run: bool,
    jobs: int = 1,
    ctx=None,
) -> None:
    """Rätta alla dåliga sidor. ``jobs`` styr hur många LLM-anrop som körs
    samtidigt via en delad semafor; sidorna är oberoende av varandra och
    DB-skrivningarna sker synkront i event loop-tråden, så delad sqlite-conn
    är säker. ``merge_one`` körs per fil efter att filens sidor är klara."""
    from merge_pages import merge_one  # noqa: PLC0415

    ctx = _ctx(ctx)
    ctx.step("LLM-korrigering")
    total = sum(len(v) for v in bad.values())
    done = 0

    conn = state_db.connect()
    state_db.init_schema(conn)

    semaphore = asyncio.Semaphore(max(1, jobs))

    def progress(stem: str, p: int, suffix: str) -> None:
        nonlocal done
        done += 1
        # Strukturerad progress till SQLite/jobblogg i stället för print —
        # worker-stdout är DEVNULL och försvinner annars.
        ctx.progress(done, total, f'{stem} sida {p}{suffix}')

    async def handle_file(txt_name: str, pages: list[int]) -> None:
        ctx.check_cancelled()
        stem = txt_name[:-4] if txt_name.endswith('.txt') else txt_name
        txt_path = txt_dir / f'{stem}.txt'
        if not txt_path.exists():
            ctx.log(f'  SAKNAS: {txt_path}', level='error')
            return

        full_text = txt_path.read_text(encoding='utf-8', errors='replace')
        page_texts = full_text.split('\f')
        file_changed = False

        async def handle_page(p: int) -> None:
            nonlocal file_changed
            ctx.check_cancelled()
            idx = p - 1
            if idx < 0 or idx >= len(page_texts):
                ctx.log(f'  [skip] {stem} sida {p}: utanför range '
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
                    ctx.log(f'  [fel] {stem} sida {p}: {e}', level='error')
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
                ctx.log(f'  [merge-fel] {stem}: {e}', level='error')

    await asyncio.gather(
        *(handle_file(name, pages) for name, pages in bad.items())
    )


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_llm_correct(
    *,
    threshold: float,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    txt: Path,
    root: Path,
    jobs: int,
    dry_run: bool,
    test: str | None,
    profile: str = "",
    context=None,
) -> int:
    """LLM-korrigera dåliga OCR-sidor. Returnerar exitkod."""
    ctx = _ctx(context)
    txt_dir = txt

    try:
        saved_cfg = _llm_config.load_profile(profile) if profile else _llm_config.load()
    except ValueError as exc:
        raise OperationFailed(str(exc)) from exc
    # Ett explicit provider-byte ogiltigförklarar sparad modell (annars kan t.ex.
    # --provider openai ärva en claude-modell) — samma semantik som syskonen
    # extract_entities/extract_map_observations.
    saved_model = saved_cfg.get("model", "") if not provider else ""
    provider = provider or saved_cfg.get("provider", "claude")
    if provider not in ("claude", "openai"):
        provider = "claude"
    base_url = base_url or saved_cfg.get("base_url", "")
    default_model = HAIKU_MODEL if provider == 'claude' else OPENAI_DEFAULT_MODEL
    model = model or saved_model or default_model
    # Profilen kan peka ut en egen miljövariabel (api_key_env) för nyckeln —
    # den måste respekteras även i jobb/CLI, inte bara i Utredning.
    profile_env = str(saved_cfg.get("api_key_env") or "").strip()
    api_key = _resolve_api_key(provider, base_url, api_key, profile_env=profile_env)

    provider_cfg = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    }

    if test:
        txt_path = Path(test)
        if not txt_path.is_file():
            ctx.log(f'Filen finns inte: {txt_path}', level="error")
            return 1
        asyncio.run(_test_mode(txt_path, provider_cfg, threshold, ctx=ctx))
        return 0

    conn = state_db.connect()
    state_db.init_schema(conn)

    raw: dict[str, list[int]] = defaultdict(list)
    for row in state_db.get_bad_pages(conn, threshold=threshold):
        raw[row["pdf_stem"] + ".txt"].append(row["page_num"])
    if not raw:
        ctx.log(f'Inga sidor under threshold {threshold} i quality_pages '
                '— kör quality --per-page först.')
        return 0

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
        ctx.log(f'Inga nya sidor att rätta ({skipped} redan rättade).')
        return 0

    ctx.log(f'Rättar {total} sidor i {len(bad)} filer'
            + (f' ({skipped} redan rättade hoppas över)' if skipped else '')
            + f' med {provider}/{model} ({max(1, jobs)} parallella).')
    if dry_run:
        ctx.log('[dry-run — inga filer skrivs]')

    asyncio.run(_correct_all(bad, txt_dir, provider_cfg, dry_run, jobs=jobs, ctx=ctx))

    if not dry_run:
        ctx.log('\nKlart. Kör quality --per-page för att se förbättringen.')
    return 0


def main() -> int:
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
    return run_llm_correct(
        threshold=args.threshold,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        txt=txt_dir,
        root=root,
        jobs=args.jobs,
        dry_run=args.dry_run,
        test=args.test,
    )


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Idempotent: korrigerade sidor skrivs löpande, så ett avbrott är säkert
        # och behöver inget felspår.
        print('\nAvbrutet.', file=sys.stderr)
        sys.exit(130)
