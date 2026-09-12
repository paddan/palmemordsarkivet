"""Regressionstester för Python-entrypoints och deras shell-genvägar."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Hjälpmoduler i scripts/ är inte entrypoints och ska inte ha någon genväg.
NON_ENTRYPOINTS = {"__init__", "_bootstrap"}


def _entrypoint_stems() -> list[str]:
    return sorted(
        path.stem
        for path in (PROJECT_ROOT / "scripts").glob("*.py")
        if path.stem not in NON_ENTRYPOINTS
    )


def test_every_entrypoint_has_a_dumb_shell_shortcut() -> None:
    """Varje ``scripts/X.py`` ska nås som ``./X.sh`` — utan egen logik i skalet.

    Genvägen får bara vidarebefordra argumenten; flaggor, defaults och validering
    bor i operationsregistret, annars uppstår två sanningar som glider isär.
    """
    for stem in _entrypoint_stems():
        path = PROJECT_ROOT / f"{stem}.sh"
        assert path.exists(), f"{path.name} saknas (genväg till scripts/{stem}.py)"
        assert os.access(path, os.X_OK), f"{path.name} är inte körbar"
        body = path.read_text(encoding="utf-8")
        assert f"scripts/{stem}.py" in body, f"{path.name} pekar inte på scripts/{stem}.py"
        assert '"$@"' in body, f"{path.name} vidarebefordrar inte argumenten"
        assert body.count("exec ") == 1, f"{path.name} ska avsluta med ett enda exec"
        assert "case " not in body and "--help" not in body, (
            f"{path.name} har egen flaggparsning — genvägarna ska vara dumma"
        )


def test_no_shell_shortcut_without_an_entrypoint() -> None:
    for path in PROJECT_ROOT.glob("*.sh"):
        assert (PROJECT_ROOT / "scripts" / f"{path.stem}.py").exists(), (
            f"{path.name} saknar scripts/{path.stem}.py"
        )


def _mentioned_operation_ids(path: Path) -> set[str]:
    """Strängliteraler i en entrypoint, utan docstrings.

    Varje operation ett ``scripts/X.py`` kan köra står som en literal i filen
    (``run("ingest")`` eller en tabell över positionsargument).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        doc
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        if (doc := ast.get_docstring(node, clean=False))
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    }


def test_every_operation_is_reachable_from_a_shell_shortcut() -> None:
    """Registry ↔ entrypoint ↔ genväg ska vara samma mängd.

    Likheten fångar båda riktningarna: en ny operation som bara finns i Admin
    (ingen ``scripts/X.py``, alltså ingen ``./X.sh``) och ett entrypoint som kör
    ett operation-id som inte finns. Admin, CLI och shell kan därför inte glida
    isär utan att testet faller.
    """
    from operations.registry import get_registry

    registered = {definition.id for definition in get_registry().all_operations()}
    mentioned: set[str] = set()
    for path in sorted((PROJECT_ROOT / "scripts").glob("*.py")):
        mentioned |= _mentioned_operation_ids(path) & registered

    assert mentioned == registered, (
        f"operationer utan entrypoint/genväg: {sorted(registered - mentioned)}; "
        f"entrypoint mot okänt id: {sorted(mentioned - registered)}"
    )


