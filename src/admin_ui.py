"""Testbara presenterare och formulärhelpers för adminsidan.

Hålls Streamlit-fri för de rena hjälparna så att beteendet kan testas utan att
produktionsoperationer körs. De Streamlit-beroende delarna läggs i samma modul
men isoleras så att enhetstesterna bara täcker den rena logiken.
"""

from __future__ import annotations

import html
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from operations.context import debug_value_enabled
from operations.exceptions import OperationFailed
from operations.registry import OperationRegistry

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / "generated" / "admin_settings.json"

BASE_KEY = "base"
# Debugloggning för nya jobb; sätts i miljön av apply_debug_logging.
DEBUG_KEY = "debug_log"

# Etikett för choice-parametrar med default None: användaren kan lämna värdet
# oförändrat i stället för att tvingas välja ett konkret alternativ.
UNCHANGED_CHOICE_LABEL = "oförändrad"
CUSTOM_MODEL_LABEL = "Annan modell…"

# Gemensamma systemvägar (relativa till base-path) som kan sättas en gång i
# Inställningar-fliken. (nyckel, etikett, relativ standardkatalog)
SYSTEM_PATHS: list[tuple[str, str, str]] = [
    ("files", "Palme-PDF:er", "downloaded/files"),
    ("wpu_files", "WPU-PDF:er", "downloaded/wpu_files"),
    ("text", "OCR-text", "generated/text"),
    ("ocr", "OCR-PDF:er", "generated/ocr"),
    ("text_pages", "Per-sida-text", "generated/text_pages"),
    ("lancedb", "Vektorindex (LanceDB)", "generated/lancedb"),
    ("unusable", "Unusable-lista", "generated/unusable.txt"),
    ("errors_log", "Fellogg", "generated/errors.log"),
]

_JOB_STATUS_LABELS: dict[str, str] = {
    "queued": "Köad",
    "running": "Körs",
    "cancel_requested": "Avbrytning begärd",
    "succeeded": "Lyckades",
    "failed": "Misslyckades",
    "cancelled": "Avbruten",
    "interrupted": "Avbruten (omstart)",
}


def progress_fraction(job: Mapping[str, object]) -> float | None:
    """Returnera genomförda/totala enheter, eller None när totalen saknas."""
    total = job.get("total_units")
    if not total:
        return None
    completed = job.get("completed_units") or 0
    # Värdena är objekt i Mapping:en; str()-vägen bevarar float()/int-beteendet.
    return float(str(completed)) / float(str(total))


def format_job_status(status: str) -> str:
    """Returnera en svensk etikett för en jobbstatus."""
    return _JOB_STATUS_LABELS.get(status, status)


def error_log_path() -> Path:
    """Absolut sökväg till felloggen (följer base-path i Inställningar)."""
    resolved = resolve_path_default(ROOT / "generated" / "errors.log", load_settings())
    return Path(str(resolved))


def job_option_labels(jobs: Sequence[Mapping[str, object]]) -> list[str]:
    """Etiketter för loggväljaren: operation, status, kort id och starttid."""
    return [
        f"{job.get('operation')} · {format_job_status(str(job.get('status', '')))}"
        f" · {str(job.get('id', ''))[:8]} · {job.get('created_at')}"
        for job in jobs
    ]


# Jobbloggen skrivs som "<tidsstämpel> [nivå] meddelande" (se worker.JobSink).
_LOG_LEVEL_RE = re.compile(r"\[(debug|info|success|warning|error)\]", re.IGNORECASE)
LOG_LEVEL_LABELS: dict[str, str] = {
    "all": "Alla nivåer",
    "debug": "Debug",
    "info": "Info",
    "warning": "Varning",
    "error": "Fel",
}
# Antal parameterkolumner i operationsformulären. Sidan körs i full bredd, så
# ett fält per rad blir orimligt brett.
FORM_COLUMNS = 3

# Fältbredd per parameterkind. Talen i den här appen är små (antal filer,
# trösklar), men Streamlit döljer stegknapparna (-/+) för number_input under
# 7.5rem + stegknapparnas bredd (~148 px), så fälten måste vara strax över det.
FIELD_WIDTHS: dict[str, int] = {"int": 170, "float": 180}

# Rutnätets bredd i vikter per rad. Rader med smala fält får en tom spacer så
# att fälten hamnar intill varandra i stället för utspridda över hela sidan.
FORM_GRID = 12

# Rader med bara talfält får en fast pixelbredd: kolumnvikter är andelar av
# sidan och skulle sprida ut två smala fält över hela bredden. Etikettbredden
# uppskattas i px (14 px-etikett, ~7,5 px/tecken) så att titeln ryms på en rad.
LABEL_CHAR_PX = 7.5
ROW_GAP_PX = 16

# Formulärlayout per operation: rubrik (för underavsnitt i en flik), knapptext
# och etiketter. Fälten grupperas per sort (se GROUP_ORDER) och läggs i
# FORM_COLUMNS kolumner.
FORM_LAYOUT: dict[str, dict[str, Any]] = {
    "run-pipeline": {
        "button": "Kör pipeline",
        "labels": {
            "skip_wpu": "Hoppa över wpu",
            "skip_redo": "Hoppa över Surya",
            "with_llm": "Kör LLM-korrigering",
            "jobs": "Parallella processer",
            "test_limit": "Testläge: Antal filer att ladda ner",
        },
    },
    "merge-pages": {
        "labels": {"stem": "Dokument"},
    },
    "llm-correct": {
        "labels": {"test": "Textfil att rätta"},
    },
}

# Formulärets fält grupperas per sort så att likartade fält hamnar intill
# varandra i stället för att blandas i ett rutnät.
GROUP_ALTERNATIVES = "Alternativ"
GROUP_SETTINGS = "Inställningar"
GROUP_PATHS = "Sökvägar"
GROUP_ORDER = (GROUP_ALTERNATIVES, GROUP_SETTINGS, GROUP_PATHS)

# Kort hjälptext per operation: vad avsnittet gör och hur det används. Visas
# till höger i sektionskortet. Nya admin-synliga operationer ska få en rad här.
@dataclass(frozen=True)
class SectionHelp:
    """Hjälp för ett avsnitt: en kort förklaring och en punkt per val.

    ``options`` nycklas på parameternamn så att etiketten i punktlistan blir
    exakt den användaren ser i formuläret (inkl. eventuell etikettöversättning).
    """

    text: str
    options: tuple[tuple[str, str], ...] = ()


