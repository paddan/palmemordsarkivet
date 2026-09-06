"""Stegvis källgranskning av grafposter utan krav på en ansluten Neo4j."""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import streamlit as st

import config
import db
from graph.review import (
    audit_entries,
    preview_name_rule,
    validate_decision,
    validate_suggestion_evidence,
)
from graph.review_service import fingerprint
from operations import job_service
from operations.exceptions import OperationFailed

_STATUS_LABELS = {
    "queued": "väntar på att starta",
    "running": "pågår",
    "cancel_requested": "avbryts",
    "succeeded": "är klar",
    "failed": "misslyckades",
    "cancelled": "avbröts",
    "interrupted": "avbröts vid omstart",
}
_TYPE_LABELS = {
    "person": "Person",
    "plats": "Plats",
    "organisation": "Organisation",
}
_REVIEW_OPERATIONS = {"graph-review", "graph-review-llm", "graph-sync"}


def source_excerpt(source: str, evidence: str, original: dict, *, radius: int = 180) -> str:
    """Returnera ett kort utdrag runt belägg eller aktuellt namn."""
    if not source:
        return "Källtext saknas."
    needles = [evidence]
    needles.extend(str(original.get(key, "")) for key in ("namn", "fran", "till"))
    folded = source.casefold()
    match_start = 0
    match_length = 0
    for needle in needles:
        clean = needle.strip()
        if not clean:
            continue
        index = folded.find(clean.casefold())
        if index >= 0:
            match_start, match_length = index, len(clean)
            break
    start = max(0, match_start - radius)
    end = min(len(source), match_start + match_length + radius)
    excerpt = source[start:end].strip()
    return ("…" if start else "") + excerpt + ("…" if end < len(source) else "")


def preview_summary(report: dict) -> list[str]:
    """Översätt en synkdiff till korta användarvända meningar."""
    diff = report.get("diff", {})
    lines = [
        f"Omnämnanden: {diff.get('mentions_add', 0)} läggs till och "
        f"{diff.get('mentions_remove', 0)} tas bort.",
        f"Relationer: {diff.get('relations_add', 0)} läggs till och "
        f"{diff.get('relations_remove', 0)} tas bort.",
    ]
    legacy = diff.get("legacy_edges", 0)
    if legacy:
        lines.append(
            f"Engångsförberedelsen omfattar även {legacy} äldre importerade kanter."
        )
    return lines


def _start(operation: str, params: dict) -> None:
    """Starta ett jobb och beskriv utfallet utan att visa dess interna id."""
    try:
        job_service.start_job(operation, params)
        st.success("Arbetet har startat. Uppdatera status om en stund.")
    except (ValueError, RuntimeError, OSError, OperationFailed) as exc:
        st.error(str(exc))


def _save(item: dict, action: str, target: dict, note: str) -> None:
    """Kontrollera den visade källan igen i samma transaktion som beslutet."""
    validate_decision(item, action, target, note)
    with closing(db.connect()) as conn, db.graph_review_write_transaction(conn):
        entries, decisions = db.read_graph_review_snapshot(conn)
        fresh = next(
            (value for value in audit_entries(entries, decisions)
             if value["item_key"] == item["item_key"]),
            None,
        )
        if fresh is None or fresh["source_hash"] != item["source_hash"]:
            raise ValueError("Källan har ändrats. Uppdatera sidan innan du sparar.")
        if fresh["decision"] != item.get("decision"):
            raise ValueError("Granskningsbeslutet har ändrats. Uppdatera sidan.")
        db.save_graph_review_decision(
            conn,
            item_key=item["item_key"],
            source_hash=item["source_hash"],
            action=action,
            target=target,
            note=note,
        )


