"""Testbara presenterare och formulärhelpers för adminsidan.

Hålls Streamlit-fri för de rena hjälparna så att beteendet kan testas utan att
produktionsoperationer körs. De Streamlit-beroende delarna läggs i samma modul
men isoleras så att enhetstesterna bara täcker den rena logiken.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from operations.registry import OperationRegistry

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_FILE = ROOT / "generated" / "admin_settings.json"

BASE_KEY = "base"

# Etikett för choice-parametrar med default None: användaren kan lämna värdet
# oförändrat i stället för att tvingas välja ett konkret alternativ.
UNCHANGED_CHOICE_LABEL = "oförändrad"

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


def group_admin_operations(registry: OperationRegistry) -> dict[str, list]:
    """Gruppera administrationssynliga operationer på (group, label)-ordning."""
    grouped: dict[str, list] = {}
    for definition in registry.admin_operations():
        grouped.setdefault(definition.group, []).append(definition)
    return grouped


def load_settings() -> dict[str, str]:
    """Läs sparad base-path, eller fall tillbaka på projektroten."""
    settings: dict[str, str] = {BASE_KEY: str(ROOT)}
    if SETTINGS_FILE.exists():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and stored.get(BASE_KEY):
                settings[BASE_KEY] = str(stored[BASE_KEY])
        except (OSError, ValueError):
            pass
    return settings


def save_settings(settings: Mapping[str, str]) -> None:
    """Spara base-path till generated/admin_settings.json."""
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps({BASE_KEY: settings.get(BASE_KEY, str(ROOT))}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


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
        updated = {BASE_KEY: base}
        save_settings(updated)
        st.session_state["admin_settings"] = updated
        st.session_state["_admin_settings_version"] = (
            st.session_state.get("_admin_settings_version", 0) + 1
        )
        st.success("Inställningar sparade.")


def render_llm_settings() -> None:
    """Rendera LLM-konfigurationer (enkel lista med namngivna profiler).

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
    st.caption("Lägg till/redigera namngivna konfigurationer; välj konfiguration per sida/operation.")

    profile_names = list(profiles.keys())

    # Pending-val (från lägg till/ta bort) appliceras innan selectboxen renderas.
    pending = st.session_state.pop("llm_pending_selection", None)
    if pending in profiles:
        st.session_state["llm_selected"] = pending
    if "llm_selected" not in st.session_state or st.session_state["llm_selected"] not in profiles:
        st.session_state["llm_selected"] = default_name
    selected = st.selectbox("Konfiguration", profile_names, key="llm_selected")

    col1, col2, col3 = st.columns(3)
    new_name = col1.text_input("Ny konfiguration", key="llm_new_name", label_visibility="collapsed", placeholder="Namn")
    if col2.button("Lägg till", key="llm_add"):
        name = new_name.strip()
        if name and name not in profiles:
            profiles[name] = dict(llm_config._DEFAULTS)
            st.session_state["llm_pending_selection"] = name
            st.rerun()
    if col3.button("Ta bort", key="llm_del", disabled=len(profiles) <= 1):
        del profiles[selected]
        next_name = next(iter(profiles))
        if selected == default_name:
            st.session_state["llm_default"] = next_name
        # Persistens direkt: annars återkommer borttagna profiler nästa session.
        llm_config.save_profiles(profiles, st.session_state["llm_default"])
        st.session_state["llm_pending_selection"] = next_name
        st.rerun()
    if st.button("Sätt som standard", key="llm_default_btn"):
        st.session_state["llm_default"] = selected
        llm_config.save_profiles(profiles, selected)
        st.success(f"Standard: {selected}")

    st.markdown(f"Standard: **{default_name}**")
    st.divider()

    profile = profiles[selected]
    keys = list(backends.BACKENDS.keys())
    saved_name = profile.get("backend_name", keys[0])

    backend_name = st.selectbox(
        "AI-modell",
        keys,
        index=keys.index(saved_name) if saved_name in keys else 0,
        key=f"llm_backend_{selected}",
    )
    backend = backends.BACKENDS[backend_name]

    env_key = profile.get("api_key_env") or backend.get("env")
    if env_key:
        available = bool(os.environ.get(env_key))
        st.caption(f"{'✓' if available else '✗'} {env_key} {'är tillgänglig' if available else 'saknas'} i processmiljön")

    model = st.text_input(
        "Modell",
        value=profile.get("model") or backend.get("model", ""),
        key=f"llm_model_{selected}",
    )
    base_url = st.text_input(
        "Base URL",
        value=profile.get("base_url") or backend.get("base_url", ""),
        key=f"llm_base_url_{selected}",
        help="Endast för OpenAI-kompatibla backends; lämna tomt för Claude.",
    )
    api_key_env = st.text_input(
        "Miljövariabel för API-nyckel",
        value=profile.get("api_key_env") or backend.get("env") or "",
        key=f"llm_api_key_env_{selected}",
        help="Endast variabelns namn sparas, aldrig själva API-nyckeln.",
    )

    if st.button("Spara konfiguration", key="llm_save"):
        profiles[selected] = llm_profile_payload(
            backend_name=backend_name,
            provider=backend["kind"],
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
        )
        llm_config.save_profiles(profiles, st.session_state["llm_default"])
        st.success(f"'{selected}' sparad.")


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