# Kort hjälptext per operation: vad avsnittet gör och vad valen betyder. Visas
# till höger i sektionskortet. Nya admin-synliga operationer ska få en rad här.
OPERATION_HELP: dict[str, SectionHelp] = {
    "run-pipeline": SectionHelp(
        "Kör hela kedjan: nedladdning → OCR → kvalitet → vektorindex. Starta och följ körningen i panelen högst upp; hela loggen finns i Logg-fliken.",
        (
            ("skip_wpu", "Hämtar inte kompletterande PDF:er från wpu.nu."),
            ("skip_redo", "Snabbare körning, men svåra sidor får sämre text."),
            ("with_llm", "Rättar sidor under score-tröskeln med vald LLM-konfiguration efter OCR:en."),
            ("profile", "Vilken LLM-konfiguration som används; skapas under Inställningar."),
            ("jobs", "Hur många filer som bearbetas samtidigt."),
            ("test_limit", "Begränsar körningen — bra för att prova flödet först."),
        ),
    ),
    "ocr": SectionHelp(
        "OCR:ar alla PDF:er — Tesseract först och Surya på sidor under tröskeln eller där Tesseract misslyckats.",
        (
            ("skip_redo", "Snabbare körning — svåra sidor behåller Tesseract-texten."),
            ("fallback_only", "Rör bara filer där Tesseract inte fick fram någon text."),
            ("redo_only", "Går förbi första OCR-varvet och kör bara Surya-steget."),
            ("no_update_pdf", "Behåller OCR-PDF:en som den är och skriver bara texten."),
            ("retry_failed", "Ger tidigare misslyckade filer ett nytt försök."),
            ("mode", "Vad redo-steget arbetar på: hela filer eller enbart sidor under tröskeln."),
            ("source", "Filer med inbäddat textlager, OCR:ade filer, eller alla."),
            ("jobs", "Hur många filer som bearbetas samtidigt."),
            ("per_file_jobs", "Antal trådar varje OCR-körning får."),
            ("threshold", "Sidor under denna poäng OCR:as om med Surya."),
        ),
    ),
    "ocr-tesseract": SectionHelp(
        "OCR:ar nya och ändrade PDF:er med Tesseract och sparar status i state.db. Kör igen när nya filer laddats ner.",
        (
            ("retry_failed", "Ger tidigare misslyckade filer ett nytt försök."),
            ("retry_blacklist", "Släpper spärren på filer som spärrats efter upprepade fel."),
            ("langs", "Språkkod för Tesseract, t.ex. swe."),
            ("psm", "Sidsegmentering (0–13); 6 passar arkivsidor med löpande text."),
            ("jobs", "Hur många filer som OCR:as samtidigt."),
            ("per_file_jobs", "Antal trådar varje OCR-körning får."),
            ("min_text_chars", "Filer med mindre text än så här räknas som textlösa och OCR:as om."),
            ("image_dpi", "Upplösning som används när en bild-OCR krävs."),
        ),
    ),
    "ocr-pages": SectionHelp(
        "OCR:ar en enskild PDF sida för sida med vald motor.",
        (
            ("engine", "tesseract är snabbast, surya bäst på svåra sidor, vision använder en bildmodell och detect-only letar bara maskeringar."),
            ("no_update_pdf", "Behåller OCR-PDF:en orörd."),
            ("no_detect_redactions", "Söker inte efter svarta maskeringsblock."),
            ("langs", "Språkkod för Tesseract, t.ex. swe."),
            ("dpi", "Upplösning när sidan renderas före OCR."),
            ("pages", "T.ex. 1,3,5-7. Tomt betyder alla sidor."),
            ("in", "Väljs ur PDF-arkivet (downloaded/files och downloaded/wpu_files)."),
        ),
    ),
    "merge-pages": SectionHelp(
        "Slår ihop per-sida-texten till en textfil per dokument.",
        (
            ("stem", "Väljs bland de dokument som har per-sida-text i state.db."),
            ("all", "Kör hela arkivet i stället för en enskild stam."),
        ),
    ),
    "merge-wpu": SectionHelp(
        "Jämför wpu- och palme-versionen av samma dokument och behåller den med högst kvalitetspoäng. Förlorarens text- och OCR-filer raderas.",
        (
            ("dry_run", "Raderar ingenting, visar bara utfallet."),
            ("rebuild", "Jämför om alla dokument, även de som redan avgjorts."),
            ("margin", "Hur många poäng bättre wpu-texten måste vara för att ersätta palme-versionen."),
            ("jobs", "Hur många dokument som jämförs samtidigt."),
        ),
    ),
    "detect-redactions": SectionHelp(
        "Letar svarta maskeringsblock i OCR-bilderna och infogar [MASKAD] i texten. Resultatet utforskas i Maskeringar-fliken.",
        (
            ("rebuild", "Tar även filer som redan kontrollerats."),
            ("rebuild_text", "Bygger om texten från OCR-PDF:erna."),
            ("jobs", "Hur många filer som analyseras samtidigt."),
            ("dpi", "Upplösning när sidorna renderas för analys (72 räcker för maskeringsblock)."),
        ),
    ),
    "quality": SectionHelp(
        "Poängsätter OCR-texten 0–100 och skriver resultatet till quality-tabellerna.",
        (
            ("per_page", "Skriver även poäng per sida — det Surya-redo och LLM-korrigeringen bygger på."),
            ("rebuild", "Bedömer om även filer som redan har poäng."),
            ("top", "Skriver de N sämsta filerna i loggen."),
            ("limit", "Begränsar körningen — bra för en snabb kontroll."),
        ),
    ),
    "llm-correct": SectionHelp(
        "Låter en LLM rätta sidor under score-tröskeln. Kör gärna en torrkörning först.",
        (
            ("profile", "Vilken LLM-konfiguration som används; skapas under Inställningar."),
            ("dry_run", "Räknar hur många sidor som skulle rättas."),
            ("threshold", "Sidor med lägre poäng än så här rättas."),
            ("test", "Väljs ur generated/text."),
            ("jobs", "Hur många sidor som rättas samtidigt."),
        ),
    ),
    "normalize": SectionHelp(
        "Städar OCR-texten regelbaserat (ligaturer, blanksteg, upprepade rader). Körs automatiskt av OCR-flödet men kan köras separat.",
        (
            ("dry_run", "Visar vad som skulle ändras utan att skriva."),
            ("stats", "Skriver statistik per fil i loggen."),
            ("rebuild", "Tar med alla filer, inte bara ändrade."),
        ),
    ),
    "build-user-words": SectionHelp(
        "Bygger Tesseracts användarordlista från OCR-texten så att namn och facktermer känns igen bättre.",
        (
            ("min_freq", "Ord som förekommer färre gånger hoppas över."),
            ("rebuild", "Bygger om hela listan från texten."),
        ),
    ),
    "ingest": SectionHelp(
        "Indexerar text i LanceDB för sökning och RAG. Bara nya och ändrade filer bearbetas.",
        (
            ("rebuild", "Bygger om hela indexet, även oförändrade filer."),
            ("limit", "Begränsar körningen (för test)."),
            ("chunk_chars", "Hur stora textutdrag som indexeras."),
            ("chunk_overlap", "Hur mycket intilliggande utdrag överlappar varandra."),
            ("model", "Måste vara samma modell som sökningen använder."),
            ("reindex_since", "Filändringar efter detta ISO-datum eller unix-tid indexeras om."),
        ),
    ),
    "extract-entities": SectionHelp(
        "Låter en LLM hitta personer, platser, organisationer och relationer per sida. Resultatet hamnar i doc_entities och granskas vidare i Graf-fliken.",
        (
            ("profile", "Vilken LLM-konfiguration som används; skapas under Inställningar."),
            ("dry_run", "Visar hur många sidor som återstår."),
            ("limit", "Hur många dokument som körs denna omgång."),
            ("jobs", "Hur många sidor som bearbetas samtidigt."),
            ("timeout", "Max tid innan sidan hoppas över."),
        ),
    ),
    "extract-map-observations": SectionHelp(
        "Föreslår person–plats–tid-kandidater ur texten. Förslagen hamnar i en granskningskö i Karta-fliken — inget visas på kartan förrän du godkänner det.",
        (
            ("profile", "Vilken LLM-konfiguration som används; skapas under Inställningar."),
            ("dry_run", "Visar hur många sidor som återstår."),
            ("limit", "Hur många dokument som körs denna omgång."),
            ("jobs", "Hur många sidor som bearbetas samtidigt."),
            ("timeout", "Max tid innan sidan hoppas över."),
        ),
    ),
    "graph-review": SectionHelp(
        "Kontrollerar det extraherade underlaget och flaggar tveksamma namn och relationer. Ändrar ingenting, bara rapporterar."
    ),
    "graph-review-llm": SectionHelp(
        "Tar fram förslag på förbättringar för flaggade poster, alltid med källbelägg. Förslagen hamnar i granskningskön och måste godkännas manuellt.",
        (
            ("profile", "Vilken LLM-konfiguration som används; skapas under Inställningar."),
            ("limit", "0 betyder alla återstående sidor."),
        ),
    ),
    "load-graph": SectionHelp(
        "Skriver det granskade underlaget till Neo4j så att grafen kan utforskas i Graf-fliken. Andra data i databasen lämnas orörda.",
        (
            ("uri", "Adressen till instansen — tom betyder lokal Neo4j."),
            ("user", "Neo4j-användare; standard är neo4j."),
            ("batch", "Antal rader per skrivning till Neo4j."),
        ),
    ),
    "neo4j-start": SectionHelp(
        "Startar den lokala Neo4j-containern via podman. Första gången skapas container och lösenord automatiskt."
    ),
    "neo4j-status": SectionHelp(
        "Visar om Neo4j-containern kör och vilken adress den svarar på."
    ),
    "neo4j-stop": SectionHelp(
        "Stoppar Neo4j-containern. Data ligger kvar till nästa start."
    ),
    "graph-sync": SectionHelp(
        "Jämför Neo4j med det granskade underlaget. Kör först utan 'Applicera uppdateringen' för att få en kontrollkod.",
        (
            ("uri", "Adressen till instansen — tom betyder lokal Neo4j."),
            ("user", "Neo4j-användare; standard är neo4j."),
            ("apply", "Utan den visas bara en förhandsvisning."),
            ("expected", "Måste anges exakt som förhandsvisningen visade."),
            ("adopt_legacy", "Engångsval för en graf importerad före ägarmärkningen."),
            ("reset_stale", "Behövs när en ny extraktion tagit bort poster."),
        ),
    ),
}

