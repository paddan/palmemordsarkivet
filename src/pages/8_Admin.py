"""Lokal administrationssida för produktionsflöden och bakgrundsjobb."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import casebook_ui as _casebook_ui
import db
from admin_ui import (
    NEO4J_OPERATION_IDS,
    TERMINAL_JOB_STATUSES,
    format_job_status,
    group_admin_operations,
    job_failure_text,
    latest_finished_job,
    operation_label,
    render_active_job,
    render_job_result,
    render_llm_settings,
    render_log_tab,
    render_neo4j_section,
    render_operation_form,
    render_settings_tab,
)
from operations.job_service import (
    cancel_job,
    reconcile_active_job,
    start_job,
)
from operations.registry import get_registry

st.set_page_config(page_title="Admin", page_icon="⚙️", layout="wide")

_casebook_ui.render_page_header("Admin")

registry = get_registry()

# Startbekräftelsen sparas till körningen efter st.rerun — annars hinner den
# aldrig synas.
started_label = st.session_state.pop("job_started_label", None)
if started_label:
    st.toast(f"{started_label} startat")

# --- Aktivt jobb: status, senaste loggrader och avbryt ---
initial_job = reconcile_active_job()
if initial_job is None:
    # Senaste avslutade jobbets utfall står kvar tills det döljs.
    conn = db.connect()
    db.init_schema(conn)
    latest_jobs = [dict(job) for job in db.list_admin_jobs(conn, limit=1)]
    conn.close()
    dismissed = st.session_state.setdefault("dismissed_job_results", set())
    finished = latest_finished_job(latest_jobs, dismissed=dismissed)
    if finished is not None and render_job_result(finished):
        dismissed.add(str(finished["id"]))
        st.rerun()
    if finished is None:
        st.caption("Inget jobb körs.")
else:
    @st.fragment(run_every=2.0)
    def active_job_fragment() -> None:
        job = reconcile_active_job()
        if job is None:
            # Jobbet är klart: rita om hela sidan så resultatrutan visas.
            st.rerun(scope="app")
        render_active_job(job)
        if st.button("Avbryt", key="active_job_cancel"):
            cancel_job()
            st.rerun(scope="app")

    active_job_fragment()

st.divider()

# --- Flikar i pipelineordning; inställningar sist ---
GROUP_ORDER = ["Pipeline", "OCR", "Kvalitet", "Index", "Extraktion och graf"]

grouped = group_admin_operations(registry)
grouped.pop("LLM-inställningar", None)
active_mutating = initial_job is not None and initial_job["status"] not in TERMINAL_JOB_STATUSES

# Neo4j-kortet ligger först i graf-fliken: grafen kräver en startad databas.
GRAPH_GROUP = "Extraktion och graf"

ordered_group_names = [g for g in GROUP_ORDER if g in grouped]
tab_names = [*ordered_group_names, "Jobbhistorik", "Logg", "Inställningar"]

conn = db.connect()
db.init_schema(conn)
recent_jobs = [dict(job) for job in db.list_admin_jobs(conn, limit=20)]
conn.close()


def _start_operation(definition, *, disabled: bool) -> None:
    params = render_operation_form(definition, disabled=disabled)
    if params is None:
        return
    try:
        start_job(definition.id, params)
        st.session_state["job_started_label"] = operation_label(definition.id)
        st.rerun(scope="app")
    except Exception as exc:  # noqa: BLE001
        st.error(f"Kunde inte starta: {exc}")


# Vald flik sparas på servern (nyckel + on_change). Utan nyckel räknas
# flikarnas identitet ur positionen, och när toppen byts (resultatruta ↔
# jobbpanel vid start/avslut) blev de ett nytt element som hoppade till Pipeline.
# Valet speglas dessutom till admin_tab_sparad och blir default, eftersom
# widgetstate kan tappas i en omkörning som inte bär det (samma skäl som
# sökvalens spegling i Utredning).
def _spara_flik() -> None:
    st.session_state["admin_tab_sparad"] = st.session_state.get("admin_tab")


saved_tab = st.session_state.get("admin_tab_sparad")
tabs = st.tabs(
    tab_names, key="admin_tab", on_change=_spara_flik,
    default=saved_tab if saved_tab in tab_names else None,
)
for tab, tab_name in zip(tabs, tab_names, strict=True):
    with tab:
        if tab_name == "Inställningar":
            render_settings_tab()
            st.divider()
            render_llm_settings()
        elif tab_name == "Logg":
            render_log_tab(recent_jobs)
        elif tab_name == "Jobbhistorik":
            if not recent_jobs:
                st.caption("Inga jobb ännu.")
            else:
                for job in recent_jobs:
                    status = format_job_status(str(job["status"]))
                    col1, col2 = st.columns([5, 1])
                    col1.markdown(
                        f"**{job['operation']}** — {status} · `{job['id'][:8]}` · {job['created_at']}"
                    )
                    deletable = job["active_slot"] is None
                    if col2.button("Ta bort", key=f"delete_{job['id']}", disabled=not deletable):
                        conn = db.connect()
                        db.init_schema(conn)
                        db.delete_admin_job(conn, job["id"])
                        conn.close()
                        log_path = job.get("log_path")
                        if log_path:
                            Path(log_path).unlink(missing_ok=True)
                        st.rerun(scope="app")
                    if job.get("error"):
                        st.caption(f"Fel: {job_failure_text(job)}")
                    if job.get("message"):
                        st.caption(f"Meddelande: {job['message']}")
        else:
            # Varje operation renderas som ett eget kort med rubrik och hjälp
            # (se render_operation_form).
            if tab_name == GRAPH_GROUP:
                render_neo4j_section(disabled=active_mutating)
            for definition in grouped[tab_name]:
                if definition.id in NEO4J_OPERATION_IDS:
                    continue
                _start_operation(definition, disabled=active_mutating)