def test_web_shell_shortcut_forwards_to_python_entrypoint() -> None:
    path = PROJECT_ROOT / "web.sh"

    result = subprocess.run(
        [str(path), "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "Användning: scripts/web.py [-- streamlit-flaggor...]\n"


def test_representative_entrypoints_answer_help() -> None:
    for name in ("run_pipeline.py", "ocr.py", "ingest.py", "quality.py", "download.py"):
        path = PROJECT_ROOT / "scripts" / name
        result = subprocess.run(
            [sys.executable, str(path), "--help"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, f"{name} --help: {result.stderr}"


# ---------------------------------------------------------------------------
# Flaggeparitet: varje legacy-flagga ska finnas i registryns parametrar.
# ---------------------------------------------------------------------------

EXPECTED_OPERATION_FLAGS = {
    "ocr": (
        "--skip-redo", "--fallback-failed", "--redo", "--mode", "--source",
        "--no-update-pdf", "--in", "--ocr", "--txt", "--pages-out",
        "--jobs", "--per-file-jobs", "--threshold", "--from-list",
        "--retry-failed",
    ),
    "ocr-tesseract": (
        "--in", "--ocr", "--txt", "--tessdata", "--user-words",
        "--user-words-auto", "--tess-config", "--psm", "--langs",
        "--jobs", "--per-file-jobs", "--min-text-chars", "--image-dpi",
        "--errors-log", "--files-from", "--retry-failed", "--retry-blacklist",
    ),
    "ingest": (
        "--rebuild", "--limit", "--text-dir", "--db-dir", "--chunk-chars",
        "--chunk-overlap", "--model", "--unusable-list", "--reindex-since",
    ),
}


def test_operation_flag_parity_with_legacy_scripts() -> None:
    from operations.registry import get_registry

    registry = get_registry()
    for operation_id, expected in EXPECTED_OPERATION_FLAGS.items():
        flags = {
            flag
            for parameter in registry.get(operation_id).parameters
            for flag in parameter.flags
        }
        missing = [flag for flag in expected if flag not in flags]
        assert not missing, f"{operation_id} saknar flaggor i registryn: {missing}"


def test_legacy_migration_files_are_removed() -> None:
    project_root = Path(__file__).resolve().parents[1]
    for path in (
        project_root / "src" / "migrate_to_db.py",
        project_root / "migrate_to_db.sh",
        project_root / "cleanup_legacy_state.sh",
    ):
        assert not path.exists(), f"{path} ska vara borttagen"


def _run_setup_tessdata(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "setup_tessdata.py"), *args],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_tessdata_unknown_flag_exits_2() -> None:
    result = _run_setup_tessdata("--okänd-flagga")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def test_setup_tessdata_root_without_value_is_clean_error() -> None:
    # Regression: tidigare IndexError-traceback när --root saknade värde.
    result = _run_setup_tessdata("--root")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--root" in result.stderr


def test_setup_tessdata_dest_without_value_is_clean_error() -> None:
    result = _run_setup_tessdata("--dest")
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "--dest" in result.stderr


def test_setup_tessdata_help_exits_0() -> None:
    result = _run_setup_tessdata("--help")
    assert result.returncode == 0
    assert "--root" in result.stdout


def test_graph_page_handles_missing_link_analysis_import() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "3_Graf.py").read_text(encoding="utf-8")
    assert "except ImportError" in text
    assert "st-link-analysis" in text


def test_graph_page_accepts_casebook_centers_query_param() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "3_Graf.py").read_text(encoding="utf-8")
    assert "decode_graph_centers_param" in text
    assert 'st.query_params.get("centers")' in text
    assert "linked_centers" in text


def test_compare_page_installs_shared_pdf_opener_for_citation_links() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "6_Jämförelse.py").read_text(encoding="utf-8")
    assert "_casebook_ui.render_pdf_opener(ROOT)" in text


def test_utredning_installs_shared_pdf_opener_for_citation_links() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")
    assert "_casebook_ui.render_pdf_opener(ROOT)" in text
    assert "urlsafe_b64decode" not in text