# Neo4j-operationerna har inga parametrar och visas därför som en gemensam
# sektion med en knapp per åtgärd i stället för tre separata kort.
NEO4J_BUTTONS: tuple[tuple[str, str], ...] = (
    ("neo4j-start", "Starta"),
    ("neo4j-stop", "Stopp"),
    ("neo4j-status", "Status"),
)
NEO4J_OPERATION_IDS: tuple[str, ...] = tuple(
    operation_id for operation_id, _ in NEO4J_BUTTONS
)
NEO4J_HEADING = "Neo4j"
NEO4J_HELP = SectionHelp(
    "Neo4j är den lokala grafdatabasen som Graf-fliken läser och skriver till. "
    "Starta den innan du laddar eller uppdaterar grafen.",
    (
        ("Starta", "Drar igång containern; första gången skapas den automatiskt med ett nytt lösenord."),
        ("Stopp", "Stänger av containern — data ligger kvar till nästa start."),
        ("Status", "Visar om containern kör och vilken adress den svarar på."),
    ),
)

# Filväljare: parametrar där användaren väljer en befintlig fil i stället för
# att skriva en sökväg. Nyckeln är (operation, parameter) och ``sources`` anger
# vilken katalog filen hämtas från — rätt katalog för funktionens syfte.
FILE_PICKERS: dict[tuple[str, str], dict[str, Any]] = {
    ("ocr-pages", "in"): {
        "sources": ("downloaded/files", "downloaded/wpu_files"),
        "pattern": "*.pdf",
        "empty": "— välj PDF —",
        "as": "path",
    },
    ("merge-pages", "stem"): {
        "sources": ("state:pdf_pages",),
        "pattern": "",
        "empty": "— välj dokument —",
        "as": "stem",
    },
    ("llm-correct", "test"): {
        "sources": ("generated/text",),
        "pattern": "*.txt",
        "empty": "— välj textfil —",
        "as": "path",
    },
}

# Äldre modellstyrning som de namngivna LLM-profilerna ersätter. Flaggorna
# finns kvar i CLI:t men visas inte i adminformulären när profilen styr valet.
LEGACY_LLM_PARAMS = ("provider", "model", "base_url")

# Värdet för systemloggen i källväljaren (felloggen, se errors_log.py).
SYSTEM_LOG_OPTION = "system"

# Lässfönster och visningstak: en jobblogg kan vara flera MB.
LOG_SCAN_LINES = 5000
LOG_SHOW_LINES = 300


def filter_log_lines(text: str, *, level: str = "all", query: str = "") -> list[str]:
    """Filtrera loggrader på nivå och fritext (skiftlägesokänsligt).

    Nivån läses ur ``[info]``/``[warning]``/``[error]``. Rader utan egen nivå
    (fortsättningsrader och felloggens rader) ärver föregående rads nivå, och
    felloggen räknas därför som ``error``.
    """
    needle = query.strip().casefold()
    kept: list[str] = []
    current = "error"
    for line in text.splitlines():
        match = _LOG_LEVEL_RE.search(line)
        if match:
            current = match.group(1).casefold()
            if current == "success":
                current = "info"
        if level != "all" and current != level:
            continue
        if needle and needle not in line.casefold():
            continue
        kept.append(line)
    return kept


def group_admin_operations(registry: OperationRegistry) -> dict[str, list]:
    """Gruppera administrationssynliga operationer på (group, label)-ordning."""
    grouped: dict[str, list] = {}
    for definition in registry.admin_operations():
        grouped.setdefault(definition.group, []).append(definition)
    return grouped


def load_settings() -> dict[str, str]:
    """Läs sparad base-path och debug-inställning, annars projektroten/av."""
    settings: dict[str, str] = {BASE_KEY: str(ROOT), DEBUG_KEY: ""}
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get(BASE_KEY):
                settings[BASE_KEY] = str(stored[BASE_KEY])
            if isinstance(stored, dict):
                settings[DEBUG_KEY] = str(stored.get(DEBUG_KEY) or "")
        except (OSError, ValueError):
            pass
    return settings


