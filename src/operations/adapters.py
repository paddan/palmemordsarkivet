"""Adapters som mappar registry-parametrar till domänmodulernas run-funktioner.

Adapters anropar aldrig modulernas ``main()`` — de kallar ``run_*`` direkt med
explicita keyword-argument och valfri ``context``. En icke-noll exitkod från en
run-funktion omvandlas till ``OperationFailed`` så att jobbet markeras misslyckat.

Domänmodulerna importeras **lata** inuti adapterfunktionerna: flera av dem drar
in tunga beroenden (lancedb/sentence-transformers via ``rag.ingest``,
claude_agent_sdk via LLM-modulerna m.fl.). En modulnivåimport skulle göra att
varje ``--help`` betalar hela importkedjan (~4 s).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .exceptions import OperationFailed

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _consume_root(params: Mapping, *path_names: str) -> dict:
    """Tillämpa legacy-wrapperns --root-semantik på relativa/defaulta sökvägar."""
    result = dict(params)
    root = Path(result.pop("root", PROJECT_ROOT))
    for name in path_names:
        value = result.get(name)
        if value is None:
            continue
        path = Path(value)
        if not path.is_absolute():
            result[name] = root / path
        elif path.is_relative_to(PROJECT_ROOT):
            result[name] = root / path.relative_to(PROJECT_ROOT)
    return result


def _raise_on_failure(label: str, exit_code: int | None) -> None:
    if exit_code:
        raise OperationFailed(f"{label} misslyckades (exitkod {exit_code})")


def download_adapter(context, params: Mapping) -> None:
    import download

    params = _consume_root(params, "out")

    rc = download.run_download(
        out=params["out"],
        sheet_id=params["sheet_id"] or download.SHEET_ID,
        limit=params["limit"],
        context=context,
    )
    _raise_on_failure("download", rc)


def download_wpu_adapter(context, params: Mapping) -> None:
    import download_wpu

    params = _consume_root(params, "out")

    rc = download_wpu.run_download_wpu(
        out=params["out"],
        dry_run=params["dry_run"],
        limit=params["limit"],
        rebuild=params["rebuild"],
        context=context,
    )
    _raise_on_failure("download-wpu", rc)


def quality_adapter(context, params: Mapping) -> None:
    import quality

    params = _consume_root(params, "text_dir", "files_dir", "files_from")

    rc = quality.run_quality(
        top=params.get("top"),
        limit=params.get("limit"),
        per_page=params["per_page"],
        text_dir=params["text_dir"],
        files_dir=params["files_dir"],
        rebuild=params["rebuild"],
        files_from=params.get("files_from"),
        context=context,
    )
    _raise_on_failure("quality", rc)


def normalize_adapter(context, params: Mapping) -> None:
    import normalize_text

    # run_normalize returnerar antal ändrade filer (inte en exitkod).
    normalize_text.run_normalize(
        root=params["root"],
        txt_dir=params["txt"],
        dry_run=params["dry_run"],
        stats=params["stats"],
        rebuild=params["rebuild"],
        files_from=params.get("files_from"),
        context=context,
    )


def merge_pages_adapter(context, params: Mapping) -> None:
    import merge_pages

    merge_pages.run_merge_pages(
        stem=params.get("stem"),
        merge_all=params["all"],
        txt_dir=params["txt_dir"],
        context=context,
    )


def merge_wpu_adapter(context, params: Mapping) -> None:
    import merge_wpu

    params = _consume_root(params, "wpu_dir", "text_dir", "ocr_dir")

    rc = merge_wpu.run_merge_wpu(
        dry_run=params["dry_run"],
        rebuild=params["rebuild"],
        margin=params["margin"],
        wpu_dir=params["wpu_dir"],
        text_dir=params["text_dir"],
        ocr_dir=params["ocr_dir"],
        jobs=params["jobs"],
        context=context,
    )
    _raise_on_failure("merge-wpu", rc)


def build_user_words_adapter(context, params: Mapping) -> None:
    import build_user_words

    params = _consume_root(params, "text_dir", "out", "user_words")

    rc = build_user_words.run_build_user_words(
        text_dir=params["text_dir"],
        out=params["out"],
        user_words=params["user_words"],
        min_freq=params["min_freq"],
        rebuild=params["rebuild"],
        context=context,
    )
    _raise_on_failure("build-user-words", rc)


def ingest_adapter(context, params: Mapping) -> None:
    from rag import ingest

    params = _consume_root(
        params, "text_dir", "db_dir", "unusable_list"
    )

    reindex_since_raw = params.get("reindex_since")
    try:
        reindex_since = (
            ingest.parse_reindex_since(reindex_since_raw) if reindex_since_raw else None
        )
    except ValueError as exc:
        # Vänligt fel i stället för traceback (förgrundskörning och jobb delar
        # vägen genom denna adapter).
        raise OperationFailed(f"Ogiltigt --reindex-since: {exc}") from exc
    rc = ingest.run_ingest(
        rebuild=params["rebuild"],
        limit=params.get("limit"),
        text_dir=params["text_dir"],
        db_dir=params["db_dir"],
        chunk_chars=params["chunk_chars"],
        chunk_overlap=params["chunk_overlap"],
        model_name=params["model"],
        unusable_list=params["unusable_list"],
        reindex_since=reindex_since,
        context=context,
    )
    _raise_on_failure("ingest", rc)


def llm_config_adapter(context, params: Mapping) -> None:
    import llm_config_cli

    rc = llm_config_cli.run_llm_config(
        provider=params.get("provider"),
        model=params.get("model"),
        base_url=params.get("base_url"),
        reset=params["reset"],
        context=context,
    )
    _raise_on_failure("llm-config", rc)


def load_graph_adapter(context, params: Mapping) -> None:
    from graph import load_neo4j

    rc = load_neo4j.run_load_graph(
        uri=params["uri"],
        user=params["user"],
        batch=params["batch"],
        context=context,
    )
    _raise_on_failure("load-graph", rc)


def llm_correct_adapter(context, params: Mapping) -> None:
    import llm_correct

    rc = llm_correct.run_llm_correct(
        threshold=params["threshold"],
        provider=params.get("provider", ""),
        model=params.get("model", ""),
        base_url=params.get("base_url", ""),
        api_key=params.get("api_key", ""),
        txt=params["txt"],
        root=params["root"],
        jobs=params["jobs"],
        dry_run=params["dry_run"],
        test=params.get("test"),
        profile=params.get("profile", ""),
        context=context,
    )
    _raise_on_failure("llm-correct", rc)


def ocr_pages_adapter(context, params: Mapping) -> None:
    import ocr_pages

    params = _consume_root(params, "in", "out_dir", "ocr_dir", "txt_dir")

    rc = ocr_pages.run_ocr_pages(
        inp=params["in"],
        out_dir=params.get("out_dir"),
        engine=params["engine"],
        langs=params["langs"],
        dpi=params["dpi"],
        pages=params.get("pages"),
        ocr_dir=params.get("ocr_dir"),
        no_update_pdf=params["no_update_pdf"],
        no_detect_redactions=params["no_detect_redactions"],
        txt_dir=params.get("txt_dir"),
        context=context,
    )
    _raise_on_failure("ocr-pages", rc)


def extract_map_observations_adapter(context, params: Mapping) -> None:
    import extract_map_observations

    rc = extract_map_observations.run_extract_map_observations(
        text_dir=params["text_dir"],
        limit=params.get("limit"),
        provider=params.get("provider", ""),
        model=params.get("model", ""),
        base_url=params.get("base_url", ""),
        api_key=params.get("api_key", ""),
        dry_run=params["dry_run"],
        jobs=params["jobs"],
        timeout=params["timeout"],
        profile=params.get("profile", ""),
        context=context,
    )
    _raise_on_failure("extract-map-observations", rc)


def detect_redactions_adapter(context, params: Mapping) -> None:
    from .detect_redactions import RedactionsOptions, run_detect_redactions

    root = Path(params.get("root", PROJECT_ROOT))
    options = _consume_root(params, "inp", "txt", "files_from")
    options["root"] = root
    if options.get("rebuild_text"):
        options["rebuild"] = True
    run_detect_redactions(RedactionsOptions(**options), context)


def extract_entities_adapter(context, params: Mapping) -> None:
    from graph import extract_entities

    rc = extract_entities.run_extract_entities(
        text_dir=params["text_dir"],
        limit=params.get("limit"),
        provider=params.get("provider", ""),
        model=params.get("model", ""),
        base_url=params.get("base_url", ""),
        api_key=params.get("api_key", ""),
        dry_run=params["dry_run"],
        jobs=params["jobs"],
        timeout=params["timeout"],
        profile=params.get("profile", ""),
        context=context,
    )
    _raise_on_failure("extract-entities", rc)


def graph_review_adapter(context, params: Mapping) -> None:
    """Kör återkommande kvalitetskontroll av grafunderlaget."""
    from graph.review_service import run_review

    _raise_on_failure("graph-review", run_review(context=context))


def graph_sync_adapter(context, params: Mapping) -> None:
    """Förhandsvisa eller applicera en granskad grafprojektion."""
    from graph.review_service import run_sync

    _raise_on_failure("graph-sync", run_sync(context=context, **params))


def graph_review_llm_adapter(context, params: Mapping) -> None:
    """Skapa källbundna LLM-förslag för den manuella grafgranskningen."""
    from graph.review_llm import run_llm_review

    _raise_on_failure("graph-review-llm", run_llm_review(context=context, **params))