def test_utredning_surfaces_provider_truncation_in_every_answer_path() -> None:
    """Regression: leverantören kan avsluta mitt i en mening (DeepSeek
    ``length``/``insufficient_system_resource``, Claude ``max_tokens`` eller
    ``max_turns``). Den halva meningen visades förut som ett färdigt svar —
    ingen av svarsvägarna fick glömma bort att kontrollera slutorsaken."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    for component in ("ask.claude", "ask.openai-mcp", "ask.openai"):
        assert f'"{component}"' in text, f"{component} saknar avklippt-kontroll"
    # Den gamla ensidiga kontrollen (bara "length", bara i RAG-strömmen) ska
    # vara borta — alla vägar går via stop_notice.
    assert 'finish_reason == "length"' not in text
    assert text.count("_truncated_notice(") >= 4


def test_casebook_page_installs_shared_pdf_opener_for_saved_answers() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "casebook_ui.py").read_text(encoding="utf-8")
    page_block = text.split("def render_casebook_page", 1)[1].split(
        "def _render_saved_answers", 1
    )[0]
    assert "render_pdf_opener(root)" in page_block


def test_utredning_reports_token_usage_for_every_llm_path() -> None:
    """Räknaren ska fyllas oavsett väg: RAG/MCP × Claude/OpenAI."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    # 1 definition + fyra anrop (stream_mcp, stream_claude, stream_openai,
    # stream_openai_mcp).
    assert text.count("_record_usage(") == 5
    assert 'stream_options={"include_usage": True}' in text
    assert "_llm_usage.usage_from_openai(response.usage)" in text
    assert "message.total_cost_usd" in text
    # Även ett anrop utan rapporterad usage ska bokföras, annars visar räknaren
    # "0 anrop" mot en endpoint som inte skickar usage.
    assert "if usage is None and cost is None" not in text
    assert "if cost is None and usage:" in text
    assert "_render_usage_panel(cfg)" in text
    assert "Totalt:" in text
    assert "Session:" in text
    assert "palme-usage" in text

    # Panelen ligger längst ner i sidofältet (efter kunskapsgrafens toggle) och
    # skrivs om både vid rendering och när ett anrop bokförts — annars visar
    # sidofältet förra anropets siffror.
    sidebar = text.split("with st.sidebar:", 1)[1].split("_render_usage_panel(backend)", 1)[0]
    assert "_usage_slot = st.empty()" in sidebar
    assert sidebar.index("_usage_slot = st.empty()") > sidebar.index('key="show_graph"')
    assert text.count("_render_usage_panel(") == 3  # def + sidofältet + efter anrop
    # Två "$" i samma markdown-block blir LaTeX-matte hos Streamlit; panelen
    # ritas som HTML och använder därför en HTML-entitet.
    assert '.replace("$", "&#36;")' in text


def test_every_page_uses_the_shared_compact_header() -> None:
    """Sidhuvudet var en h1:a + två captions per sida, och hjälp-? trycktes till
    högerkanten av Streamlits flex-wrapper runt etiketten (flex:1). CSS:en och
    rubriken delas nu av alla sidor via casebook_ui."""
    project_root = Path(__file__).resolve().parents[1]
    src = project_root / "src"
    shared = (src / "casebook_ui.py").read_text(encoding="utf-8")

    assert "class='palme-header'" in shared
    assert "padding-top: 1.5rem !important" in shared
    assert (
        '[data-testid="stWidgetLabel"] > div '
        "{ flex: 0 1 auto; justify-content: flex-start; }" in shared
    )

    for sida in [src / "Utredning.py", *sorted((src / "pages").glob("*.py"))]:
        text = sida.read_text(encoding="utf-8")
        assert "st.title(" not in text, f"{sida.name} har kvar st.title"
        assert "palme-header" not in text, f"{sida.name} duplicerar CSS:en"
        # 2_Utredningspärm renderas inifrån casebook_ui (render_casebook_page).
        if sida.name != "2_Utredningspärm.py":
            assert "render_page_header(" in text, f"{sida.name} saknar sidhuvud"


def test_utredning_header_shows_the_index_size() -> None:
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    assert '"Palmemordsarkivet",' in text
    assert "index: {table.count_rows():,} chunks" in text


def test_utredning_puts_rag_and_mcp_in_their_own_tabs() -> None:
    """Lägena är flikar (som på adminsidan), inte en sidofälts-toggle."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    assert (
        '_tab_rag, _tab_mcp = st.tabs(["Fråga arkivet (RAG)", "Utredningsläge (MCP)"])'
        in text
    )
    assert "    _render_rag_tab()" in text
    assert "    _render_mcp_tab()" in text
    # Toggeln och dess synk mot RAG-kontrollerna ska vara borta.
    assert "mcp_mode" not in text
    assert "_on_mcp_change" not in text


def test_utredning_rag_controls_live_inside_the_rag_tab() -> None:
    """Sökinställningarna hör till RAG-fliken — de ska inte synas i chatten."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    rag_block = text.split("def _render_rag_tab()", 1)[1]
    assert 'st.expander("Sökinställningar", expanded=False)' in rag_block
    for label in (
        "Hämta top-K kandidater",
        "Skicka top-N till AI",
        "Sökfilter",
        "Begränsa till entiteter",
        "OCR-tolerant fuzzy-sökning",
    ):
        assert label in rag_block, f"{label} ligger utanför RAG-fliken"

    sidebar = text.split("with st.sidebar:", 1)[1].split("def _render_mcp_tab()", 1)[0]
    assert "Visa kunskapsgraf" in sidebar
    assert "Hämta top-K kandidater" not in sidebar