def _resolve_suggestion(
    suggestion: dict, accept: bool, expected_decision: dict | None = None,
) -> None:
    """Godkänn eller avvisa exakt det visade förslaget under skrivlås."""
    with closing(db.connect()) as conn, db.graph_review_write_transaction(conn):
        current = next(
            (
                value
                for value in db.list_graph_review_suggestions(conn)
                if value["item_key"] == suggestion["item_key"]
                and value["source_hash"] == suggestion["source_hash"]
            ),
            None,
        )
        if current is None or current != suggestion or current["status"] != "pending":
            raise ValueError("Förslaget har ändrats. Uppdatera sidan.")
        entries, decisions = db.read_graph_review_snapshot(conn)
        item = next(
            (value for value in audit_entries(entries, decisions)
             if value["item_key"] == suggestion["item_key"]),
            None,
        )
        if accept:
            if item is None or item["source_hash"] != suggestion["source_hash"]:
                raise ValueError(
                    "Källan har ändrats. Det inaktuella förslaget kan bara avvisas."
                )
            if item["decision"] != expected_decision:
                raise ValueError("Granskningsbeslutet har ändrats. Uppdatera sidan.")
            validate_decision(
                item, suggestion["action"], suggestion["target"], suggestion["note"]
            )
            source = db.get_graph_review_page(conn, item["pdf_stem"], item["page_num"])
            validate_suggestion_evidence(
                item,
                suggestion["action"],
                suggestion["target"],
                suggestion["evidence"],
                source,
            )
            db.save_graph_review_decision(
                conn,
                item_key=item["item_key"],
                source_hash=item["source_hash"],
                action=suggestion["action"],
                target=suggestion["target"],
                note=suggestion["note"],
            )
        db.set_graph_review_suggestion_status(
            conn,
            suggestion["item_key"],
            suggestion["source_hash"],
            "accepted" if accept else "rejected",
        )


def _item_title(value: dict, kind: str) -> str:
    if kind == "entity":
        raw_type = str(value.get("typ") or "Okänd typ")
        return f"{value.get('namn', 'Namnlös')} · {_TYPE_LABELS.get(raw_type, raw_type.title())}"
    return (
        f"{value.get('fran', '?')} → {value.get('till', '?')} "
        f"· {value.get('typ', 'Okänd relation')}"
    )


def _review_queue(
    items: list[dict],
    suggestions: list[dict],
    *,
    state: str = "Att granska",
    kind: str = "Alla",
    query: str = "",
) -> list[tuple[dict, dict | None]]:
    """Prioritera aktuella LLM-förslag före regelbaserade fynd."""
    item_by_key = {item["item_key"]: item for item in items}
    current_suggestions: list[tuple[dict, dict | None]] = []
    for suggestion in suggestions:
        item = item_by_key.get(suggestion["item_key"])
        if item and item["source_hash"] == suggestion["source_hash"]:
            current_suggestions.append((item, suggestion))
    suggested = {(item["item_key"], item["source_hash"]) for item, _ in current_suggestions}
    remaining = [
        (item, None)
        for item in items
        if (item["item_key"], item["source_hash"]) not in suggested
        and bool(item["issues"])
        and (not item["decision"] or item["stale"])
    ]
    queue = current_suggestions + remaining
    if state != "Att granska":
        if state == "Alla":
            queue = [(item, next(
                (s for s in suggestions if s["item_key"] == item["item_key"]
                 and s["source_hash"] == item["source_hash"]),
                None,
            )) for item in items]
        elif state == "Granskade":
            queue = [(item, None) for item in items if item["decision"] and not item["stale"]]
        elif state == "Inaktuella":
            queue = [(item, None) for item in items if item["stale"]]
    wanted_kind = {"Entiteter": "entity", "Relationer": "relation"}.get(kind)
    folded_query = query.casefold().strip()
    return [
        (item, suggestion)
        for item, suggestion in queue
        if (not wanted_kind or item["kind"] == wanted_kind)
        and (
            not folded_query
            or folded_query
            in f"{item['pdf_stem']} {item['original']}".casefold()
        )
    ]


def _render_status(jobs: list[dict]) -> None:
    relevant = [job for job in jobs if job["operation"] in _REVIEW_OPERATIONS]
    if relevant:
        job = relevant[0]
        operation = {
            "graph-review": "Regelanalysen",
            "graph-review-llm": "LLM-analysen",
            "graph-sync": "Grafuppdateringen",
        }[job["operation"]]
        st.info(f"{operation} {_STATUS_LABELS.get(job['status'], job['status'])}.")
    st.button("Uppdatera status")