def numeric_input_bounds(name: str, kind: str) -> tuple[float | None, float | None]:
    """Härled vettiga min/max-värden för en ``number_input``-widget.

    Antalsparametrar (jobs, batch, chunk-*) kräver positiva heltal, trösklar
    hålls inom 0–100 och DPI kräver ett positivt värde. Parametrar som använder
    0 som "ingen begränsning" (limit/test/top/min_freq) tillåter 0.
    """
    lowered = name.lower()
    if kind == "int":
        if "dpi" in lowered:
            return 1.0, None
        if any(marker in lowered for marker in ("jobs", "batch", "chunk", "per_file")):
            return 1.0, None
        return 0.0, None
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


def render_operation_form(definition, *, disabled: bool = False, settings: Mapping[str, str] | None = None) -> dict[str, object] | None:
    """Rendera ett Streamlit-formulär för en operation.

    Returnerar parametrarna som dict när användaren startar operationen,
    annars None. Riskoperationer kräver en bekräftelsecheckbox och obligatoriska
    path-parametrar måste ha ett värde innan startknappen aktiveras.
    """
    import streamlit as st

    settings = settings if settings is not None else get_settings()
    version = st.session_state.get("_admin_settings_version", 0)
    key = f"{definition.id}"

    params: dict[str, object] = {}
    for parameter in definition.parameters:
        if parameter.secret:
            continue
        widget_key = f"{key}__{parameter.name}"
        if parameter.name == "profile":
            import config as llm_config

            profile_names = list(llm_config.load_all()["profiles"].keys())
            params[parameter.name] = st.selectbox(
                "LLM-konfiguration", profile_names, key=widget_key, disabled=disabled
            )
            continue
        if parameter.kind == "bool":
            params[parameter.name] = st.checkbox(
                parameter.help, value=bool(parameter.default), disabled=disabled,
                key=widget_key,
            )
        elif parameter.kind == "int":
            min_value, max_value = numeric_input_bounds(parameter.name, parameter.kind)
            params[parameter.name] = st.number_input(
                parameter.help, value=int(parameter.default or 0), step=1,
                min_value=min_value, max_value=max_value, disabled=disabled,
                key=widget_key,
            )
        elif parameter.kind == "float":
            min_value, max_value = numeric_input_bounds(parameter.name, parameter.kind)
            params[parameter.name] = st.number_input(
                parameter.help, value=float(parameter.default or 0.0),
                min_value=min_value, max_value=max_value, disabled=disabled,
                key=widget_key,
            )
        elif parameter.kind == "choice":
            options, default_selection = choice_form_options(parameter)
            index = options.index(default_selection) if default_selection in options else 0
            selection = st.selectbox(
                parameter.help, options=options, index=index, disabled=disabled,
                key=widget_key,
            )
            params[parameter.name] = normalize_choice_selection(parameter, selection)
        elif parameter.kind == "path":
            if parameter.required:
                # Specifik fil användaren måste ange (t.ex. --in för sid-OCR).
                params[parameter.name] = st.text_input(
                    parameter.help, value=str(parameter.default or ""), disabled=disabled,
                    key=f"{widget_key}__v{version}",
                )
            else:
                # Hårdkodad/om-rotad sökväg — injicera upplöst värde tyst.
                params[parameter.name] = resolve_path_default(parameter.default, settings)
        else:
            params[parameter.name] = st.text_input(
                parameter.help, value=str(parameter.default or ""), disabled=disabled,
                key=widget_key,
            )

    # Obligatoriska path-parametrar utan värde får inte skickas vidare —
    # ett tomt fält skulle normaliseras till Path("") (cwd).
    missing = missing_required_paths(definition, params)
    if missing and not disabled:
        st.error(f"Ange ett värde för: {', '.join(missing)}")

    confirmed = True
    if definition.confirmation:
        confirmed = st.checkbox(definition.confirmation, disabled=disabled, key=f"{key}__confirm")

    if st.button(
        f"Starta {definition.label}",
        disabled=disabled or not confirmed or bool(missing),
        key=f"{key}__start",
    ):
        return params
    return None


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
