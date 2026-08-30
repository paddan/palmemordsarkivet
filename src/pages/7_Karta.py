"""Streamlit-sida: karta över källhänvisade observationer mordkvällen."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

try:
    import folium
    from folium.plugins import TimestampedGeoJson
    from streamlit_folium import st_folium
except ImportError:  # pragma: no cover - optional web extra
    st.set_page_config(page_title="Palmemordsarkivet — Karta", layout="wide")
    st.title("Karta")
    st.warning("Installera kartstödet med `pip install -e .[web]`.")
    st.stop()

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import casebook_ui as _casebook_ui  # noqa: E402
import citations as _citations  # noqa: E402
import db as _state_db  # noqa: E402
import karta as _karta  # noqa: E402
import map_extract as _map_extract  # noqa: E402

SEED_DIR = ROOT / "data" / "karta"
DEFAULT_CENTER = [59.33695, 18.06324]
NEW_OBSERVATION_VALUE = "__new_observation__"


@st.cache_data(show_spinner=False)
def _load_seed_file(name: str) -> list[dict]:
    path = SEED_DIR / name
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _seed_if_needed(conn) -> int:
    count: int = _state_db.seed_map_data_if_empty(
        conn,
        _load_seed_file("platser.json"),
        _load_seed_file("rorelser.json"),
    )
    return count


@st.cache_resource(show_spinner=False)
def _nr_to_pdf_mapping(root: str) -> dict[str, Path]:
    mapping: dict[str, Path] = _citations.build_nr_to_pdf(Path(root))
    return mapping


@st.cache_data(show_spinner=False)
def _source_index(root: str) -> list[tuple[str, str]]:
    """(nr, etikett) för alla arkivdokument, sorterat på etikett för sökval."""
    mapping = _nr_to_pdf_mapping(root)
    return sorted(
        ((nr, pdf.stem) for nr, pdf in mapping.items()),
        key=lambda item: item[1].casefold(),
    )


def _sources_for_observation(obs: dict) -> list[dict]:
    nr = str(obs.get("nr") or "").strip()
    sida = obs.get("sida")
    if not nr or sida in (None, ""):
        return []
    mapping = _nr_to_pdf_mapping(str(ROOT))
    stems = [pdf.stem for pdf in _citations.resolve_nr_all(nr, mapping)]
    payloads: list[dict] = _karta.observation_source_payloads(obs, source_stems=stems)
    return payloads


def _sources_for_candidate(candidate: dict) -> list[dict]:
    nr = str(candidate.get("nr") or "").strip()
    sida = candidate.get("sida")
    if not nr or sida in (None, ""):
        return []
    mapping = _nr_to_pdf_mapping(str(ROOT))
    stems = [pdf.stem for pdf in _citations.resolve_nr_all(nr, mapping)]
    payloads = []
    for stem in stems:
        payload = _karta.candidate_source_payload(candidate, source_stem=stem)
        if payload:
            payloads.append(payload)
    return payloads


def _add_place_markers(map_obj: folium.Map, places: list[dict]) -> None:
    for place in places:
        folium.CircleMarker(
            location=[place["lat"], place["lon"]],
            radius=4,
            color="#555555",
            fill=True,
            fill_color="#555555",
            fill_opacity=0.55,
            tooltip=place["name"],
        ).add_to(map_obj)


def _add_observation_markers(map_obj: folium.Map, observations: list[dict]) -> None:
    """Statiska, alltid synliga markörer per observation (färg per person)."""
    for obs in observations:
        color = _karta.person_color(str(obs.get("person") or ""))
        folium.CircleMarker(
            location=[float(obs["lat"]), float(obs["lon"])],
            radius=7,
            color=color,
            weight=2,
            fill=True,
            fill_color=color,
            fill_opacity=0.9,
            popup=folium.Popup(_karta.popup_html(obs), max_width=320),
            tooltip=f"{obs.get('person') or 'Okänd'} · {obs.get('time') or 'okänd tid'}",
        ).add_to(map_obj)


def _add_person_paths(map_obj: folium.Map, observations: list[dict]) -> None:
    """Rörelsespår per person med minst två tidsatta observationer."""
    for person, items in _karta.observations_by_person(observations).items():
        coords = [
            [float(o["lat"]), float(o["lon"])]
            for o in items
            if str(o.get("time") or "").strip()
        ]
        if len(coords) >= 2:
            folium.PolyLine(
                coords,
                color=_karta.person_color(person),
                weight=3,
                opacity=0.6,
            ).add_to(map_obj)


def _build_map(
    observations: list[dict], places: list[dict], *, animate: bool
) -> folium.Map:
    map_obj = folium.Map(location=DEFAULT_CENTER, zoom_start=16, tiles="OpenStreetMap")
    _add_place_markers(map_obj, places)
    classified = _karta.classify_timeline_observations(observations)
    invalid = classified["invalid"]
    missing_time = classified["missing_time"]
    # Alla observationer med giltig koordinat (även tidlösa) ritas som statiska
    # markörer så att valda personer alltid syns oavsett tidslinjens läge.
    plottable = classified["valid_timed"] + missing_time
    if invalid:
        st.warning(
            f"{len(invalid)} observationer saknar giltig koordinat eller källa "
            "och kan inte visas på kartan."
        )
    if missing_time:
        st.info(
            f"{len(missing_time)} observationer saknar tid — de visas som markörer "
            "men inte i den animerade tidslinjen."
        )
    _add_observation_markers(map_obj, plottable)
    _add_person_paths(map_obj, plottable)
    if animate:
        geojson = _karta.build_timestamped_geojson(observations)
        if geojson["features"]:
            TimestampedGeoJson(
                geojson,
                period="PT1M",
                add_last_point=True,
                auto_play=False,
                loop=False,
                max_speed=10,
                loop_button=True,
                date_options="HH:mm",
                time_slider_drag_update=True,
            ).add_to(map_obj)
        else:
            st.info("Ingen tidsatt rörelse att animera ännu.")
    return map_obj


def _sync_form_state(selected: dict | None, default_place_choice: str) -> None:
    """Återställ formuläret när användaren byter vald observation."""
    if selected:
        signature = selected.get("id")
    else:
        signature = f"{NEW_OBSERVATION_VALUE}:{st.session_state.get('karta_new_form_revision', 0)}"
    if st.session_state.get("karta_form_signature") == signature:
        return

    defaults = _karta.map_form_defaults(
        selected,
        picked_place=None,
        latest_click=None,
        use_latest_click=False,
    )
    st.session_state["karta_form_signature"] = signature
    st.session_state["karta_form_target_id"] = selected.get("id") if selected else None
    st.session_state["karta_form_person"] = selected.get("person", "") if selected else ""
    st.session_state["karta_form_place_name"] = defaults["place_name"]
    st.session_state["karta_form_lat"] = defaults["lat"]
    st.session_state["karta_form_lon"] = defaults["lon"]
    st.session_state["karta_form_time"] = selected.get("time") or "" if selected else ""
    st.session_state["karta_form_uncertainty"] = (
        selected.get("uncertainty") or "" if selected else ""
    )
    st.session_state["karta_form_nr"] = selected.get("nr") or "" if selected else ""
    st.session_state["karta_form_sida"] = (
        int(selected["sida"]) if selected and selected.get("sida") else 1
    )
    st.session_state["karta_form_note"] = selected.get("note") or "" if selected else ""
    st.session_state["karta_place_choice"] = default_place_choice
    st.session_state["karta_place_choice_applied"] = default_place_choice


st.set_page_config(page_title="Palmemordsarkivet — Karta", layout="wide")
st.title("Karta")
st.caption("Källhänvisade observationer och rörelser runt mordkvällen 28 februari 1986.")

conn = _casebook_ui.state_conn()
seeded = _seed_if_needed(conn)
if seeded:
    st.toast(f"Seedade {seeded} kartposter")

places = _state_db.list_map_places(conn)
observations = _state_db.list_map_observations(conn)
people = sorted({obs["person"] for obs in observations})

with st.sidebar:
    st.header("Filter")
    selected_people = st.multiselect("Personer", people, default=people)
    show_places = st.toggle("Visa platskatalog", value=True)
    animate_timeline = st.toggle(
        "Animera tidslinje",
        value=False,
        help="Av: alla valda observationer visas som markörer. "
        "På: spela upp dem i tidsordning med tidsreglaget.",
    )

filtered = [
    obs for obs in observations
    if not selected_people or obs["person"] in selected_people
]
map_places = places if show_places else []

# Aktuellt val (sätts av listan längre ned eller av ett markörklick på kartan).
_selected_id = st.session_state.get("karta_selected_observation")
current_selected = next((o for o in observations if o["id"] == _selected_id), None)

left, right = st.columns([1, 3])
with left:
    st.subheader("Personer")
    if people:
        for person in people:
            st.markdown(_karta.legend_person_html(person), unsafe_allow_html=True)
    else:
        st.info("Inga observationer ännu. Lägg till en källhänvisad observation nedan.")
    st.caption(f"{len(filtered)} visade observationer")

    st.divider()
    st.subheader("Flytta / tidssätt")
    move_mode = st.toggle(
        "Flytta-läge",
        value=False,
        help="Klicka på en markör för att välja en observation. När flytta-läge "
        "är på flyttas den valda observationen dit du klickar på kartan.",
    )
    if current_selected:
        st.caption(
            f"Vald: **{current_selected['person']}** · "
            f"{current_selected.get('place_name') or 'okänd plats'} · "
            f"{current_selected.get('time') or 'utan tid'}"
        )
        quick_time = st.text_input(
            "Tid (HH:MM)",
            value=current_selected.get("time") or "",
            key=f"karta_quick_time_{current_selected['id']}",
        )
        if st.button("Uppdatera tid", use_container_width=True, key="karta_quick_time_btn"):
            try:
                _state_db.update_map_observation(
                    conn, current_selected["id"], time=quick_time or None
                )
                st.toast("Tid uppdaterad")
                st.rerun()
            except ValueError as exc:
                st.error(f"Ogiltig tid: {exc}")
    else:
        st.caption(
            "Klicka en markör (eller välj i listan nedan) för att flytta eller "
            "tidssätta en observation."
        )

with right:
    result = st_folium(
        _build_map(filtered, map_places, animate=animate_timeline),
        height=650,
        use_container_width=True,
        key="karta_folium",
    )
    # Markörklick → välj observationen (om markörerna inte ligger ovanpå varandra).
    obj_click = result.get("last_object_clicked") if result else None
    if obj_click and obj_click != st.session_state.get("karta_prev_obj_click"):
        st.session_state["karta_prev_obj_click"] = obj_click
        matches = _karta.observations_at_coord(
            observations, obj_click["lat"], obj_click["lng"]
        )
        if len(matches) == 1:
            st.session_state["karta_selected_observation"] = matches[0]["id"]
            st.rerun()
        elif len(matches) > 1:
            st.info(
                f"{len(matches)} observationer ligger på samma punkt — välj i listan nedan."
            )

    # Kartklick → flytta vald observation (flytta-läge) eller spara som senaste klick.
    map_click = result.get("last_clicked") if result else None
    if map_click and map_click != st.session_state.get("karta_prev_map_click"):
        st.session_state["karta_prev_map_click"] = map_click
        st.session_state["karta_last_clicked"] = map_click
        if move_mode and current_selected:
            _state_db.update_map_observation(
                conn,
                current_selected["id"],
                lat=float(map_click["lat"]),
                lon=float(map_click["lng"]),
            )
            st.toast(f"Flyttade {current_selected['person']} hit")
            st.rerun()
    last_click = st.session_state.get("karta_last_clicked")
    if last_click:
        st.caption(f"Senaste kartklick: {last_click['lat']:.5f}, {last_click['lng']:.5f}")

st.subheader("Redigera observationer")

if observations:
    observation_options = [NEW_OBSERVATION_VALUE] + [obs["id"] for obs in observations]
    observation_labels = {
        NEW_OBSERVATION_VALUE: "Ny observation",
        **{
            obs["id"]: (
                f"{obs['id']} · {obs['person']} · {obs.get('time') or 'utan tid'} · "
                f"{obs.get('place_name') or 'okänd plats'}"
            )
            for obs in observations
        },
    }
    if st.session_state.get("karta_selected_observation") not in observation_options:
        st.session_state["karta_selected_observation"] = NEW_OBSERVATION_VALUE
    selected_observation = st.selectbox(
        "Observation",
        observation_options,
        key="karta_selected_observation",
        format_func=lambda value: observation_labels[value],
    )
    selected = next(
        (obs for obs in observations if obs["id"] == selected_observation),
        None,
    )
else:
    selected = None
    st.session_state["karta_selected_observation"] = NEW_OBSERVATION_VALUE
    st.info("Databasen har inga observationer ännu.")

selected_for_form = selected

place_names = ["(fritt)"] + [place["name"] for place in places]
place_lookup = {place["name"]: place for place in places}
default_place = selected_for_form.get("place_name") if selected_for_form else ""
default_place_choice = default_place if default_place in place_lookup else "(fritt)"
_sync_form_state(selected_for_form, default_place_choice)

st.caption("Förifyll formuläret med en katalogplats eller senaste kartklick innan du sparar.")
if selected_for_form is None:
    st.info("Formuläret är återställt för en ny observation.")
prefill_left, prefill_right = st.columns([2, 1])
with prefill_left:
    place_choice = st.selectbox("Plats ur katalog", place_names, key="karta_place_choice")
with prefill_right:
    latest_click = st.session_state.get("karta_last_clicked")
    use_click_clicked = st.button(
        "Använd senaste kartklick",
        use_container_width=True,
        disabled=latest_click is None,
    )

picked_place = place_lookup.get(place_choice)
if place_choice != st.session_state.get("karta_place_choice_applied"):
    defaults = _karta.map_form_defaults(
        selected_for_form,
        picked_place=picked_place,
        latest_click=None,
        use_latest_click=False,
    )
    st.session_state["karta_form_place_name"] = defaults["place_name"]
    st.session_state["karta_form_lat"] = defaults["lat"]
    st.session_state["karta_form_lon"] = defaults["lon"]
    st.session_state["karta_place_choice_applied"] = place_choice
    st.rerun()

if use_click_clicked and latest_click:
    defaults = _karta.map_form_defaults(
        selected_for_form,
        picked_place=picked_place,
        latest_click=latest_click,
        use_latest_click=True,
    )
    st.session_state["karta_form_lat"] = defaults["lat"]
    st.session_state["karta_form_lon"] = defaults["lon"]
    st.rerun()

# Källväljare: sök bland arkivets dokument och fyll i Nr (annars blir fältet blint).
st.markdown("**Källa**")
source_query = st.text_input(
    "Sök källa (nr eller titel)",
    key="karta_source_query",
    placeholder="t.ex. Grand, Engström eller 2055…",
)
source_matches = _karta.search_sources(_source_index(str(ROOT)), source_query)
current_nr = str(st.session_state.get("karta_form_nr") or "").strip()
keep_label = f"(behåll nuvarande: {current_nr or 'ingen'})"
label_to_nr = {label: nr for nr, label in source_matches}
src_left, src_right = st.columns([3, 1])
with src_left:
    source_pick = st.selectbox(
        f"Välj källa ({len(source_matches)} träffar)",
        [keep_label] + [label for _nr, label in source_matches],
        key="karta_source_pick",
    )
with src_right:
    use_source_clicked = st.button("Använd källa", use_container_width=True)
if use_source_clicked and source_pick != keep_label:
    st.session_state["karta_form_nr"] = label_to_nr[source_pick]
    st.toast(f"Källa satt: Nr {label_to_nr[source_pick]}")
    st.rerun()

with st.form("map_observation_form", clear_on_submit=False):
    cols = st.columns(3)
    with cols[0]:
        person = st.text_input("Person", key="karta_form_person")
        place_name = st.text_input("Platsnamn", key="karta_form_place_name")
    with cols[1]:
        lat = st.number_input("Lat", key="karta_form_lat", format="%.6f")
        lon = st.number_input("Lon", key="karta_form_lon", format="%.6f")
    with cols[2]:
        time = st.text_input("Tid (HH:MM)", key="karta_form_time")
        uncertainty = st.text_input("Osäkerhet", key="karta_form_uncertainty")
        nr = st.text_input("Nr", key="karta_form_nr")
        sida = st.number_input("Sida", min_value=1, step=1, key="karta_form_sida")
    note = st.text_area("Notering", key="karta_form_note")

    save, new, delete = st.columns(3)
    save_clicked = save.form_submit_button("Spara")
    new_clicked = new.form_submit_button("Lägg till ny")
    delete_clicked = delete.form_submit_button("Ta bort vald")

payload = {
    "person": person,
    "place_name": place_name,
    "lat": lat,
    "lon": lon,
    "time": time,
    "uncertainty": uncertainty,
    "nr": nr,
    "sida": int(sida),
    "note": note,
}
form_target_id = st.session_state.get("karta_form_target_id")
errors = _karta.validate_observation(payload)
if (save_clicked or new_clicked) and errors:
    st.error("Kan inte spara: " + ", ".join(errors))
elif save_clicked and form_target_id:
    _state_db.update_map_observation(conn, form_target_id, **_karta.db_observation_payload(payload))
    st.toast("Observation uppdaterad")
    st.rerun()
elif save_clicked:
    st.info("Formuläret är i ny-läge. Välj en observation att uppdatera eller använd Lägg till ny.")
elif new_clicked:
    _state_db.record_map_observation(conn, **_karta.db_observation_payload(payload))
    st.session_state["karta_selected_observation"] = NEW_OBSERVATION_VALUE
    st.session_state["karta_new_form_revision"] = (
        st.session_state.get("karta_new_form_revision", 0) + 1
    )
    st.toast("Observation tillagd")
    st.rerun()
elif delete_clicked and selected:
    _state_db.delete_map_observation(conn, selected["id"])
    st.toast("Observation borttagen")
    st.rerun()

if selected_for_form:
    sources = _sources_for_observation(selected_for_form)
    if sources:
        st.subheader("Källa för vald observation")
        _casebook_ui.render_source_cards(
            ROOT,
            sources,
            conn,
            key_prefix=f"karta_source_{selected_for_form['id']}",
        )
    elif (
        str(selected_for_form.get("nr") or "").strip()
        and selected_for_form.get("sida") not in (None, "")
    ):
        st.caption(
            "Källhänvisning finns, men ingen lokal arkivfil matchade detta nr ännu."
        )

st.subheader("Granska extraherade kartförslag")
pending_candidates = _state_db.list_map_observation_candidates(conn, status="pending", limit=50)
# Platsindex ur katalogen så koordinatlösa kandidater kan föreslås rätt plats
# i stället för att tyst hamna på mordplatsens defaultkoordinat vid godkännande.
_candidate_place_index = _map_extract.build_place_index(places)
if not pending_candidates:
    st.info("Inga kartförslag väntar på granskning.")
else:
    st.caption(f"{len(pending_candidates)} förslag väntar på granskning")

for candidate in pending_candidates:
    label_time = candidate.get("time") or "utan tid"
    label_place = candidate.get("place_name") or candidate.get("raw_place") or "okänd plats"
    with st.expander(
        f"{candidate['person']} · {label_time} · {label_place} · "
        f"Nr {candidate['nr']}, sida {candidate['sida']}"
    ):
        st.write(candidate.get("quote") or "")
        st.caption(
            f"Matchning: {candidate['place_match']} · "
            f"confidence: {candidate['confidence']} · modell: {candidate['model']}"
        )
        sources = _sources_for_candidate(candidate)
        if sources:
            _casebook_ui.render_source_cards(
                ROOT,
                sources,
                conn,
                key_prefix=f"karta_candidate_source_{candidate['id']}",
            )
        else:
            st.caption("Ingen lokal arkivfil matchade kandidatens nr.")

        # Föreslå koordinat för kandidater som saknar en: matcha platsen mot
        # katalogen. Saknas både koordinat och match varnar vi i stället för att
        # tyst publicera punkten på mordplatsen.
        if candidate["lat"] is not None:
            default_lat, default_lon = float(candidate["lat"]), float(candidate["lon"])
        else:
            _m = _map_extract.match_place(
                candidate.get("place_name") or candidate.get("raw_place") or "",
                _candidate_place_index,
            )
            if _m["place_match"] != "none":
                default_lat, default_lon = _m["lat"], _m["lon"]
                st.info(
                    f"📍 Koordinat föreslagen från katalogen "
                    f"({_m['place_name']}, {_m['place_match']}) — kontrollera innan godkännande."
                )
            else:
                default_lat, default_lon = DEFAULT_CENTER
                st.warning(
                    "⚠️ Ingen koordinat hittad för platsen. Sätt rätt koordinat "
                    "innan du godkänner — annars hamnar punkten på mordplatsen."
                )

        with st.form(f"karta_candidate_form_{candidate['id']}"):
            cols = st.columns(3)
            with cols[0]:
                person = st.text_input("Person", value=candidate["person"])
                place_name = st.text_input(
                    "Platsnamn",
                    value=candidate.get("place_name") or candidate.get("raw_place") or "",
                )
            with cols[1]:
                lat = st.number_input("Lat", value=float(default_lat), format="%.6f")
                lon = st.number_input("Lon", value=float(default_lon), format="%.6f")
            with cols[2]:
                time = st.text_input("Tid (HH:MM)", value=candidate.get("time") or "")
                uncertainty = st.text_input("Osäkerhet", value=candidate.get("uncertainty") or "")
            note = st.text_area(
                "Notering",
                value=candidate.get("note") or candidate.get("quote") or "",
            )
            save, approve, reject = st.columns(3)
            save_clicked = save.form_submit_button("Spara ändringar")
            approve_clicked = approve.form_submit_button("Godkänn till kartan")
            reject_clicked = reject.form_submit_button("Avvisa")

        candidate_updates = _karta.candidate_review_payload(
            {
                "person": person,
                "place_name": place_name,
                "lat": lat,
                "lon": lon,
                "time": time,
                "uncertainty": uncertainty,
                "note": note,
            },
            candidate,
            default_lat=DEFAULT_CENTER[0],
            default_lon=DEFAULT_CENTER[1],
        )
        if save_clicked:
            _state_db.update_map_observation_candidate(conn, candidate["id"], **candidate_updates)
            st.toast("Kartförslaget uppdaterat")
            st.rerun()
        if approve_clicked:
            _state_db.update_map_observation_candidate(conn, candidate["id"], **candidate_updates)
            try:
                _state_db.approve_map_observation_candidate(conn, candidate["id"])
            except ValueError as exc:
                st.error(f"Kan inte godkänna: {exc}")
            else:
                st.toast("Kartförslaget godkänt")
                st.rerun()
        if reject_clicked:
            _state_db.reject_map_observation_candidate(conn, candidate["id"])
            st.toast("Kartförslaget avvisat")
            st.rerun()