def _render_analysis(profiles: dict, jobs: list[dict], review_count: int) -> None:
    st.subheader("1. Analysera")
    st.write(
        "Analysen hittar osäkra namn och relationer och föreslår förbättringar. "
        "Inget ändras i grafen förrän du själv har granskat förslagen."
    )
    noun = "post" if review_count == 1 else "poster"
    st.caption(
        f"{review_count} {noun} behöver granskas. En LLM-omgång tar högst 20 källsidor."
    )
    default = profiles["default"]
    if st.button("Analysera med LLM", type="primary", use_container_width=True):
        _start("graph-review-llm", {"profile": default, "limit": 20})
    _render_status(jobs)
    with st.expander("Avancerade inställningar"):
        names = list(profiles["profiles"])
        profile = st.selectbox(
            "Alternativ LLM-profil",
            names,
            index=names.index(default),
        )
        limit = st.number_input(
            "Max antal källsidor", min_value=0, value=20,
            help="Ange 0 endast om alla återstående källsidor ska analyseras i samma körning.",
        )
        if st.button("Analysera valda med LLM"):
            _start("graph-review-llm", {"profile": profile, "limit": limit})
        if st.button("Analysera enbart med regler"):
            _start("graph-review", {})
        st.caption(
            "Vid felsökning finns fullständiga jobb och loggar på Admin-sidan."
        )


def _render_source(root: Path, item: dict, suggestion: dict | None, source: str) -> None:
    from casebook_ui import find_pdf
    from citations import pdf_anchor

    evidence = suggestion["evidence"] if suggestion else ""
    st.markdown("**Källutdrag**")
    st.write(source_excerpt(source, evidence, item["original"]))
    with st.expander("Visa källa"):
        st.text_area(
            "Hela källsidans text",
            source,
            height=260,
            disabled=True,
            key=f"source-{item['source_hash']}",
        )
        pdf = find_pdf(root, item["pdf_stem"])
        if pdf:
            st.markdown(
                pdf_anchor(pdf, "Öppna källsidan i PDF", page=item["page_num"]),
                unsafe_allow_html=True,
            )
        else:
            st.info("Original-PDF saknas lokalt.")


def _run_action(callback) -> None:
    try:
        callback()
    except ValueError as exc:
        st.error(str(exc))
    else:
        st.rerun()


def _render_manual_form(item: dict, *, key_suffix: str = "") -> None:
    fields = ("namn", "typ") if item["kind"] == "entity" else ("fran", "typ", "till")
    with st.form(f"manual-{item['item_key']}-{item['source_hash']}-{key_suffix}"):
        st.markdown("**Redigera posten**")
        target = {
            field: st.text_input(
                {"namn": "Namn", "fran": "Från", "typ": "Typ", "till": "Till"}[field],
                value=item["original"].get(field, ""),
            )
            for field in fields
        }
        note = st.text_area("Motivering")
        if st.form_submit_button("Spara ändring", type="primary"):
            _run_action(lambda: _save(item, "replace", target, note))


def _render_name_rules(entries: list[dict], decisions: list[dict], name_rules: list[dict]) -> None:
    """Visa och ändra typbundna namnregler som gäller hela grafprojektionen."""
    st.markdown("#### Ersätt namn i hela grafen")
    st.write(
        "En namnregel ersätter en variant överallt i den granskade grafen. "
        "Originalextraktionen ändras inte."
    )
    with st.expander("Lägg till eller ändra namnregel"):
        labels = {"Person": "person", "Plats": "plats", "Organisation": "organisation"}
        selected_label = st.selectbox("Entitetstyp", list(labels))
        source = st.text_input("Variant", placeholder="t.ex. RKA2")
        target = st.text_input("Kanoniskt namn", placeholder="t.ex. Rikskriminalen A2")
        candidate = {"typ": labels[selected_label], "source": source, "target": target}
        if st.button("Visa träffar"):
            if not source.strip() or not target.strip():
                st.error("Fyll i både variant och kanoniskt namn.")
            else:
                st.session_state["graph-name-rule-preview"] = {
                    "candidate": candidate,
                    "counts": preview_name_rule(entries, decisions, name_rules, candidate),
                }
        preview = st.session_state.get("graph-name-rule-preview")
        if preview and preview["candidate"] == candidate:
            counts = preview["counts"]
            st.info(
                f"Regeln påverkar {counts['entities']} omnämnanden på {counts['pages']} sidor "
                f"och {counts['relations']} relationsförekomster."
            )
            if st.button("Spara namnregel", type="primary"):
                def save_rule() -> None:
                    with closing(db.connect()) as conn:
                        db.init_schema(conn)
                        db.save_graph_name_rule(conn, **candidate)
                _run_action(save_rule)
    if name_rules:
        st.markdown("**Aktiva namnregler**")
        for rule in name_rules:
            label = _TYPE_LABELS[rule["typ"]]
            columns = st.columns([6, 2])
            columns[0].markdown(f"{label}: `{rule['source']}` → `{rule['target']}`")
            if columns[1].button(
                f"Ta bort {rule['source']}", key=f"delete-name-rule-{rule['typ']}-{rule['source']}"
            ):
                def delete_rule(rule: dict = rule) -> None:
                    with closing(db.connect()) as conn:
                        db.init_schema(conn)
                        if not db.delete_graph_name_rule(conn, typ=rule["typ"], source=rule["source"]):
                            raise ValueError("Namnregeln har redan tagits bort. Uppdatera sidan.")
                _run_action(delete_rule)


