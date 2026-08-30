"""Registry för projektets gemensamma operationsdefinitioner."""

from __future__ import annotations

import os
from pathlib import Path

from .models import OperationDefinition, ParameterDefinition, ParameterKind


class OperationRegistry:
    """Samlar operationer som kan användas från CLI och administration."""

    def __init__(self) -> None:
        self._definitions: dict[str, OperationDefinition] = {}

    def register(self, definition: OperationDefinition) -> OperationDefinition:
        """Registrera en operation och avvisa dubbla identiteter."""
        if definition.id in self._definitions:
            raise ValueError(f"Operationen {definition.id!r} är redan registrerad")
        self._definitions[definition.id] = definition
        return definition

    def get(self, operation_id: str) -> OperationDefinition:
        """Hämta en operation eller ge ett svenskt felmeddelande."""
        try:
            return self._definitions[operation_id]
        except KeyError as exc:
            raise KeyError(f"Okänd operation: {operation_id}") from exc

    def admin_operations(self) -> tuple[OperationDefinition, ...]:
        """Returnera administrationssynliga operationer i visningsordning."""
        return tuple(
            sorted(
                (definition for definition in self._definitions.values() if definition.admin_visible),
                key=lambda definition: (definition.group, definition.label),
            )
        )


_registry = OperationRegistry()

ROOT = Path(__file__).resolve().parents[2]


def _p(name: str, flags: str, kind: ParameterKind, default, help_: str, **kw) -> ParameterDefinition:
    return ParameterDefinition(name, (flags,), kind, default, help_, **kw)


