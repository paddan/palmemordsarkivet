"""LLM-baserad extraktor av granskningsbara kartobservations-kandidater.

Läser generated/text/<stem>.txt sida för sida (\\f-separerade), skickar varje
sida till den konfigurerade LLM:en (se ``generated/llm_config.json``, default
Claude Haiku) och sparar tidsatta person-positioner som *kandidater* i
state.db-tabellen ``map_observation_candidates``. Extraktorn skriver aldrig
direkt till kartans publicerade observationer — kandidaterna granskas och
godkänns manuellt i Karta-fliken.

Kör:
    python -m extract_map_observations [--limit 5] [--dry-run]
    python -m extract_map_observations --provider openai --model gpt-4o-mini
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    TextBlock,
    query,
)

import config as _llm_config  # noqa: E402
import db as state_db  # noqa: E402
from llm_correct import _resolve_api_key  # noqa: E402
from map_extract import (  # noqa: E402
    build_place_index,
    candidate_payload,
    parse_map_observation_extraction,
)

try:
    from openai import AsyncOpenAI
except ImportError:
    AsyncOpenAI = None  # type: ignore[assignment,misc]

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"
MIN_PAGE_ALNUM = 100  # sidor med färre alfanumeriska tecken hoppas över (bildsidor)

_SYSTEM = """\
Du extraherar ENDAST källbelagda observationer där en person befinner sig på
en plats vid en tidpunkt under mordkvällen 28 februari 1986.
Returnera ENBART giltig JSON på exakt denna form:
{"observationer": [{
  "person": "Förnamn Efternamn",
  "plats": "platsnamn ur texten",
  "tid": "ca 21.15",
  "citat": "kort ordagrant citat eller mycket nära textutdrag",
  "notering": "kort neutral sammanfattning",
  "confidence": "low|medium|high"
}]}
Regler:
- Ta bara med observationer där texten explicit säger eller starkt anger att
  en person var, sågs, gick, anlände, lämnade eller befann sig vid en plats.
- Gissa inte koordinater. Skriv platsnamnet som det står eller som tydligast
  framgår av sidan.
- Tid får vara exakt eller osäker, t.ex. "ca 21.15", "strax efter 23.20".
- Ta inte med allmänna biografier, adresser, arbetsplatser eller hypotetiska
  resonemang om de inte placerar personen där vid tidpunkten.
- "citat" ska vara kort och källnära så granskaren kan avgöra om kandidaten
  är rimlig.
- Om sidan inte innehåller sådana observationer: {"observationer": []}."""


def select_candidate_pages(
    conn, pdf_stem: str, pages: list[str]
) -> list[tuple[int, str]]:
    """Välj sidor som kan innehålla kartobservationer.

    Hoppar bildsidor (< MIN_PAGE_ALNUM alfanumeriska tecken) och sidor som
    redan har minst en kandidat."""
    out: list[tuple[int, str]] = []
    for idx, text in enumerate(pages, start=1):
        if sum(1 for c in text if c.isalnum()) < MIN_PAGE_ALNUM:
            continue
        if state_db.map_observation_extracted(conn, pdf_stem, idx):
            continue
        if state_db.map_observation_candidate_exists(conn, pdf_stem, idx):
            continue
        out.append((idx, text))
    return out


def resolve_provider_cfg(saved: dict, *, provider: str = "", model: str = "",
                         base_url: str = "") -> dict:
    """Slå ihop sparad llm_config med CLI-overrides → {provider, model, base_url}.

    Samma semantik som llm_correct/extract_entities: CLI-provider ogiltigförklarar
    sparad modell (annars kan t.ex. --provider openai ärva en claude-modell)."""
    prov = provider or saved.get("provider", "claude")
    if prov not in ("claude", "openai"):
        prov = "claude"
    saved_model = saved.get("model", "") if not provider else ""
    default = DEFAULT_CLAUDE_MODEL if prov == "claude" else OPENAI_DEFAULT_MODEL
    return {
        "provider": prov,
        "model": model or saved_model or default,
        "base_url": base_url or saved.get("base_url", ""),
    }


async def _claude_call(text: str, model: str) -> str:
    """Skicka en sidtext till Claude, returnera råsvaret."""
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
    return "".join(parts)


async def _openai_call(text: str, model: str, base_url: str, api_key: str) -> str:
    """Skicka en sidtext till en OpenAI-kompatibel modell, returnera råsvaret."""
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
    return response.choices[0].message.content or ""


async def _call_llm(text: str, cfg: dict) -> str:
    """Skicka en sidtext till konfigurerad LLM, returnera råsvaret."""
    if cfg["provider"] == "claude":
        return await _claude_call(text, cfg["model"])
    return await _openai_call(text, cfg["model"], cfg["base_url"], cfg["api_key"])


def fmt_eta(seconds: float) -> str:
    """Formatera sekunder som '2h05m' eller '4m32s' (timmar när det behövs)."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m{s % 60:02d}s"