def test_utredning_raises_instead_of_stopping_the_whole_page_in_a_tab() -> None:
    """``st.stop()`` i en flik hade tömt även den andra fliken (båda körs)."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")
    assert "st.stop()" not in text.split("def _render_rag_tab()", 1)[1]


# ---------------------------------------------------------------------------
# ocr_db_helper.py — text_mtime-stämpling
# ---------------------------------------------------------------------------

def _run_helper(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    import os
    import sys
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["STATE_DB"] = str(db_path)
    return subprocess.run(
        [sys.executable, str(project_root / "src" / "ocr_db_helper.py"), *args],
        capture_output=True, text=True, env=env,
    )


def _db_row(db_path: Path, stem: str):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import db
    conn = db.connect(db_path)
    return conn.execute(
        "SELECT tesseract_done_at, text_mtime FROM pdf_files WHERE pdf_stem=?",
        (stem,),
    ).fetchone()


def test_mark_done_with_txt_path_stamps_text_mtime(tmp_path: Path) -> None:
    """mark-done med txt-sökväg ska stämpla text_mtime så att
    normalize/quality-deltat ser filen."""
    txt = tmp_path / "doc.txt"
    txt.write_text("ocr-text", encoding="utf-8")
    db_path = tmp_path / "state.db"
    r = _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf"), str(txt)], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["tesseract_done_at"] is not None
    assert row["text_mtime"] == txt.stat().st_mtime


def test_mark_done_without_txt_path_still_works(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    r = _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf")], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["tesseract_done_at"] is not None
    assert row["text_mtime"] is None


def test_touch_mtime_command(tmp_path: Path) -> None:
    """touch-mtime ska uppdatera text_mtime för befintlig rad (används av
    ocr.sh --redo --mode files efter om-OCR)."""
    txt = tmp_path / "doc.txt"
    txt.write_text("ny text", encoding="utf-8")
    db_path = tmp_path / "state.db"
    _run_helper(["mark-done", "doc", str(tmp_path / "doc.pdf")], db_path)
    r = _run_helper(["touch-mtime", "doc", str(txt)], db_path)
    assert r.returncode == 0, r.stderr
    row = _db_row(db_path, "doc")
    assert row["text_mtime"] == txt.stat().st_mtime


def test_answer_graph_reuses_cached_centers_only_for_the_same_answer() -> None:
    """Regression: med stängd graf-toggle återanvändes föregående svars
    entiteter och sparades i utredningspärmen kopplade till fel fråga."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "Utredning.py").read_text(encoding="utf-8")

    block = text.split("def _render_answer_graph", 1)[1].split("if not st.toggle(", 1)[0]
    assert "cache_identity = (answer, _profile_runtime_key)" in block, (
        "cache-identiteten måste beräknas innan graf-toggeln läses"
    )
    toggle_block = text.split("if not st.toggle(", 1)[1].split("cache_identity = (answer", 1)[0]
    assert "answer_key" in toggle_block, (
        "den cachade grenen måste kontrollera att cachen hör till aktuellt svar"
    )


def test_compare_page_keeps_result_in_session_state() -> None:
    """Regression: jämförelseresultatet låg bara i lokala variabler och
    raderades av varje rerun (bokmärke, PDF-knapp, sidofältsändring)."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "6_Jämförelse.py").read_text(encoding="utf-8")

    assert 'st.session_state["compare_result"]' in text
    render_block = text.split("result = st.session_state.get", 1)[1]
    assert 'st.markdown(result["answer"]' in render_block
    assert "render_source_cards" in render_block


def test_admin_page_has_a_log_tab_that_owns_all_logging() -> None:
    """All loggning ska samlas i Logg-fliken, inte renderas löst på sidan."""
    project_root = Path(__file__).resolve().parents[1]
    text = (project_root / "src" / "pages" / "8_Admin.py").read_text(encoding="utf-8")

    assert '"Logg"' in text.split("tab_names = ", 1)[1].split("\n", 1)[0]
    assert 'render_log_tab(recent_jobs)' in text
    assert "read_log_tail" not in text
    assert "st.code(" not in text
