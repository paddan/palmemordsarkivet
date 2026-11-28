"""Operationen ``extract-mordnatten``: urval → LLM per sida → SQLite → Neo4j.

Flödet:

1. Profilen löses först (``config.load_profile`` + ``resolve_runtime_profile``);
   okänd profil, en backend som inte är ``claude``/``openai`` eller en saknad
   nyckel stoppar körningen före all LLM-kontakt. Vid ``dry_run`` blir det bara
   en varning (priserna behövs för uppskattningen), vid ``load_only`` löses ingen
   profil alls.
2. Urvalet (``ontologi.selection.select_pages``) får dokumentmetadata, sidtexter
   (``\\f``-indelning som ingest), tidigare extraktioner, sökträffar för
   ``urval.sokfragor`` och de sidor som har granskade observationer i Neo4j.
   Går grafen inte att läsa behandlas alla redan extraherade sidor som granskade.
3. ``dry_run`` loggar urvalet och en kostnadsuppskattning och skriver ingenting.
4. Annars körs sidorna med ``asyncio`` och högst ``jobs`` samtidiga anrop. Varje
   råsvar tolkas med ``parse_response`` och sparas i ``ontology_extractions``
   direkt — SQLite är den beständiga kopian; grafen är ett pekregister som alltid
   kan byggas om med ``--load-only``. Ett avklippt eller otolkbart svar och ett
   oväntat fel på en sida sparas inte: sidan räknas som misslyckad, loggas och tas
   om nästa körning.
5. Sist läses de extraherade sidorna in med ``load_ontology``. Svarar inte Neo4j
   ligger råsvaren kvar och körningen lyckas med en varning.

Beroenden (LLM, sökning, Neo4j) injiceras via ``Deps`` så att testerna aldrig
når nätet. Nycklar och hela sidtexter loggas aldrig.
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config as _llm_config
import db as state_db
import errors_log
from backends import BACKENDS
from graph import viz
from ontologi import llm as _llm
from ontologi.config import OntologiConfig, load_config
from ontologi.extraction import SYSTEM_PROMPT, build_prompt, parse_response, text_hash
from ontologi.load_graph import load_ontology
from ontologi.selection import PageRef, select_pages, series_of
from operations.exceptions import OperationCancelled, OperationFailed

SOK_TOPP = 20
# Kostnadsuppskattning per sida: tecken/4 + fast prompt in, ett typiskt svar ut.
_PROMPT_TOKEN = 1500
# Uppmätt i piloten: ~2 400 utdata-token per sida (176 974 token / 74 sidor).
_SVAR_TOKEN = 2500
_GRAF_NERE = "Grafen svarar inte — kör igen med --load-only när Neo4j är igång."

_GRANSKADE_CYPHER = """
MATCH (o:Observation)-[:KÄLLA]->(s:Sida)
WHERE o.status <> 'ogranskad'
RETURN DISTINCT s.stem AS stem, s.sida AS sida
"""


RAG_DIR = Path(__file__).resolve().parents[1] / "rag"


def _ensure_rag_path() -> None:
    """Lägg src/rag först i sys.path före import av ``rag.ask``.

    rag.ask gör ``from ingest import …`` och förutsätter att src/rag ligger först
    (som i Utredning.py). Körd via scripts/ hittade Python annars
    scripts/ingest.py och importen föll."""
    rag_dir = str(RAG_DIR)
    if sys.path[:1] != [rag_dir]:
        sys.path.insert(0, rag_dir)


def _default_search(fragor: list[str], top_k: int) -> list[tuple[str, int]]:
    """Hybridsök i LanceDB (ingen reranker); laddar index och modell lat."""
    import lancedb  # noqa: PLC0415 — tung import, bara när sökfrågor finns
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    _ensure_rag_path()
    from rag.ask import DB_DIR, EMBED_MODEL, TABLE, search_hybrid  # noqa: PLC0415

    table = lancedb.connect(str(DB_DIR)).open_table(TABLE)
    model = SentenceTransformer(EMBED_MODEL)
    traffar: list[tuple[str, int]] = []
    for fraga in fragor:
        for hit in search_hybrid(table, model, fraga, top_k):
            source, page = hit.get("source"), hit.get("page")
            if not isinstance(source, str) or not isinstance(page, int):
                continue
            traffar.append((source.removesuffix(".txt"), page))
    return traffar


def _default_driver(uri: str, auth: tuple[str, str]) -> Any:
    from neo4j import GraphDatabase  # noqa: PLC0415 — kräver extras [graph]

    return GraphDatabase.driver(uri, auth=auth)


@dataclass
class Deps:
    """Utbytbara beroenden; standardvärdena är de riktiga."""

    llm: Callable[[str, str, dict], Awaitable[tuple[str, dict]]] = _llm.call_page
    search: Callable[[list[str], int], list[tuple[str, int]]] = _default_search
    driver_factory: Callable[..., Any] = _default_driver
    load: Callable[..., dict[str, int]] = load_ontology
    password: Callable[[], str | None] = viz.resolve_password
    load_config: Callable[[], OntologiConfig] = load_config


@dataclass
class _Resultat:
    sparade: list[tuple[str, int]] = field(default_factory=list)
    token_in: int = 0
    token_ut: int = 0
    observationer: int = 0
    varningar: list[str] = field(default_factory=list)
    misslyckade: int = 0
    fatal: str | None = None
    avbruten: OperationCancelled | None = None


def _ctx(context):
    if context is not None:
        return context
    from operations.context import ensure_terminal_context  # noqa: PLC0415

    return ensure_terminal_context(None)


def _neo4j_fel() -> tuple[type[BaseException], ...]:
    """Fel som betyder att grafen inte går att nå (inte ett programfel)."""
    fel: list[type[BaseException]] = [OSError]
    try:
        from neo4j.exceptions import AuthError, ServiceUnavailable, SessionExpired
    except ImportError:  # pragma: no cover — extras [graph] saknas
        return tuple(fel)
    return (*fel, ServiceUnavailable, SessionExpired, AuthError)


def _resolve_profile(profile: str) -> tuple[str, dict]:
    """Profilnamn och upplöst runtime-profil; höjer ``OperationFailed``."""
    try:
        if profile:
            namn = profile
            sparad = _llm_config.load_profile(profile)
        else:
            alla = _llm_config.load_all()
            namn = alla["default"]
            sparad = alla["profiles"][namn]
        cfg = _llm_config.resolve_runtime_profile(sparad, BACKENDS)
    except (KeyError, ValueError) as exc:
        raise OperationFailed(f"LLM-profilen kunde inte läsas: {exc}") from exc
    if cfg.get("kind") not in {"claude", "openai"}:
        raise OperationFailed(f"LLM-backenden {cfg.get('kind')!r} stöds inte.")
    if cfg["kind"] == "openai" and cfg.get("api_key_env") and not cfg.get("api_key"):
        raise OperationFailed(
            f"Miljövariabeln {cfg['api_key_env']} saknas för LLM-profilen {namn!r}.")
    return namn, cfg


def _split_pages(text: str) -> list[str]:
    """Sidindelning som ingest: ``\\f``, utan ett tomt sista segment."""
    sidor = text.split("\f")
    if len(sidor) > 1 and not sidor[-1].strip():
        sidor.pop()
    return sidor


def _page_texts(stems: set[str], text_dir: Path, ctx) -> dict[str, list[str]]:
    texter: dict[str, list[str]] = {}
    saknas: list[str] = []
    for stem in sorted(stems):
        path = text_dir / f"{stem}.txt"
        if path.is_file():
            texter[stem] = _split_pages(path.read_text(encoding="utf-8"))
        else:
            saknas.append(stem)
    ctx.log(f"Sidtexter: {len(texter)} dokument lästa, {len(saknas)} saknar textfil "
            f"(första: {saknas[:5]})", level="debug")
    return texter


def _reviewed(deps: Deps, uri: str, user: str, ctx) -> set[tuple[str, int]] | str:
    """Sidor med granskade observationer, eller orsaken (sträng) om grafen inte går att läsa."""
    password = deps.password()
    if not password:
        return "Neo4j-lösenord saknas"
    try:
        with deps.driver_factory(uri, auth=(user, password)) as driver:
            driver.verify_connectivity()
            with driver.session() as session:
                rows = session.run(_GRANSKADE_CYPHER).data()
    except _neo4j_fel() as exc:
        return f"grafen svarar inte ({type(exc).__name__})"
    granskade = {(r["stem"], int(r["sida"])) for r in rows}
    ctx.log(f"Granskade sidor i grafen: {len(granskade)}", level="debug")
    return granskade


def _select(cfg: OntologiConfig, deps: Deps, *, max_pages: int, force: bool, uri: str,
            user: str, text_dir: Path, ctx) -> tuple[list[PageRef], dict[str, int],
                                                     dict[str, list[str]]]:
    with closing(state_db.connect()) as conn:
        state_db.init_schema(conn)
        metas = state_db.list_document_meta(conn)
        extraktioner = {(r["pdf_stem"], r["sida"]): r
                        for r in state_db.list_ontology_extractions(conn)}
    urval = cfg.urval
    sokträffar: list[tuple[str, int]] = []
    if urval.sokfragor:
        sokträffar = deps.search(list(urval.sokfragor), SOK_TOPP)
        ctx.log(f"Sökfrågor: {len(urval.sokfragor)} frågor gav {len(sokträffar)} träffar "
                f"(första: {sokträffar[:5]})", level="debug")
    serier = set(urval.serier)
    stammar = {m["pdf_stem"] for m in metas
               if any(series_of(k) in serier for k in m.get("uppslag") or [])}
    stammar |= {s for s, _ in sokträffar} | set(urval.stammar) | {s for s, _ in urval.sidor}
    texter = _page_texts(stammar, Path(text_dir), ctx)
    granskade_eller_orsak = _reviewed(deps, uri, user, ctx)
    if isinstance(granskade_eller_orsak, str):
        # Utan grafen vet vi inte vilka sidor som granskats. Hellre skydda alla
        # redan extraherade sidor än att skriva över granskade observationer.
        granskade = set(extraktioner)
        ctx.log(f"Granskade sidor kan inte läsas: {granskade_eller_orsak} — alla "
                f"{len(granskade)} redan extraherade sidor behandlas som granskade "
                "(--force väljer ändå om dem vid ny schemaversion).", level="warning")
    else:
        granskade = granskade_eller_orsak
    tak = max_pages if max_pages > 0 else urval.max_sidor
    valda, stats = select_pages(
        metas, texter, urval, extraktioner, schemaversion=cfg.schemaversion,
        reviewed=granskade, search_hits=sokträffar, max_pages=tak, force=force,
    )
    ctx.log("Urval: " + ", ".join(f"{k} {v}" for k, v in stats.items()), level="debug")
    return valda, stats, texter


def _estimate(valda: list[PageRef], texter: dict[str, list[str]], prices: dict) -> str:
    if "input" not in prices or "output" not in prices:
        return "kostnad okänd"
    in_pris, ut_pris = prices["input"] / 1_000_000, prices["output"] / 1_000_000
    summa = sum((len(texter[r.pdf_stem][r.sida - 1]) / 4 + _PROMPT_TOKEN) * in_pris
                + _SVAR_TOKEN * ut_pris for r in valda)
    return f"uppskattad kostnad ${summa:.2f}"


def _dry_run(profile: str, valda: list[PageRef], stats: dict[str, int],
             texter: dict[str, list[str]], ctx) -> None:
    prices: dict = {}
    try:
        _, cfg = _resolve_profile(profile)
        prices = cfg.get("prices") or {}
    except OperationFailed as exc:
        ctx.log(f"Torrkörning: {exc}", level="warning")
    skal = ", ".join(f"{k} {stats[k]}" for k in ("serie", "sok", "uttrycklig"))
    bortvalda = ", ".join(f"{k} {stats[k]}" for k in
                          ("kort", "oförändrad", "granskad", "avkortade", "utanför"))
    ctx.log(f"Torrkörning: {len(valda)} sidor valda ({skal}); bortvalda: {bortvalda}.")
    for ref in valda[:10]:
        ctx.log(f"  {ref.pdf_stem} sida {ref.sida} ({ref.skal})")
    ctx.log(f"Torrkörning: {_estimate(valda, texter, prices)} för {len(valda)} sidor. "
            "Ingenting skrivs.")


async def _extract(valda: list[PageRef], texter: dict[str, list[str]],
                   cfg: OntologiConfig, llm_cfg: dict, profile_name: str,
                   deps: Deps, jobs: int, conn, ctx) -> _Resultat:
    res = _Resultat()
    sem = asyncio.Semaphore(max(1, jobs))
    klara = 0

    async def en_sida(ref: PageRef) -> None:
        nonlocal klara
        async with sem:
            if res.fatal or res.avbruten:
                return
            try:
                ctx.check_cancelled()
            except OperationCancelled as exc:
                res.avbruten = exc
                return
            text = texter[ref.pdf_stem][ref.sida - 1]
            item = f"{ref.pdf_stem} s{ref.sida}"
            try:
                raw, usage = await deps.llm(build_prompt(text, cfg), SYSTEM_PROMPT, llm_cfg)
                parsed = parse_response(raw, text, cfg)
                ogiltiga = [v for v in parsed.varningar if v.startswith("ogiltigt svar")]
                if ogiltiga:
                    # Ett svar som inte går att tolka är ett misslyckat anrop, inte en
                    # sida utan iakttagelser — sparas det väljs sidan aldrig om.
                    raise _llm.LlmPageError(ogiltiga[0])
                state_db.upsert_ontology_extraction(conn, {
                    "pdf_stem": ref.pdf_stem, "sida": ref.sida, "text_hash": text_hash(text),
                    "schemaversion": cfg.schemaversion, "profile": profile_name,
                    "model": llm_cfg.get("model") or "", "raw": raw,
                    "parsed": {"observationer": parsed.observationer,
                               "varningar": parsed.varningar},
                    "n_observationer": len(parsed.observationer),
                })
            except _llm.LlmFatal as exc:
                if res.fatal is None:
                    res.fatal = str(exc)
                ctx.log(f"{ref.pdf_stem} sida {ref.sida}: {exc} — inga fler anrop.",
                        level="error")
                errors_log.log_error("extract-mordnatten", item, f"{exc} — inga fler anrop")
                return
            except _llm.LlmPageError as exc:
                res.misslyckade += 1
                ctx.log(f"{ref.pdf_stem} sida {ref.sida}: {exc}", level="warning")
                errors_log.log_error("extract-mordnatten", item, str(exc))
                return
            except Exception as exc:  # noqa: BLE001 — ett sidfel ska inte fälla körningen
                res.misslyckade += 1
                orsak = f"oväntat fel {type(exc).__name__}: {exc}"
                ctx.log(f"{ref.pdf_stem} sida {ref.sida}: {orsak}", level="error")
                errors_log.log_error("extract-mordnatten", item, orsak)
                return
            res.sparade.append((ref.pdf_stem, ref.sida))
            res.token_in += usage.get("input_tokens") or 0
            res.token_ut += usage.get("output_tokens") or 0
            res.observationer += len(parsed.observationer)
            res.varningar.extend(parsed.varningar)
            klara += 1
            ctx.progress(klara, len(valda), f"{ref.pdf_stem} sida {ref.sida}")
            if parsed.varningar:
                ctx.log(f"{ref.pdf_stem} sida {ref.sida}: {len(parsed.observationer)} "
                        f"observationer, {len(parsed.varningar)} varningar", level="debug")

    await asyncio.gather(*(en_sida(ref) for ref in valda))
    return res


def _log_warnings(varningar: list[str], ctx) -> None:
    """Varningar grupperade på prefixet före första kolon, med högst fem exempel."""
    grupper: dict[str, list[str]] = defaultdict(list)
    for v in varningar:
        grupper[v.split(":", 1)[0]].append(v)
    for prefix, antal in Counter({k: len(v) for k, v in grupper.items()}).most_common():
        ctx.log(f"Varningar «{prefix}»: {antal} st (exempel: {grupper[prefix][:5]})",
                level="debug")


def _load(rows: list[dict], cfg: OntologiConfig, deps: Deps, uri: str, user: str,
          ctx) -> bool:
    """Läs in raderna i grafen; False (med varning) om grafen inte svarar."""
    if not rows:
        ctx.log("Inga extraktioner att läsa in i grafen.", level="debug")
        return True
    password = deps.password()
    if not password:
        ctx.log("Neo4j-lösenord saknas. " + _GRAF_NERE, level="warning")
        return False
    try:
        with deps.driver_factory(uri, auth=(user, password)) as driver:
            driver.verify_connectivity()
            with driver.session() as session:
                deps.load(session, rows, cfg, ctx=ctx)
    except _neo4j_fel() as exc:
        ctx.log(f"{_GRAF_NERE} ({type(exc).__name__})", level="warning")
        return False
    return True


def _rows_for(keys: list[tuple[str, int]] | None) -> list[dict]:
    with closing(state_db.connect()) as conn:
        state_db.init_schema(conn)
        rader: list[dict] = state_db.list_ontology_extractions(conn)
    if keys is None:
        return rader
    onskade = set(keys)
    return [r for r in rader if (r["pdf_stem"], r["sida"]) in onskade]


def run_extract_mordnatten(
    *, profile: str, max_pages: int, dry_run: bool, force: bool, load_only: bool,
    jobs: int, uri: str, user: str, text_dir: Path, context=None,
    deps: Deps | None = None,
) -> int:
    """Extrahera mordnattens observationer och läs in dem i grafen. Returnerar exitkod."""
    ctx = _ctx(context)
    deps = deps or Deps()
    if jobs < 1:
        raise OperationFailed("Antalet parallella anrop måste vara minst 1.")
    if max_pages < 0:
        raise OperationFailed("Max antal sidor får inte vara negativt.")
    try:
        cfg = deps.load_config()
    except ValueError as exc:
        raise OperationFailed(f"Ontologins konfiguration är ogiltig: {exc}") from exc
    if cfg.handelser_utkast:
        ctx.log("Ankarhändelserna är ett utkast — rätta data/ontologi/ankarhandelser.json "
                'och ta bort "utkast".', level="warning")

    if load_only:
        rader = _rows_for(None)
        ctx.log(f"Bara inläsning: {len(rader)} extraherade sidor i state.db.")
        _load(rader, cfg, deps, uri, user, ctx)
        ctx.log(f"Mordnatten: 0 sidor extraherade (0 token in, 0 ut), "
                f"{sum(r['n_observationer'] for r in rader)} observationer, 0 varningar.")
        return 0

    profile_name, llm_cfg = ("", {}) if dry_run else _resolve_profile(profile)
    if not dry_run and not profile:
        ctx.log(f"Ingen LLM-profil angiven — standardprofilen {profile_name!r} används.",
                level="warning")
    valda, stats, texter = _select(cfg, deps, max_pages=max_pages, force=force, uri=uri,
                                   user=user, text_dir=Path(text_dir), ctx=ctx)
    if dry_run:
        _dry_run(profile, valda, stats, texter, ctx)
        return 0

    ctx.log(f"Extraherar {len(valda)} sidor med profilen {profile_name!r} "
            f"({llm_cfg.get('model')}), {jobs} parallella anrop.")
    with closing(state_db.connect()) as conn:
        state_db.init_schema(conn)
        res = asyncio.run(_extract(valda, texter, cfg, llm_cfg, profile_name, deps,
                                   jobs, conn, ctx))

    _log_warnings(res.varningar, ctx)
    load_ctx = ctx.without_cancellation() if res.avbruten else ctx
    _load(_rows_for(res.sparade), cfg, deps, uri, user, load_ctx)
    ctx.log(f"Mordnatten: {len(res.sparade)} sidor extraherade ({res.token_in} token in, "
            f"{res.token_ut} ut), {res.observationer} observationer, "
            f"{len(res.varningar)} varningar.")
    if res.misslyckade:
        ctx.log(f"{res.misslyckade} sidor misslyckades och tas om vid nästa körning.",
                level="warning")
    if res.avbruten:
        raise res.avbruten
    if res.fatal:
        raise OperationFailed(
            f"LLM-tjänsten avbröt körningen: {res.fatal}. {len(res.sparade)} sidor "
            "sparades och lästes in.")
    if res.misslyckade and not res.sparade:
        return 1
    return 0