async def extract_doc(conn, txt_path: Path, *, cfg: dict, dry_run: bool,
                      place_index: list[dict], timeout: float = 120.0,
                      jobs: int = 1, on_page=None) -> int:
    """Extrahera kvarvarande sidor i ett dokument. Returnerar antal försökta sidor.

    ``on_page(page_num)`` anropas efter varje försökt sida, men inte vid dry-run
    (då görs inga LLM-anrop alls). ``timeout`` är max väntetid per LLM-anrop;
    ``jobs`` styr hur många sidor som bearbetas samtidigt (via en semafor)."""
    stem = txt_path.stem
    nr = stem.split(" — ")[0].strip()
    pages = txt_path.read_text(encoding="utf-8", errors="replace").split("\f")
    todo = select_candidate_pages(conn, stem, pages)
    if dry_run:
        return len(todo)

    semaphore = asyncio.Semaphore(max(1, jobs))

    async def handle_page(page_num: int, text: str) -> None:
        async with semaphore:
            try:
                # [:6000] — begränsa promptstorlek/kostnad; sidor är sällan längre.
                raw = await asyncio.wait_for(
                    _call_llm(text[:6000], cfg), timeout=timeout
                )
                stored = 0
                for obs in parse_map_observation_extraction(raw):
                    payload = candidate_payload(
                        obs,
                        pdf_stem=stem,
                        page_num=page_num,
                        nr=nr,
                        model=cfg["model"],
                        place_index=place_index,
                    )
                    if payload:
                        state_db.record_map_observation_candidate(conn, **payload)
                        stored += 1
                state_db.mark_map_observation_extracted(
                    conn,
                    pdf_stem=stem,
                    page_num=page_num,
                    model=cfg["model"],
                    observations=stored,
                )
            except TimeoutError:
                msg = f"timeout efter {timeout:.0f}s"
                print(f"  [{stem} p{page_num}] FEL: {msg}", file=sys.stderr)
                log_error("extract_map_observations", f"{txt_path.name}#p{page_num}", msg)
            except Exception as e:  # noqa: BLE001
                print(f"  [{stem} p{page_num}] FEL: {e}", file=sys.stderr)
                log_error("extract_map_observations", f"{txt_path.name}#p{page_num}", str(e))
            finally:
                if on_page is not None:
                    on_page(page_num)

    await asyncio.gather(*(handle_page(p, t) for p, t in todo))
    return len(todo)


def _load_place_index() -> list[dict]:
    places_path = ROOT / "data" / "karta" / "platser.json"
    places = json.loads(places_path.read_text(encoding="utf-8")) if places_path.exists() else []
    index: list[dict] = build_place_index(places)
    return index


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_extract_map_observations(
    *,
    text_dir: Path,
    limit: int | None,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    dry_run: bool,
    jobs: int,
    timeout: float,
    profile: str = "",
    context=None,
) -> int:
    """Extrahera kartobservationskandidater. Returnerar exitkod."""
    ctx = _ctx(context)
    base_cfg = _llm_config.load_profile(profile) if profile else _llm_config.load()
    cfg = resolve_provider_cfg(
        base_cfg,
        provider=provider, model=model, base_url=base_url,
    )
    if dry_run:
        cfg["api_key"] = ""
    else:
        profile_env = str(base_cfg.get("api_key_env") or "").strip()
        cfg["api_key"] = _resolve_api_key(
            cfg["provider"], cfg["base_url"], api_key, profile_env=profile_env
        )

    files = sorted(text_dir.glob("*.txt"))
    if limit:
        files = files[: limit]
    if not files:
        ctx.log(f"Inga .txt-filer i {text_dir}/", level="error")
        return 1

    conn = state_db.connect()
    state_db.init_schema(conn)
    place_index = _load_place_index()

    work: list[tuple[Path, int]] = []
    total_pages = 0
    for f in files:
        pages = f.read_text(encoding="utf-8", errors="replace").split("\f")
        n = len(select_candidate_pages(conn, f.stem, pages))
        if n:
            work.append((f, n))
            total_pages += n
    skipped_docs = len(files) - len(work)
    ctx.log(f"Hittade {total_pages} sidor att granska i {len(work)} dokument "
            f"({skipped_docs} dokument redan klara/utan sidor).")

    if dry_run:
        ctx.log(f"[dry-run] Klart: {total_pages} sidor i {len(work)} dokument.")
        return 0

    ctx.log(f"Extraherar med {cfg['provider']}/{cfg['model']} "
            f"({jobs} parallella sidor).")

    done = 0
    attempted_pages = 0
    for f, _n in work:
        ctx.check_cancelled()

        def on_page(page_num: int, stem: str = f.stem) -> None:
            nonlocal done
            done += 1
            ctx.progress(done, total_pages, f"{stem} sida {page_num}")

        attempted_pages += asyncio.run(
            extract_doc(conn, f, cfg=cfg, dry_run=False, place_index=place_index,
                        timeout=timeout, jobs=jobs, on_page=on_page)
        )

    ctx.log(f"Klart: {attempted_pages} sidor i {len(work)} dokument.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text-dir", default=os.environ.get(
        "TEXT_DIR", str(ROOT / "generated" / "text")))
    ap.add_argument("--limit", type=int, help="max antal dokument (testkörning)")
    ap.add_argument("--provider", default="",
                    help="LLM-provider: claude eller openai (default: från llm_config.json)")
    ap.add_argument("--model", default="",
                    help="modellnamn (default: från llm_config.json)")
    ap.add_argument("--base-url", default="",
                    help="override API-URL för OpenAI-kompatibla providers (Ollama, DeepSeek, ...)")
    ap.add_argument("--api-key", default="",
                    help="override API-nyckel (annars läses från env)")
    ap.add_argument("--dry-run", action="store_true",
                    help="visa antal sidor utan att anropa LLM")
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("JOBS", "4")),
                    help="antal parallella sidanrop (default: env JOBS eller 4)")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="max väntetid i sekunder per LLM-anrop (default: 120)")
    args = ap.parse_args()

    return run_extract_map_observations(
        text_dir=Path(args.text_dir),
        limit=args.limit,
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        dry_run=args.dry_run,
        jobs=args.jobs,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Allt arbete är idempotent (kandidater dedupas) — ett avbrott behöver
        # inget felspår.
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