def save_settings(settings: Mapping[str, str]) -> None:
    """Spara base-path och debug-inställning till generated/admin_settings.json."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(
            {
                BASE_KEY: settings.get(BASE_KEY, str(ROOT)),
                DEBUG_KEY: settings.get(DEBUG_KEY, ""),
            },
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def apply_debug_logging(settings: Mapping[str, str]) -> bool:
    """Applicera sparad debug-inställning på processmiljön.

    Nya jobb ärver miljön från processen som startar dem, så växlingen i
    Logg-fliken styr vad som loggas. Pågående jobb påverkas inte.
    """
    import os

    from operations.context import DEBUG_ENV, debug_value_enabled

    enabled: bool = debug_value_enabled(settings.get(DEBUG_KEY))
    os.environ[DEBUG_ENV] = "1" if enabled else ""
    return enabled


def resolve_path_default(default: object, settings: Mapping[str, str]) -> object:
    """Om-rotar en standardväg under ROOT till base-path.

    Sökvägar under projektroten (downloaded/, generated/, tessdata/ …) löses mot
    den konfigurerade base-path. Sökvägar utanför ROOT och None lämnas orörda.
    """
    if default is None:
        return None
    try:
        rel = Path(str(default)).relative_to(ROOT)
    except ValueError:
        return str(default)
    base = Path(settings.get(BASE_KEY) or ROOT)
    return str(base / rel)


def get_settings() -> dict[str, str]:
    """Returnera systeminställningarna (session_state-cachat, initierat från fil)."""
    import streamlit as st

    if "admin_settings" not in st.session_state:
        st.session_state["admin_settings"] = load_settings()
    settings: dict[str, str] = st.session_state["admin_settings"]
    return settings


def render_settings_tab() -> None:
    """Rendera Inställningar-fliken: endast base-path (underkataloger är hårdkodade)."""
    import streamlit as st

    settings = get_settings()
    st.subheader("Sökvägar")
    st.caption("Base-path som alla underkataloger (downloaded/, generated/, …) löses mot.")
    base = st.text_input(
        "Base-path",
        value=settings.get(BASE_KEY, str(ROOT)),
        key="setting_base",
    )
    st.caption(
        "Underkatalogerna är hårdkodade: "
        + ", ".join(rel for _, _, rel in SYSTEM_PATHS)
        + "."
    )
    if st.button("Spara inställningar", key="settings_save"):
        updated = {**get_settings(), BASE_KEY: base}
        save_settings(updated)
        st.session_state["admin_settings"] = updated
        st.session_state["_admin_settings_version"] = (
            st.session_state.get("_admin_settings_version", 0) + 1
        )
        st.success("Inställningar sparade.")


def render_llm_settings() -> None:
    """Rendera namngivna LLM-konfigurationer som ett sammanhållet formulär.

    Man lägger till/redigerar namngivna konfigurationer och väljer sedan vilken
    som ska användas per sida/operation. Sparas till generated/llm_config.json.
    API-nycklar visas eller lagras inte — endast om miljövariabeln är tillgänglig.
    """
    import os

    import streamlit as st

    import backends
    import config as llm_config

    if "llm_profiles" not in st.session_state:
        all_cfg = llm_config.load_all()
        st.session_state["llm_profiles"] = all_cfg["profiles"]
        st.session_state["llm_default"] = all_cfg["default"]

    profiles: dict[str, dict] = st.session_state["llm_profiles"]
    default_name: str = st.session_state["llm_default"]

    st.subheader("LLM-konfigurationer")
    st.caption(
        "Varje konfiguration samlar namn, tjänst, modell och autentisering. "
        "Välj sedan konfiguration per sida eller operation."
    )

    flash = st.session_state.pop("llm_flash", None)
    if flash:
        st.success(flash)

    profile_names = list(profiles.keys())

    # Pending-val (från lägg till/ta bort) appliceras innan selectboxen renderas.
    pending = st.session_state.pop("llm_pending_selection", None)
    if pending in profiles:
        st.session_state["llm_selected"] = pending
    if "llm_selected" not in st.session_state or st.session_state["llm_selected"] not in profiles:
        st.session_state["llm_selected"] = default_name
    creating = bool(st.session_state.get("llm_creating", False))

    with st.container(border=True):
        # vertical_alignment="bottom" lägger knapparna i nivå med dropdownen
        # i stället för med dess etikett.
        select_col, new_col, delete_col = st.columns([5, 1, 1], vertical_alignment="bottom")
        selected = select_col.selectbox(
            "Konfiguration",
            profile_names,
            key="llm_selected",
            disabled=creating,
            format_func=lambda name: f"{name} (standard)" if name == default_name else name,
        )
        if new_col.button("Ny", key="llm_new", use_container_width=True):
            st.session_state["llm_new_version"] = (
                st.session_state.get("llm_new_version", 0) + 1
            )
            st.session_state["llm_creating"] = True
            st.rerun()
        if delete_col.button(
            "Ta bort",
            key="llm_del",
            disabled=creating or len(profiles) <= 1,
            use_container_width=True,
        ):
            del profiles[selected]
            next_name = next(iter(profiles))
            if selected == default_name:
                default_name = next_name
            llm_config.save_profiles(profiles, default_name)
            st.session_state["llm_profiles"] = profiles
            st.session_state["llm_default"] = default_name
            st.session_state["llm_pending_selection"] = next_name
            st.session_state["llm_flash"] = f"'{selected}' borttagen."
            st.rerun()

        profile_key = (
            f"new_{st.session_state.get('llm_new_version', 0)}"
            if creating
            else selected
        )
        profile = dict(llm_config._DEFAULTS) if creating else profiles[selected]
        name = st.text_input(
            "Namn",
            value="" if creating else selected,
            key=f"llm_name_{profile_key}",
            placeholder="Exempel: DeepSeek snabb",
        )
        make_default = st.checkbox(
            "Använd som standardkonfiguration",
            value=not creating and selected == default_name,
            key=f"llm_make_default_{profile_key}",
            disabled=not creating and selected == default_name,
            help=(
                "Standardkonfigurationen kan inte avmarkeras. Välj en annan "
                "konfiguration och markera den i stället."
                if not creating and selected == default_name
                else None
            ),
        )

        keys = list(backends.BACKENDS.keys())
        saved_name = profile.get("backend_name", keys[0])
        backend_name = st.selectbox(
            "Tjänst",
            keys,
            index=keys.index(saved_name) if saved_name in keys else 0,
            key=f"llm_backend_{profile_key}",
        )
        backend = backends.BACKENDS[backend_name]
        field_defaults = llm_form_defaults(profile, backend_name, backend)

        saved_model = field_defaults["model"]
        base_url_key = f"llm_base_url_{profile_key}_{backend_name}"
        api_key_env_key = f"llm_api_key_env_{profile_key}_{backend_name}"
        selected_base_url = str(
            st.session_state.get(base_url_key, field_defaults["base_url"])
        )
        selected_api_key_env = str(
            st.session_state.get(api_key_env_key, field_defaults["api_key_env"])
        ).strip()

        @st.cache_data(ttl=300, show_spinner=False)
        def _cached_provider_models(base_url: str, api_key_env: str) -> list[str]:
            """Hämta modellkatalog utan att använda nyckelvärdet som cache-nyckel."""
            return cast(
                list[str], backends.fetch_models(base_url, os.environ.get(api_key_env, ""))
            )

        model_catalog = backends.available_models(
            {**backend, "base_url": selected_base_url},
            fetcher=lambda base_url, _api_key: _cached_provider_models(
                base_url, selected_api_key_env
            ),
        )
        model_options, model_selection = llm_model_options(
            {"models": model_catalog}, saved_model
        )
        model_choice = st.selectbox(
            "Modell",
            model_options,
            index=model_options.index(model_selection),
            key=f"llm_model_choice_{profile_key}_{backend_name}",
        )
        if model_choice == CUSTOM_MODEL_LABEL:
            model = st.text_input(
                "Eget modellnamn",
                value=saved_model if saved_model not in backend.get("models", []) else "",
                key=f"llm_custom_model_{profile_key}_{backend_name}",
            )
        else:
            model = model_choice

        credential_status = st.empty()

        with st.expander(
            "Avancerade inställningar",
            expanded=bool(backend.get("configurable")),
        ):
            base_url = st.text_input(
                "Base URL",
                value=field_defaults["base_url"],
                key=base_url_key,
                help="Endpoint för OpenAI-kompatibla tjänster; lämnas tom för Claude.",
            )
            api_key_env = st.text_input(
                "Miljövariabel för API-nyckel",
                value=field_defaults["api_key_env"],
                key=api_key_env_key,
                help="Endast variabelns namn sparas, aldrig själva API-nyckeln.",
            )

        # En tom override betyder inte att kända molntjänster blir nyckelfria:
        # runtime-konfigurationen faller då tillbaka till backend-katalogens env.
        env_key = api_key_env.strip() or str(backend.get("env") or "").strip()
        if env_key:
            available = bool(os.environ.get(env_key))
            status = "är tillgänglig" if available else "saknas i processmiljön"
            credential_status.caption(
                f"{'✓' if available else '⚠'} API-nyckeln `{env_key}` {status}."
            )
        else:
            credential_status.caption("✓ Ingen API-nyckel krävs av den valda tjänsten.")

        save_col, cancel_col = st.columns([3, 1])
        save_label = "Skapa konfiguration" if creating else "Spara ändringar"
        if save_col.button(save_label, key=f"llm_save_{profile_key}", type="primary"):
            payload = llm_profile_payload(
                backend_name=backend_name,
                provider=backend["kind"],
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
            )
            try:
                updated, updated_default = apply_llm_profile_form(
                    profiles,
                    selected_name=None if creating else selected,
                    entered_name=name,
                    payload=payload,
                    default_name=default_name,
                    make_default=make_default,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                saved_name = name.strip()
                llm_config.save_profiles(updated, updated_default)
                st.session_state["llm_profiles"] = updated
                st.session_state["llm_default"] = updated_default
                st.session_state["llm_creating"] = False
                st.session_state["llm_pending_selection"] = saved_name
                st.session_state["llm_flash"] = f"'{saved_name}' sparad."
                st.rerun()
        if creating and cancel_col.button("Avbryt", key=f"llm_cancel_{profile_key}"):
            st.session_state["llm_creating"] = False
            st.rerun()


def llm_model_options(backend: Mapping[str, object], saved_model: str) -> tuple[list[str], str]:
    """Returnera kända modellval och bevara ett sparat eget modellnamn."""
    configured_models = backend.get("models")
    models = (
        [str(model) for model in configured_models]
        if isinstance(configured_models, (list, tuple))
        else []
    )
    options = [*models, CUSTOM_MODEL_LABEL]
    selected = saved_model if saved_model in models else CUSTOM_MODEL_LABEL
    return options, selected


def llm_form_defaults(
    profile: Mapping[str, object],
    backend_name: str,
    backend: Mapping[str, object],
) -> dict[str, str]:
    """Använd profilvärden tills tjänsten byts, då tjänstens defaults gäller."""
    same_backend = profile.get("backend_name") == backend_name
    source = profile if same_backend else backend
    return {
        "model": str(source.get("model") or backend.get("model") or ""),
        "base_url": str(source.get("base_url") or backend.get("base_url") or ""),
        "api_key_env": str(source.get("api_key_env") or backend.get("env") or "").strip(),
    }


def apply_llm_profile_form(
    profiles: Mapping[str, Mapping[str, object]],
    *,
    selected_name: str | None,
    entered_name: str,
    payload: Mapping[str, object],
    default_name: str,
    make_default: bool,
) -> tuple[dict[str, dict], str]:
    """Applicera skapa/redigera/namnbyte och standardval i ett enda steg."""
    name = entered_name.strip()
    if not name:
        raise ValueError("Ange ett namn på konfigurationen.")
    if name in profiles and name != selected_name:
        raise ValueError(f"Det finns redan en konfiguration som heter '{name}'.")

    updated = {profile_name: dict(profile) for profile_name, profile in profiles.items()}
    if selected_name is not None:
        updated.pop(selected_name)
    updated[name] = dict(payload)

    if make_default or selected_name == default_name:
        default_name = name
    return updated, default_name


def llm_profile_payload(
    *,
    backend_name: str,
    provider: str,
    model: str,
    base_url: str,
    api_key_env: str,
) -> dict[str, str]:
    """Bygg den persistenta profilen utan hemliga API-nyckelvärden."""
    return {
        "backend_name": backend_name,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key_env": api_key_env.strip(),
    }


def numeric_input_bounds(
    name: str, kind: str
) -> tuple[int | float | None, int | float | None]:
    """Härled vettiga min/max-värden för en ``number_input``-widget.

    Antalsparametrar (jobs, batch, chunk-*) kräver positiva heltal, trösklar
    hålls inom 0–100 och DPI kräver ett positivt värde. Parametrar som använder
    0 som "ingen begränsning" (limit/test/top/min_freq) tillåter 0.
    """
    lowered = name.lower()
    if kind == "int":
        if "dpi" in lowered:
            return 1, None
        if any(marker in lowered for marker in ("jobs", "batch", "chunk", "per_file")):
            return 1, None
        return 0, None
    if kind == "float" and "threshold" in lowered:
        return 0.0, 100.0
    return None, None


def choice_form_options(parameter) -> tuple[list[str], object]:
    """Returnera (etiketter, förvalt värde) för en choice-parameter.

    Choice-parametrar med default None får ett "oförändrad"-alternativ först,
    som mappar till None så värdet kan lämnas orört.
    """
    if parameter.default is None:
        return [UNCHANGED_CHOICE_LABEL, *parameter.choices], UNCHANGED_CHOICE_LABEL
    return list(parameter.choices), parameter.default


def normalize_choice_selection(parameter, selection: str) -> object:
    """Mappa en widget-etikett till parameterns faktiska värde."""
    if selection == UNCHANGED_CHOICE_LABEL:
        return None
    return selection


def missing_required_paths(definition, params: Mapping[str, object]) -> list[str]:
    """Returnera namnen på obligatoriska path-parametrar utan angivet värde.

    Ett tomt fält normaliseras annars till ``Path("")`` (cwd) — det måste
    stoppas i formuläret i stället.
    """
    missing: list[str] = []
    for parameter in definition.parameters:
        if not parameter.required or parameter.kind != "path":
            continue
        value = params.get(parameter.name)
        if value is None or str(value).strip() in ("", "."):
            missing.append(parameter.name)
    return missing


# Filväljaren listar kataloger med tusentals filer och state.db-frågor; de
# cachas i en minut så att nya filer dyker upp utan att varje rendering läser om.
_CHOICE_CACHE: dict[object, tuple[float, list]] = {}
_CHOICE_TTL_SECONDS = 60.0


def _cached(key: object, build: Callable[[], list]) -> list:
    """Returnera ett cachat värde, eller bygg om det när TTL gått ut."""
    now = time.monotonic()
    cached = _CHOICE_CACHE.get(key)
    if cached is not None and now - cached[0] < _CHOICE_TTL_SECONDS:
        return list(cached[1])
    value = build()
    _CHOICE_CACHE[key] = (now, value)
    return list(value)


def _files_in(directory: str, pattern: str) -> list[str]:
    """Filnamn i en katalog (cachat — katalogerna kan ha tusentals filer)."""
    def build() -> list:
        path = Path(directory)
        if not path.is_dir():
            return []
        return sorted(entry.name for entry in path.glob(pattern) if entry.is_file())

    return _cached(("files", directory, pattern), build)


def _page_stems() -> list[str]:
    """Dokument med per-sida-text i state.db (underlag för merge)."""
    import os

    def build() -> list:
        from contextlib import closing

        import db as state_db

        with closing(state_db.connect()) as conn:
            state_db.init_schema(conn)
            return list(state_db.list_page_stems(conn))

    return _cached(("page_stems", os.environ.get("STATE_DB", "")), build)


def file_choices(
    sources: tuple[str, ...], pattern: str, *, settings: Mapping[str, str],
) -> list[tuple[str, str]]:
    """(värde, etikett) för filerna i väljarens källor, sorterade på etikett."""
    choices: dict[str, str] = {}
    for source in sources:
        if source == "state:pdf_pages":
            for stem in _page_stems():
                choices.setdefault(stem, stem)
            continue
        directory = Path(str(resolve_path_default(ROOT / source, settings)))
        for name in _files_in(str(directory), pattern):
            choices.setdefault(str(directory / name), Path(name).stem)
    return sorted(choices.items(), key=lambda item: item[1].casefold())


def _render_file_picker(
    spec: Mapping[str, Any], *, label: str, widget_key: str, disabled: bool,
    settings: Mapping[str, str],
) -> str:
    """Välj en befintlig fil i stället för att skriva sökvägen."""
    import streamlit as st

    choices = file_choices(tuple(spec["sources"]), str(spec["pattern"]), settings=settings)
    options = ["", *[value for value, _ in choices]]
    labels = {"": str(spec["empty"]), **{value: text for value, text in choices}}
    current = str(st.session_state.get(widget_key) or "")
    selected = st.selectbox(
        label,
        options,
        index=options.index(current) if current in options else 0,
        format_func=lambda value: labels.get(value, value),
        disabled=disabled,
        key=widget_key,
        help=f"{len(choices)} filer att välja mellan.",
    )
    if not selected:
        return ""
    return Path(selected).stem if spec["as"] == "stem" else str(selected)


def _render_parameter(
    parameter, *, widget_key: str, version: int, disabled: bool, label: str,
    picker: Mapping[str, Any] | None = None, settings: Mapping[str, str] | None = None,
) -> object:
    """Rendera en parameter och returnera dess värde."""
    import streamlit as st

    if picker is not None:
        return _render_file_picker(
            picker, label=label, widget_key=widget_key, disabled=disabled,
            settings=settings or {},
        )
    if parameter.name == "profile":
        import config as llm_config

        all_profiles = llm_config.load_all()
        profile_names = list(all_profiles["profiles"].keys())
        default_profile = all_profiles["default"]
        return st.selectbox(
            label,
            profile_names,
            index=profile_names.index(default_profile),
            key=widget_key,
            disabled=disabled,
        )
    if parameter.kind == "bool":
        return st.checkbox(
            label, value=bool(parameter.default), disabled=disabled, key=widget_key,
        )
    if parameter.kind in ("int", "float"):
        min_value, max_value = numeric_input_bounds(parameter.name, parameter.kind)
        # Titeln ritas separat: ``width`` begränsar annars även etiketten, och
        # ett smalt fält skulle tvinga fram radbrytning i rubriken.
        st.markdown(
            f"<div style='font-size:0.875rem;line-height:1.4;"
            f"margin:0 0 0.25rem 0'>{html.escape(label)}</div>",
            unsafe_allow_html=True,
        )
        kind = "int" if parameter.kind == "int" else "float"
        value = int(parameter.default or 0) if kind == "int" else float(parameter.default or 0.0)
        return st.number_input(
            label, value=value, step=1 if kind == "int" else None,
            min_value=min_value, max_value=max_value, disabled=disabled,
            key=widget_key, width=FIELD_WIDTHS[kind], label_visibility="collapsed",
        )
    if parameter.kind == "choice":
        options, default_selection = choice_form_options(parameter)
        index = options.index(default_selection) if default_selection in options else 0
        selection = st.selectbox(
            label, options=options, index=index, disabled=disabled,
            key=widget_key,
        )
        return normalize_choice_selection(parameter, selection)
    if parameter.kind == "path" and parameter.required:
        # Specifik fil användaren måste ange (t.ex. --in för sid-OCR).
        return st.text_input(
            label, value=str(parameter.default or ""), disabled=disabled,
            key=f"{widget_key}__v{version}",
        )
    return st.text_input(
        label, value=str(parameter.default or ""), disabled=disabled, key=widget_key,
    )


def _visible_parameters(definition, key: str) -> list:
    """Synliga parametrar i registryordning; hemliga och villkorliga tas bort.

    En parameter kan vara villkorad (run-pipelinens LLM-profil visas först när
    ``with_llm`` är valt). Värdet läses ur widgetens session state — det är
    samma värde widgeten visar — med parameterns default som fallback.
    """
    import streamlit as st

    with_llm = next((p for p in definition.parameters if p.name == "with_llm"), None)
    profilen_syns = with_llm is None or bool(
        st.session_state.get(f"{key}__with_llm", with_llm.default)
    )
    har_profil = any(p.name == "profile" for p in definition.parameters)
    return [
        parameter for parameter in definition.parameters
        if not parameter.secret
        and not (parameter.name == "profile" and not profilen_syns)
        and not (har_profil and parameter.name in LEGACY_LLM_PARAMS)
    ]


def field_weight(label: str) -> int:
    """Kolumnvikt för ett fält: ungefär så brett att etiketten ryms på en rad."""
    return max(2, min(6, len(label) // 10))


def label_width_px(label: str) -> int:
    """Uppskattad bredd för en etikett så att titeln inte radbryts.

    Marginalen (+16 px) är den luft etiketten får i sin kolumn.
    """
    return int(len(label) * LABEL_CHAR_PX) + 16


def is_numeric_row(row: list) -> bool:
    """True när raden bara innehåller talfält."""
    return bool(row) and all(parameter.kind in FIELD_WIDTHS for parameter in row)


def numeric_row_spec(row: list, labels: Mapping[str, str]) -> tuple[list[int], int]:
    """Kolumner (px) och radbredd för en rad med bara talfält.

    Varje kolumn är så bred att etiketten ryms och fältet får plats, så fälten
    hamnar intill varandra i stället för utspridda över sidan.
    """
    widths = [
        max(label_width_px(labels.get(parameter.name) or parameter.help),
            FIELD_WIDTHS[parameter.kind] + 8)
        for parameter in row
    ]
    # Kolumnerna delar (radbredd - mellanrum), så exakt ett mellanrum läggs på.
    return widths, sum(widths) + ROW_GAP_PX


def row_specs(row: list, labels: Mapping[str, str]) -> list[int]:
    """Kolumnvikter för en rad, med en spacer sist så att raden packas till vänster."""
    weights = [
        field_weight(labels.get(parameter.name) or parameter.help)
        for parameter in row
    ]
    spacer = FORM_GRID - sum(weights)
    return [*weights, spacer] if spacer > 0 else weights


PROFILE_LABEL = "LLM-konfiguration"


def parameter_label(parameter, labels: Mapping[str, str]) -> str:
    """Etiketten formuläret visar för en parameter."""
    if parameter.name == "profile":
        return PROFILE_LABEL
    return labels.get(parameter.name) or parameter.help


def parameter_group(parameter) -> str:
    """Avsnittet en parameter hör till."""
    if parameter.kind == "bool":
        return GROUP_ALTERNATIVES
    if parameter.kind == "path":
        return GROUP_PATHS
    return GROUP_SETTINGS


def form_field_order(parameters: list) -> list:
    """Parametrarna i samma ordning som formuläret visar dem."""
    ordered: list = []
    for group, group_parameters in parameter_groups(parameters):
        for row in group_rows(group, group_parameters):
            ordered.extend(row)
    return ordered


def _silent_parameters(parameters: list) -> set[str]:
    """Parametrar som injiceras tyst i stället för att bli ett eget fält.

    Hårdkodade/om-rotade sökvägar (t.ex. ``--root`` och ``--txt``) ägs av
    Inställningar och ska inte bli fält i formuläret.
    """
    return {
        parameter.name for parameter in parameters
        if parameter.kind == "path" and not parameter.required
    }


def form_fields(definition, *, key: str | None = None) -> list:
    """Fälten formuläret visar, i renderingsordning."""
    parameters = _visible_parameters(definition, key or definition.id)
    silent = _silent_parameters(parameters)
    return form_field_order([
        parameter for parameter in parameters if parameter.name not in silent
    ])


def parameter_groups(parameters: list) -> list[tuple[str, list]]:
    """Gruppera formulärfälten i visningsordning; tomma grupper hoppas över.

    Registryordningen bevaras inom varje grupp.
    """
    grouped: dict[str, list] = {name: [] for name in GROUP_ORDER}
    for parameter in parameters:
        grouped[parameter_group(parameter)].append(parameter)
    return [(name, grouped[name]) for name in GROUP_ORDER if grouped[name]]


def group_rows(group: str, parameters: list) -> list[list]:
    """Radindelning inom en grupp.

    Kryssrutor får en egen rad var. Breda fält (val, text) läggs först i rader
    om ``FORM_COLUMNS``, sedan talfälten tillsammans i en kompakt rad — annars
    hamnar smala talfält med stora luckor bredvid breda fält.
    """
    if group == GROUP_ALTERNATIVES:
        return [[parameter] for parameter in parameters]
    wide = [parameter for parameter in parameters if parameter.kind not in FIELD_WIDTHS]
    narrow = [parameter for parameter in parameters if parameter.kind in FIELD_WIDTHS]
    rows = [wide[index:index + FORM_COLUMNS]
            for index in range(0, len(wide), FORM_COLUMNS)]
    rows += [narrow[index:index + FORM_COLUMNS]
             for index in range(0, len(narrow), FORM_COLUMNS)]
    return rows


def form_button_label(operation_id: str, fallback: str) -> str:
    """Knapptext för en operation; layouten kan korta den ('Hämta')."""
    return str(FORM_LAYOUT.get(operation_id, {}).get("button") or fallback)


def section_help_markdown(text: str, bullets: list[str]) -> str:
    """Hjälpen som markdown: förklaringen följd av punktlistan."""
    return "\n\n".join(part for part in (text, "\n".join(bullets)) if part)


def operation_help(operation_id: str) -> SectionHelp:
    """Hjälp för ett avsnitt, eller en tom hjälp."""
    return OPERATION_HELP.get(operation_id, SectionHelp(""))


def form_header(operation_id: str, fallback: str = "") -> str:
    """Rubrik för ett avsnitt i en flik.

    ``FORM_LAYOUT`` kan korta rubriken (t.ex. *Palmemordsarkivet* i stället för
    operationens etikett), annars används operationens etikett.
    """
    return str(FORM_LAYOUT.get(operation_id, {}).get("header") or fallback)


def render_operation_form(
    definition, *, disabled: bool = False, settings: Mapping[str, str] | None = None,
) -> dict[str, object] | None:
    """Rendera ett Streamlit-formulär för en operation.

    Parametrarna läggs i ett rutnät (``FORM_COLUMNS`` per rad) så att fälten inte
    blir onödigt breda på en sida i full bredd. Returnerar parametrarna som dict
    när användaren startar operationen, annars None. Riskoperationer kräver en
    bekräftelsecheckbox och obligatoriska path-parametrar måste ha ett värde
    innan startknappen aktiveras.
    """
    import streamlit as st

    settings = settings if settings is not None else get_settings()
    version = st.session_state.get("_admin_settings_version", 0)
    key = f"{definition.id}"

    layout = FORM_LAYOUT.get(definition.id, {})
    labels: Mapping[str, str] = layout.get("labels") or {}
    heading = form_header(definition.id, definition.label)
    parameters = _visible_parameters(definition, key)
    # Hårdkodade/om-rotade sökvägar injiceras tyst — de ska inte bli ett fält.
    # dry_run/rebuild blir en radioknapp när layouten säger det.
    silent = _silent_parameters(parameters)
    params: dict[str, object] = {
        parameter.name: resolve_path_default(parameter.default, settings)
        for parameter in parameters if parameter.name in silent
    }
    editable = form_field_order([
        parameter for parameter in parameters if parameter.name not in silent
    ])

    # Ett kort per operation ger tydlig avgränsning mellan sektionerna.
    with st.container(border=True):
        fields, help_column = st.columns([3, 1], gap="large")
        with help_column:
            help_info = operation_help(definition.id)
            meanings = dict(help_info.options)
            # Punkterna följer samma ordning som fälten i formuläret.
            bullets = [
                f"- **{parameter_label(parameter, labels)}** — {meanings[parameter.name]}"
                for parameter in editable
                if parameter.name in meanings
            ]
            # Förklaring och punktlista i samma stil som övrig hjälptext.
            if help_info.text or bullets:
                st.caption(section_help_markdown(help_info.text, bullets))

        with fields:
            # Förklaringen ligger till höger i kortet, så rubriken står ensam här.
            st.subheader(heading)

        groups = parameter_groups(editable)
        with fields:
            for group, group_parameters in groups:
                if len(groups) > 1:
                    st.markdown(f"##### {group}")
                for row in group_rows(group, group_parameters):
                    if is_numeric_row(row):
                        widths, total = numeric_row_spec(row, labels)
                        columns = st.columns(widths, gap="small", width=total)
                    else:
                        columns = st.columns(row_specs(row, labels))
                    for parameter, column in zip(row, columns, strict=False):
                        widget_key = f"{key}__{parameter.name}"
                        with column:
                            params[parameter.name] = _render_parameter(
                                parameter, widget_key=widget_key, version=version,
                                disabled=disabled,
                                label=parameter_label(parameter, labels),
                                picker=FILE_PICKERS.get((definition.id, parameter.name)),
                                settings=settings,
                            )

            # Obligatoriska path-parametrar utan värde får inte skickas vidare —
            # ett tomt fält skulle normaliseras till Path("") (cwd).
            missing = missing_required_paths(definition, params)
            if missing and not disabled:
                named = [
                    parameter_label(parameter, labels)
                    for parameter in editable if parameter.name in missing
                ] or missing
                st.error(f"Välj ett värde för: {', '.join(named)}")

            confirmed = True
            if definition.confirmation:
                confirmed = st.checkbox(
                    definition.confirmation, disabled=disabled, key=f"{key}__confirm",
                )

            clicked = st.button(
                form_button_label(definition.id, f"Starta {definition.label}"),
                disabled=disabled or not confirmed or bool(missing),
                key=f"{key}__start",
            )
    return params if clicked else None


def render_neo4j_section(*, disabled: bool = False) -> None:
    """Rendera Neo4j-sektionen: en knapp per åtgärd i ett gemensamt kort."""
    import streamlit as st

    from operations.job_service import start_job

    with st.container(border=True):
        fields, help_column = st.columns([3, 1], gap="large")
        with help_column:
            st.caption(section_help_markdown(NEO4J_HELP.text, [
                f"- **{label}** — {meaning}" for label, meaning in NEO4J_HELP.options
            ]))

        with fields:
            st.subheader(NEO4J_HEADING)
            columns = st.columns(len(NEO4J_BUTTONS))
            for column, (operation_id, label) in zip(columns, NEO4J_BUTTONS, strict=True):
                clicked = column.button(
                    label, disabled=disabled, key=f"{operation_id}__start",
                    use_container_width=True,
                )
                if not clicked:
                    continue
                try:
                    job = start_job(operation_id, {})
                except (ValueError, RuntimeError, OSError, OperationFailed) as exc:
                    st.error(f"Kunde inte starta: {exc}")
                else:
                    st.success(f"Startat jobb {job['id']}")
                    st.rerun(scope="app")


def render_active_job(job: Mapping[str, object]) -> None:
    """Rendera panelen för det aktiva jobbet."""
    import streamlit as st

    status = format_job_status(str(job.get("status", "")))
    st.subheader(f"Jobb: {job.get('id')} — {status}")
    st.write(f"Operation: {job.get('operation')}")

    fraction = progress_fraction(job)
    if fraction is not None:
        st.progress(min(1.0, max(0.0, fraction)), text=str(job.get("message") or ""))
    elif job.get("current_step"):
        st.write(f"Steg: {job['current_step']}")

    if job.get("message"):
        st.caption(str(job["message"]))


def render_log_tab(jobs: Sequence[Mapping[str, object]]) -> None:
    """Visa systemets loggar i ett enda fönster.

    Källväljaren listar systemloggen (felloggen, som alla steg skriver till) och
    de senaste jobben. Förvalet är det jobb som körs — annars systemloggen.
    Fönstret uppdateras automatiskt medan ett jobb körs.
    """
    import streamlit as st

    from operations.job_service import read_log_tail

    by_id = {str(job.get("id")): job for job in jobs}
    active = next((job for job in jobs if job.get("active_slot") is not None), None)
    options = [SYSTEM_LOG_OPTION, *by_id]
    default = str(active["id"]) if active else SYSTEM_LOG_OPTION

    settings = load_settings()
    apply_debug_logging(settings)
    errors_path = error_log_path()

    def label(value: str) -> str:
        job = by_id.get(value)
        return job_option_labels([job])[0] if job else "Systemlogg (nu)"

    # Auto-uppdatering bara medan ett jobb körs; annars är loggen statisk.
    @st.fragment(run_every=2.0 if active else None)
    def _render_log() -> None:
        if st.session_state.get("log_source") not in options:
            st.session_state["log_source"] = default

        # Filtren staplas i en smal kolumn så att loggfönstret får resten av
        # bredden i stället för att fyra fält delar på hela sidan.
        filters, window = st.columns([1, 4], gap="medium")
        with filters:
            source = st.selectbox(
                "Källa", options, format_func=label, key="log_source",
            )
            level = st.selectbox(
                "Nivå", tuple(LOG_LEVEL_LABELS),
                format_func=lambda value: LOG_LEVEL_LABELS[value], key="log_level",
            )
            query = st.text_input(
                "Sök i loggen", key="log_query",
                placeholder="t.ex. SKIP eller ett filnamn",
            )
            debug_on = st.toggle(
                "Debugloggning", value=debug_value_enabled(settings.get(DEBUG_KEY)),
                key="log_debug",
                help="Loggar beslut och avvikelser på debug-nivå i jobbloggen. "
                     "Gäller jobb som startas efter växlingen.",
            )

        job = by_id.get(source)
        log_path = (
            Path(str(job["log_path"])) if job and job.get("log_path") else errors_path
        )
        with window:
            if job is None:
                st.caption(f"Systemlogg — fel och avvikelser från alla steg ({errors_path})")
            else:
                status = format_job_status(str(job.get("status", "")))
                st.caption(f"Jobb: {job.get('operation')} · {status} · {log_path.name}")

            text = read_log_tail(log_path, lines=LOG_SCAN_LINES)
            if not text.strip():
                st.caption("Loggen är tom ännu.")
            else:
                lines = filter_log_lines(text, level=level, query=query)[-LOG_SHOW_LINES:]
                if lines:
                    st.code("\n".join(lines))
                else:
                    st.caption("Inga rader matchar filtret.")

        if debug_on != debug_value_enabled(settings.get(DEBUG_KEY)):
            updated = {**settings, DEBUG_KEY: "1" if debug_on else ""}
            save_settings(updated)
            apply_debug_logging(updated)
            settings.update(updated)
            st.rerun()

    _render_log()