def _render_decision_actions(item: dict, suggestion: dict | None) -> None:
    key = hashlib.sha256(
        f"{item['item_key']}:{item['source_hash']}".encode()
    ).hexdigest()
    if suggestion:
        stale = item["source_hash"] != suggestion["source_hash"]
        if stale:
            st.warning("Förslaget är inaktuellt och kan bara avvisas.")
        cols = st.columns(3)
        if cols[0].button("Godkänn", type="primary", disabled=stale, key=f"accept-{key}"):
            _run_action(
                lambda: _resolve_suggestion(suggestion, True, item.get("decision"))
            )
        if cols[1].button("Avvisa", key=f"reject-{key}"):
            _run_action(lambda: _resolve_suggestion(suggestion, False))
        if cols[2].button("Redigera själv", key=f"edit-{key}"):
            st.session_state[f"graph-edit-{key}"] = True
    else:
        cols = st.columns(3)
        if cols[0].button("Behåll", type="primary", key=f"keep-{key}"):
            _run_action(
                lambda: _save(item, "keep", {}, "Manuellt kontrollerad mot källan.")
            )
        if cols[1].button("Uteslut", key=f"exclude-{key}"):
            _run_action(
                lambda: _save(item, "exclude", {}, "Manuellt utesluten efter källkontroll.")
            )
        if cols[2].button("Redigera", key=f"edit-{key}"):
            st.session_state[f"graph-edit-{key}"] = True
    if st.session_state.get(f"graph-edit-{key}"):
        _render_manual_form(item, key_suffix=key)


def _render_review(
    root: Path, entries: list[dict], decisions: list[dict], name_rules: list[dict],
    items: list[dict], suggestions: list[dict],
) -> None:
    st.subheader("2. Granska")
    _render_name_rules(entries, decisions, name_rules)
    state, kind, query = "Att granska", "Alla", ""
    with st.expander("Sök, filtrera och se historik"):
        query = st.text_input("Sök namn eller dokument")
        kind = st.selectbox("Posttyp", ["Alla", "Entiteter", "Relationer"])
        state = st.selectbox(
            "Granskningsstatus", ["Att granska", "Alla", "Granskade", "Inaktuella"]
        )
    queue = _review_queue(items, suggestions, state=state, kind=kind, query=query)
    if not queue:
        st.success("Det finns inga poster i det valda urvalet.")
        return
    signature = "|".join(item["item_key"] + item["source_hash"] for item, _ in queue)
    if st.session_state.get("graph-review-signature") != signature:
        st.session_state["graph-review-signature"] = signature
        st.session_state["graph-review-index"] = 0
    index = min(st.session_state.get("graph-review-index", 0), len(queue) - 1)
    item, suggestion = queue[index]
    st.caption(f"Post {index + 1} av {len(queue)} · {item['pdf_stem']}, sida {item['page_num']}")
    st.markdown(f"### {_item_title(item['original'], item['kind'])}")
    for issue in item["issues"]:
        st.warning(issue)
    if suggestion:
        st.markdown("**LLM föreslår**")
        st.markdown(f"### {_item_title(suggestion['target'] if suggestion['action'] == 'replace' else item['original'], item['kind'])}")
        action_label = {"keep": "Behåll", "exclude": "Uteslut", "replace": "Ändra"}[suggestion["action"]]
        st.write(f"Åtgärd: {action_label}")
        st.write(f"Motivering: {suggestion['note']}")
        st.write(f"Källbelägg: {suggestion['evidence']}")
    with closing(db.connect()) as conn:
        source = db.get_graph_review_page(conn, item["pdf_stem"], item["page_num"])
        history = db.get_graph_review_decision_history(conn, item["item_key"])
    _render_source(root, item, suggestion, source)
    _render_decision_actions(item, suggestion)
    nav = st.columns(3)
    if nav[0].button("Föregående", disabled=index == 0):
        st.session_state["graph-review-index"] = index - 1
        st.rerun()
    if nav[1].button("Hoppa över", disabled=len(queue) == 1):
        st.session_state["graph-review-index"] = (index + 1) % len(queue)
        st.rerun()
    if nav[2].button("Nästa", disabled=index == len(queue) - 1):
        st.session_state["graph-review-index"] = index + 1
        st.rerun()
    if history:
        with st.expander("Beslutshistorik"):
            for decision in reversed(history):
                st.write(
                    f"{decision['created_at']} · "
                    f"{ {'keep': 'Behölls', 'exclude': 'Uteslöts', 'replace': 'Redigerades', 'reset': 'Återställdes'}[decision['action']] }"
                )
                st.caption(decision["note"])
            if item["decision"]:
                reset_note = st.text_input("Motivering till återställning")
                if st.button("Återställ beslut", disabled=not reset_note.strip()):
                    _run_action(lambda: _save(item, "reset", {}, reset_note))