def _register_builtin_operations() -> None:
    from . import adapters
    from . import neo4j as neo4j_mod
    from .ocr import OcrOptions, run_ocr
    from .pipeline import PipelineOptions, run_pipeline
    from .tesseract import TesseractOptions, run_tesseract

    def validate_merge_pages(params) -> None:
        if bool(params.get("stem")) == bool(params.get("all")):
            raise ValueError("Ange exakt en av --stem och --all")

    _registry.register(OperationDefinition(
        id="run-pipeline", label="Full pipeline", group="Pipeline",
        description="Hela pipelinen: download → OCR → (LLM) → ingest.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("skip_wpu", "--skip-wpu", "bool", False, "Hoppa över wpu.nu-nedladdning"),
            _p("skip_redo", "--skip-redo", "bool", False, "Hoppa över Surya-steget"),
            _p("with_llm", "--with-llm", "bool", False, "Kör LLM-korrigering"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella processer"),
            _p("test_limit", "--test", "int", 0, "Testläge: N filer"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=lambda ctx, params: run_pipeline(PipelineOptions(**params), ctx),
    ))

    _registry.register(OperationDefinition(
        id="download", label="Ladda ned palme-PDF:er", group="Pipeline",
        description="Ladda ned PDF:er från palmemordsarkivet.se.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("out", "--out", "path", ROOT / "downloaded" / "files", "Målmapp"),
            _p("sheet_id", "--sheet-id", "str", "", "Google Sheets-ID"),
            _p("limit", "--limit", "int", 0, "Begränsa till N filer"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.download_adapter,
    ))

    _registry.register(OperationDefinition(
        id="download-wpu", label="Ladda ned wpu-PDF:er", group="Pipeline",
        description="Ladda ned PDF:er från wpu.nu.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("out", "--out", "path", ROOT / "downloaded" / "wpu_files", "Målmapp"),
            _p("dry_run", "--dry-run", "bool", False, "Lista utan att ladda ned"),
            _p("limit", "--limit", "int", 0, "Begränsa till N filer"),
            _p("rebuild", "--rebuild", "bool", False, "Ladda ned igen"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.download_wpu_adapter,
    ))

    _registry.register(OperationDefinition(
        id="ocr", label="Full OCR", group="OCR",
        description="Tesseract → Surya-fallback → merge → redactions → normalize → quality → redo.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("skip_redo", "--skip-redo", "bool", False, "Hoppa över Surya-redo"),
            _p("fallback_only", "--fallback-failed", "bool", False, "Bara Surya-fallback"),
            _p("redo_only", "--redo", "bool", False, "Hoppa direkt till redo-logiken"),
            _p("mode", "--mode", "choice", "pages", "Redo-läge", choices=("pages", "files")),
            _p("source", "--source", "choice", "any", "Källtyp för redo", choices=("text-layer", "ocr", "any")),
            _p("no_update_pdf", "--no-update-pdf", "bool", False, "Hoppa över PDF-textlager-patch efter Surya"),
            _p("inp", "--in", "path", ROOT / "downloaded" / "files", "Ingångskatalog med PDF:er"),
            _p("ocr", "--ocr", "path", ROOT / "generated" / "ocr", "Output-katalog för OCR-PDF:er"),
            _p("txt", "--txt", "path", ROOT / "generated" / "text", "Output-katalog för .txt"),
            _p("pages_out", "--pages-out", "path", ROOT / "generated" / "text_pages", "Per-sida-output"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella processer"),
            _p("per_file_jobs", "--per-file-jobs", "int", int(os.environ.get("PER_FILE_JOBS", "2")), "OCR-trådar per fil"),
            _p("threshold", "--threshold", "float", 50.0, "Score-tröskel"),
            _p("files_from", "--from-list", "path", None, "Filstam-lista"),
            _p("retry_failed", "--retry-failed", "bool", False, "Kör om misslyckade"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=lambda ctx, params: run_ocr(OcrOptions(**params), ctx),
    ))

    _registry.register(OperationDefinition(
        id="ocr-tesseract", label="Tesseract-OCR", group="OCR",
        description="Tesseract-OCR med retries och state-spårning.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("inp", "--in", "path", ROOT / "downloaded" / "files", "Ingångskatalog"),
            _p("ocr", "--ocr", "path", ROOT / "generated" / "ocr", "Output-katalog för OCR-PDF:er"),
            _p("txt", "--txt", "path", ROOT / "generated" / "text", "Output-katalog för .txt"),
            _p("tessdata", "--tessdata", "path", ROOT / "tessdata", "Tessdata-katalog"),
            _p("user_words", "--user-words", "path", ROOT / "tessdata" / "swe.user-words", "swe.user-words"),
            _p("user_words_auto", "--user-words-auto", "path", ROOT / "tessdata" / "swe.user-words.auto", "swe.user-words.auto"),
            _p("tess_config", "--tess-config", "path", ROOT / "tessdata" / "tesseract.config", "tesseract.config"),
            _p("psm", "--psm", "int", 6, "Page segmentation mode"),
            _p("langs", "--langs", "str", "swe", "Tesseract-språk"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella filer"),
            _p("per_file_jobs", "--per-file-jobs", "int", int(os.environ.get("PER_FILE_JOBS", "2")), "OCR-trådar per fil"),
            _p("min_text_chars", "--min-text-chars", "int", 200, "Tröskel för \"har redan text\""),
            _p("image_dpi", "--image-dpi", "int", 300, "Bild-DPI för OCR"),
            _p("errors_log", "--errors-log", "path", ROOT / "generated" / "errors.log", "Fellogg"),
            _p("files_from", "--files-from", "path", None, "Filstam-lista"),
            _p("retry_failed", "--retry-failed", "bool", False, "Kör om misslyckade"),
            _p("retry_blacklist", "--retry-blacklist", "bool", False, "Återaktivera blacklistade"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=lambda ctx, params: run_tesseract(TesseractOptions(**params), ctx),
    ))

    _registry.register(OperationDefinition(
        id="detect-redactions", label="Maskeringsdetektering", group="OCR",
        description="Detektera maskeringsblock och infoga [MASKAD].",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("inp", "--in", "path", ROOT / "downloaded" / "files", "PDF-katalog"),
            _p("txt", "--txt", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella processer"),
            _p("dpi", "--dpi", "int", 72, "Render-DPI"),
            _p("rebuild", "--rebuild", "bool", False, "Kör om alla"),
            _p("rebuild_text", "--rebuild-text", "bool", False, "Regenerera text"),
            _p("files_from", "--files-from", "path", None, "Filstam-lista"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.detect_redactions_adapter,
    ))

    _registry.register(OperationDefinition(
        id="merge-pages", label="Slå ihop per-sida-text", group="OCR",
        description="Slå ihop pdf_pages-sidor in i text/<stem>.txt.",
        parameters=(
            _p("stem", "--stem", "str", None, "Enskild filstam"),
            _p("all", "--all", "bool", False, "Alla stems med sidor"),
            _p("txt_dir", "--txt-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.merge_pages_adapter,
        validate=validate_merge_pages,
    ))

    _registry.register(OperationDefinition(
        id="merge-wpu", label="Jämför wpu mot palme", group="OCR",
        description="Jämför wpu och palme; raderar förloraren.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("dry_run", "--dry-run", "bool", False, "Visa utan att radera"),
            _p("rebuild", "--rebuild", "bool", False, "Ignorera wpu_decisions"),
            _p("margin", "--margin", "float", 5.0, "Poängfördel"),
            _p("wpu_dir", "--files-wpu", "path", ROOT / "downloaded" / "wpu_files", "wpu-PDF-katalog"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("ocr_dir", "--ocr-dir", "path", ROOT / "generated" / "ocr", "OCR-katalog"),
            _p("jobs", "--jobs", "int", max(1, os.cpu_count() or 4), "Parallella processer"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.merge_wpu_adapter,
    ))

    _registry.register(OperationDefinition(
        id="build-user-words", label="Användarordlista", group="Kvalitet",
        description="Bygg Tesseract user-words från OCR-text.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("out", "--out", "path", ROOT / "tessdata" / "swe.user-words.auto", "Output-fil"),
            _p("user_words", "--user-words", "path", ROOT / "tessdata" / "swe.user-words", "Befintliga ord"),
            _p("min_freq", "--min-freq", "int", 0, "Minsta frekvens"),
            _p("rebuild", "--rebuild", "bool", False, "Ignorera cache"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.build_user_words_adapter,
    ))

    _registry.register(OperationDefinition(
        id="llm-correct", label="LLM-korrigering", group="Kvalitet",
        description="LLM-korrigera dåliga OCR-sidor.",
        parameters=(
            _p("profile", "--profile", "str", "", "LLM-konfiguration att använda"),
            _p("threshold", "--threshold", "float", 50.0, "Score-tröskel"),
            _p("provider", "--provider", "str", "", "LLM-provider"),
            _p("model", "--model", "str", "", "Modellnamn"),
            _p("base_url", "--base-url", "str", "", "API-URL"),
            _p("api_key", "--api-key", "str", "", "API-nyckel", secret=True),
            _p("txt", "--txt", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella anrop"),
            _p("dry_run", "--dry-run", "bool", False, "Visa utan att skriva"),
            _p("test", "--test", "str", None, "Korrigera en enskild fil"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.llm_correct_adapter,
    ))

    _registry.register(OperationDefinition(
        id="ocr-pages", label="Sid-OCR", group="OCR",
        description="OCR per sida (Tesseract/Surya/vision).",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("in", "--in", "path", None, "PDF-fil", required=True),
            _p("out_dir", "--out-dir", "path", None, "Output-katalog"),
            _p("engine", "--engine", "choice", "tesseract", "Motor", choices=("tesseract", "vision", "surya", "detect-only")),
            _p("langs", "--langs", "str", "swe", "Tesseract-språk"),
            _p("dpi", "--dpi", "int", 300, "Render-DPI"),
            _p("pages", "--pages", "str", None, "Kommaseparerade sidnummer"),
            _p("ocr_dir", "--ocr-dir", "path", None, "OCR-PDF-katalog"),
            _p("no_update_pdf", "--no-update-pdf", "bool", False, "Stäng av PDF-patch"),
            _p("no_detect_redactions", "--no-detect-redactions", "bool", False, "Stäng av maskeringsdetektering"),
            _p("txt_dir", "--txt-dir", "path", None, "Text-katalog"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.ocr_pages_adapter,
    ))

    _registry.register(OperationDefinition(
        id="load-graph", label="Ladda grafdata", group="Extraktion och graf",
        description="Ladda doc_entities till Neo4j.",
        parameters=(
            _p("uri", "--uri", "str", "bolt://localhost:7687", "Neo4j-URI"),
            _p("user", "--user", "str", "neo4j", "Användare"),
            _p("batch", "--batch", "int", 1000, "Sidor per transaktion"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.load_graph_adapter,
    ))

    _registry.register(OperationDefinition(
        id="quality", label="Kvalitetsbedömning", group="Kvalitet",
        description="Bedöm OCR-kvalitet och skriv till quality-tabellerna.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("top", "--top", "int", None, "Visa värsta N"),
            _p("limit", "--limit", "int", None, "Bara N första filerna"),
            _p("per_page", "--per-page", "bool", False, "Bedöm per sida"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("files_dir", "--files-dir", "path", ROOT / "downloaded" / "files", "PDF-katalog"),
            _p("rebuild", "--rebuild", "bool", False, "Kör om alla"),
            _p("files_from", "--files-from", "path", None, "Filstam-lista"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.quality_adapter,
    ))

    _registry.register(OperationDefinition(
        id="normalize", label="Normalisering", group="Kvalitet",
        description="Regelbaserad textnormalisering.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("txt", "--txt", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("dry_run", "--dry-run", "bool", False, "Visa utan att skriva"),
            _p("stats", "--stats", "bool", False, "Visa per-fil-statistik"),
            _p("rebuild", "--rebuild", "bool", False, "Ignorera delta"),
            _p("files_from", "--files-from", "path", None, "Filstam-lista"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.normalize_adapter,
    ))

    _registry.register(OperationDefinition(
        id="ingest", label="LanceDB-ingest", group="Index",
        description="Indexera text till LanceDB.",
        parameters=(
            _p("root", "--root", "path", ROOT, "Projektrot"),
            _p("rebuild", "--rebuild", "bool", False, "Börja om från noll"),
            _p("limit", "--limit", "int", None, "Max antal filer"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("db_dir", "--db-dir", "path", ROOT / "generated" / "lancedb", "LanceDB-katalog"),
            _p("chunk_chars", "--chunk-chars", "int", 800, "Chunk-storlek"),
            _p("chunk_overlap", "--chunk-overlap", "int", 150, "Chunk-överlapp"),
            # Motsvarar rag.ingest.MODEL_NAME — hårdkodad här för att slippa den
            # tunga lancedb/sentence-transformers-importen vid --help.
            _p("model", "--model", "str", "intfloat/multilingual-e5-large", "Embedding-modell"),
            _p("unusable_list", "--unusable-list", "path", ROOT / "generated" / "unusable.txt", "Unusable-lista"),
            _p("reindex_since", "--reindex-since", "str", None,
               "Tvinga re-index av filer modifierade efter denna tid (ISO 8601 eller unix-sekunder)"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.ingest_adapter,
    ))

    _registry.register(OperationDefinition(
        id="extract-map-observations", label="Kartobservationsförslag", group="Extraktion och graf",
        description="LLM-extraktion av person-position-tid-kandidater.",
        parameters=(
            _p("profile", "--profile", "str", "", "LLM-konfiguration att använda"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("limit", "--limit", "int", None, "Max antal dokument"),
            _p("provider", "--provider", "str", "", "LLM-provider"),
            _p("model", "--model", "str", "", "Modellnamn"),
            _p("base_url", "--base-url", "str", "", "API-URL"),
            _p("api_key", "--api-key", "str", "", "API-nyckel", secret=True),
            _p("dry_run", "--dry-run", "bool", False, "Räkna utan LLM-anrop"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella sidor"),
            _p("timeout", "--timeout", "float", 120.0, "Timeout per anrop"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.extract_map_observations_adapter,
    ))

    _registry.register(OperationDefinition(
        id="extract-entities", label="Entitetsextraktion", group="Extraktion och graf",
        description="LLM-extraktion av entiteter/relationer per sida.",
        parameters=(
            _p("profile", "--profile", "str", "", "LLM-konfiguration att använda"),
            _p("text_dir", "--text-dir", "path", ROOT / "generated" / "text", "Text-katalog"),
            _p("limit", "--limit", "int", None, "Max antal dokument"),
            _p("provider", "--provider", "str", "", "LLM-provider"),
            _p("model", "--model", "str", "", "Modellnamn"),
            _p("base_url", "--base-url", "str", "", "API-URL"),
            _p("api_key", "--api-key", "str", "", "API-nyckel", secret=True),
            _p("dry_run", "--dry-run", "bool", False, "Räkna utan LLM-anrop"),
            _p("jobs", "--jobs", "int", int(os.environ.get("JOBS", "4")), "Parallella sidor"),
            _p("timeout", "--timeout", "float", 120.0, "Timeout per anrop"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.extract_entities_adapter,
    ))

    _registry.register(OperationDefinition(
        id="llm-config", label="LLM-inställningar", group="LLM-inställningar",
        description="Visa/ändra provider, modell och base URL.",
        parameters=(
            _p("provider", "--provider", "choice", None, "claude eller openai", choices=("claude", "openai")),
            _p("model", "--model", "str", None, "Modellnamn"),
            _p("base_url", "--base-url", "str", None, "API-URL"),
            _p("reset", "--reset", "bool", False, "Återställ till defaults"),
        ),
        admin_visible=True, mutating=True, confirmation=None,
        run=adapters.llm_config_adapter,
    ))

    _registry.register(OperationDefinition(
        id="neo4j-status", label="Neo4j-status", group="Extraktion och graf",
        description="Visa Neo4j-containerstatus.",
        parameters=(), admin_visible=True, mutating=False, confirmation=None,
        run=lambda ctx, params: neo4j_mod.neo4j_status(ctx),
    ))
    _registry.register(OperationDefinition(
        id="neo4j-start", label="Neo4j-start", group="Extraktion och graf",
        description="Starta lokal Neo4j-container.",
        parameters=(), admin_visible=True, mutating=True, confirmation=None,
        run=lambda ctx, params: neo4j_mod.neo4j_start(ctx),
    ))
    _registry.register(OperationDefinition(
        id="neo4j-stop", label="Neo4j-stop", group="Extraktion och graf",
        description="Stoppa lokal Neo4j-container.",
        parameters=(), admin_visible=True, mutating=True, confirmation=None,
        run=lambda ctx, params: neo4j_mod.neo4j_stop(ctx),
    ))


_register_builtin_operations()


def get_registry() -> OperationRegistry:
    """Returnera projektets gemensamma operationsregistry."""
    return _registry
