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
    return _state_db.seed_map_data_if_empty(
        conn,
        _load_seed_file("platser.json"),
        _load_seed_file("rorelser.json"),
    )


@st.cache_resource(show_spinner=False)
def _nr_to_pdf_mapping(root: str) -> dict[str, Path]:
    return _citations.build_nr_to_pdf(Path(root))


def _sources_for_observation(obs: dict) -> list[dict]:
    nr = str(obs.get("nr") or "").strip()
    sida = obs.get("sida")
    if not nr or sida in (None, ""):
        return []
    mapping = _nr_to_pdf_mapping(str(ROOT))
    stems = [pdf.stem for pdf in _citations.resolve_nr_all(nr, mapping)]
    return _karta.observation_source_payloads(obs, source_stems=stems)


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


def _build_map(observations: list[dict], places: list[dict]) -> folium.Map:
    map_obj = folium.Map(location=DEFAULT_CENTER, zoom_start=16, tiles="OpenStreetMap")
    _add_place_markers(map_obj, places)
    classified = _karta.classify_timeline_observations(observations)
    invalid = classified["invalid"]
    missing_time = classified["missing_time"]
    if invalid:
        st.warning(
            f"{len(invalid)} observationer saknar giltig tid, koordinat eller källa "
            "och visas inte i tidslinjen."
        )
    if missing_time:
        st.info(
            f"{len(missing_time)} observationer saknar tid och visas därför inte i tidslinjen."
        )
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

filtered = [
    obs for obs in observations
    if not selected_people or obs["person"] in selected_people
]
map_places = places if show_places else []

left, right = st.columns([1, 3])
with left:
    st.subheader("Personer")
    if people:
        for person in people:
            st.markdown(_karta.legend_person_html(person), unsafe_allow_html=True)
    else:
        st.info("Inga observationer ännu. Lägg till en källhänvisad observation nedan.")
    st.caption(f"{len(filtered)} visade observationer")

with right:
    result = st_folium(
        _build_map(filtered, map_places),
        height=650,
        use_container_width=True,
        key="karta_folium",
    )
    clicked = result.get("last_clicked") if result else None
    if clicked:
        st.session_state["karta_last_clicked"] = clicked
        st.caption(f"Senaste kartklick: {clicked['lat']:.5f}, {clicked['lng']:.5f}")

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
