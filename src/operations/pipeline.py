"""Migrerad logik från ``run_pipeline.sh``.

Full pipeline: download → (wpu) → OCR → (LLM-korrigering + quality) → ingest.
OCR och ingest körs alltid, även när download inte hittar nya PDF:er.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .context import OperationContext, ensure_terminal_context
from .exceptions import OperationFailed
from .ocr import OcrOptions, run_ocr

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class PipelineOptions:
    root: Path = ROOT
    inp: Path = ROOT / "downloaded" / "files"
    txt: Path = ROOT / "generated" / "text"
    skip_wpu: bool = False
    skip_redo: bool = False
    with_llm: bool = False
    profile: str = ""
    jobs: int = 4
    test_limit: int = 0

    def __post_init__(self) -> None:
        # Låt --root (eller adminens base-path) om-rotar defaultkatalogerna,
        # precis som de andra operationernas options-dataclasses gör.
        for name in ("inp", "txt"):
            value = getattr(self, name)
            if value is not None and value.is_relative_to(ROOT):
                setattr(self, name, self.root / value.relative_to(ROOT))


@dataclass
class PipelineDependencies:
    download: Callable[..., int] | None = None
    download_wpu: Callable[..., int] | None = None
    ocr: Callable[..., int] | None = None
    llm_correct: Callable[..., int] | None = None
    quality: Callable[..., int] | None = None
    ingest: Callable[..., int] | None = None
    count_pdfs: Callable[[Path], int] | None = None


def run_pipeline(
    options: PipelineOptions,
    context: OperationContext | None = None,
    *,
    deps: PipelineDependencies | None = None,
) -> int:
    """Kör hela pipelinen. Returnerar exitkod."""
    ctx = ensure_terminal_context(context)
    deps = deps or PipelineDependencies()

    if options.with_llm and options.profile:
        import config as llm_config

        try:
            llm_config.load_profile(options.profile)
        except ValueError as exc:
            raise OperationFailed(str(exc)) from exc

    import download
    import download_wpu
    import llm_correct
    import quality
    from rag import ingest

    run_download = deps.download or (lambda **kw: download.run_download(**kw))
    run_download_wpu = deps.download_wpu or (lambda **kw: download_wpu.run_download_wpu(**kw))
    run_ocr_fn = deps.ocr or (lambda **kw: run_ocr(OcrOptions(**kw), ctx))
    run_llm = deps.llm_correct or (lambda **kw: llm_correct.run_llm_correct(**kw))
    run_quality = deps.quality or (lambda **kw: quality.run_quality(**kw))
    run_ingest = deps.ingest or (lambda **kw: ingest.run_ingest(**kw))

    def _require(step: str, rc: int | None) -> None:
        """Kasta OperationFailed med stegnamn om steget returnerade en felkod."""
        if rc:
            raise OperationFailed(f"Steget {step} misslyckades (exitkod {rc})")

    limit = options.test_limit or 0
    download_rc = run_download(
        out=options.inp, sheet_id=download.SHEET_ID, limit=limit, context=ctx
    )
    if download_rc == 2:
        ctx.log(
            "Några PDF:er kunde inte laddas ned; fortsätter med väntande OCR och ingest.",
            level="warning",
        )
    else:
        _require("download", download_rc)

    if not options.skip_wpu:
        _require("download-wpu", run_download_wpu(
            out=options.root / "downloaded" / "wpu_files", dry_run=False,
            limit=limit or None, rebuild=False, context=ctx,
        ))

    _require("ocr", run_ocr_fn(root=options.root, inp=options.inp, txt=options.txt,
                               skip_redo=options.skip_redo, jobs=options.jobs))

    if options.with_llm:
        _require("llm-correct", run_llm(
            threshold=50.0, provider="", model="", base_url="", api_key="",
            txt=options.txt, root=options.root, jobs=options.jobs,
            dry_run=False, test=None, profile=options.profile, context=ctx,
        ))
        _require("quality", run_quality(top=None, limit=None, per_page=True, text_dir=options.txt,
                                        files_dir=options.inp, rebuild=False, files_from=None, context=ctx))

    _require("ingest", run_ingest(
        rebuild=False, limit=None, text_dir=options.txt,
        db_dir=options.root / "generated" / "lancedb", chunk_chars=800,
        chunk_overlap=150, model_name="", unusable_list=options.root / "generated" / "unusable.txt",
        reindex_since=None, context=ctx,
    ))

    ctx.log("Pipeline klar.")
    return 0