def _latest_valid_preview(runs: list[dict], current_fingerprint: str) -> dict | None:
    for run in runs:
        report: dict = run["report"]
        if report.get("kind") == "sync" and report.get("verified"):
            return None
        if (
            report.get("kind") == "preview"
            and report.get("fingerprint") == current_fingerprint
            and report.get("diff")
        ):
            return report
    return None


def _render_update(runs: list[dict], current_fingerprint: str) -> None:
    st.subheader("3. Uppdatera")
    latest = runs[0]["report"] if runs else {}
    if latest.get("kind") == "sync" and latest.get("verified"):
        st.success("Grafen är uppdaterad och efterkontrollen hittade inga avvikelser.")
    preview = _latest_valid_preview(runs, current_fingerprint)
    if preview:
        for line in preview_summary(preview):
            st.markdown(f"- {line}")
        if st.button(
            "Uppdatera och verifiera grafen", type="primary", use_container_width=True
        ):
            _start(
                "graph-sync",
                {
                    "apply": True,
                    "expected": preview["fingerprint"],
                    "adopt_legacy": preview.get("adopt_legacy", False),
                },
            )
    elif st.button("Förbered uppdatering", type="primary", use_container_width=True):
        _start("graph-sync", {"adopt_legacy": True})


def render_review(root: Path) -> None:
    """Visa en styrd analys-, gransknings- och uppdateringsprocess."""
    from casebook_ui import render_pdf_opener

    render_pdf_opener(root)
    profiles = config.load_all()
    with closing(db.connect()) as conn:
        db.init_schema(conn)
        entries, decisions = db.read_graph_review_snapshot(conn)
        name_rules = db.list_graph_name_rules(conn)
        runs = db.list_graph_review_runs(conn, limit=100)
        suggestions = [
            value
            for value in db.list_graph_review_suggestions(conn)
            if value["status"] == "pending"
        ]
        jobs = [
            dict(job)
            for job in db.list_admin_jobs(conn, limit=20)
            if job["operation"] in _REVIEW_OPERATIONS
        ]
    items = audit_entries(entries, decisions)
    review_count = sum(
        1 for item in items if item["issues"] and (not item["decision"] or item["stale"])
    )
    _render_analysis(profiles, jobs, review_count)
    _render_review(root, entries, decisions, name_rules, items, suggestions)
    _render_update(runs, fingerprint(entries, decisions, name_rules))
    with st.expander("Avancerad felsökning"):
        st.write("Fullständiga jobbloggar och avbrytning finns på Admin-sidan.")
        for job in jobs:
            st.write(
                f"{job['operation']} · {_STATUS_LABELS.get(job['status'], job['status'])} "
                f"· {job['id']}"
            )
        for run in runs:
            st.code(json.dumps(run["report"], ensure_ascii=False, indent=2), language="json")
