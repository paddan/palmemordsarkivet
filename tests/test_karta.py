from karta import (
    build_timestamped_geojson,
    candidate_review_payload,
    candidate_source_payload,
    classify_timeline_observations,
    db_observation_payload,
    legend_person_html,
    map_form_defaults,
    observation_source_payload,
    observation_source_payloads,
    observations_at_coord,
    observations_by_person,
    person_color,
    popup_html,
    search_sources,
    validate_observation,
)


def _obs(**overrides):
    base = {
        "id": 1,
        "person": "Olof Palme",
        "place_name": "Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "time": "21:15",
        "uncertainty": "ca",
        "nr": "2055",
        "sida": 1,
        "note": "Biobesök",
    }
    base.update(overrides)
    return base


def test_person_color_is_stable_and_hex():
    assert person_color("Olof Palme") == person_color("Olof Palme")
    assert person_color("Olof Palme").startswith("#")
    assert len(person_color("Olof Palme")) == 7
    assert person_color("Olof Palme") != person_color("Stig Engström")


def test_validate_observation_accepts_complete_source():
    assert validate_observation(_obs()) == []


def test_validate_observation_accepts_empty_string_time_as_missing():
    assert validate_observation(_obs(time="")) == []


def test_validate_observation_accepts_whitespace_time_as_missing():
    assert validate_observation(_obs(time="   ")) == []


def test_validate_observation_reports_missing_required_fields():
    errors = validate_observation(_obs(person=" ", lat=None, nr="", sida=None))
    assert "person saknas" in errors
    assert "lat/lon saknas" in errors
    assert "källa saknas" in errors


def test_validate_observation_reports_bad_time_and_coordinates():
    errors = validate_observation(_obs(lat=120, lon=18, time="25:99"))
    assert "lat måste ligga mellan -90 och 90" in errors
    assert "time måste vara HH:MM" in errors


def test_observations_by_person_sorts_time_and_unknown_last():
    grouped = observations_by_person(
        [
            _obs(id=2, person="Olof Palme", time="23:21"),
            _obs(id=1, person="Olof Palme", time="21:15"),
            _obs(id=3, person="Olof Palme", time=None),
            _obs(id=4, person="Lisbeth Palme", time="21:15"),
        ]
    )
    assert list(grouped) == ["Lisbeth Palme", "Olof Palme"]
    assert [o["id"] for o in grouped["Olof Palme"]] == [1, 2, 3]


def test_popup_html_escapes_text_and_includes_source():
    html = popup_html(_obs(person="<Olof>", place_name="Grand & Svea"))
    assert "&lt;Olof&gt;" in html
    assert "Grand &amp; Svea" in html
    assert "[Nr 2055, sida 1]" in html
    assert "kl 21:15" in html


def test_legend_person_html_escapes_personnamn():
    html = legend_person_html('Eva <script>alert("x")</script>')
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "■" in html


def test_map_form_defaults_uses_selected_observation_without_prefill():
    defaults = map_form_defaults(_obs(), picked_place=None, latest_click=None, use_latest_click=False)
    assert defaults["place_name"] == "Grand"
    assert defaults["lat"] == 59.34057
    assert defaults["lon"] == 18.06024


def test_map_form_defaults_uses_catalog_place_before_submit():
    defaults = map_form_defaults(
        _obs(place_name="Gamla stan", lat=59.325, lon=18.07),
        picked_place={"name": "Tunnelgatan", "lat": 59.33695, "lon": 18.06324},
        latest_click=None,
        use_latest_click=False,
    )
    assert defaults["place_name"] == "Tunnelgatan"
    assert defaults["lat"] == 59.33695
    assert defaults["lon"] == 18.06324


def test_map_form_defaults_lets_latest_click_override_coordinates():
    defaults = map_form_defaults(
        _obs(place_name="Tunnelgatan", lat=59.33695, lon=18.06324),
        picked_place={"name": "Tunnelgatan", "lat": 59.33695, "lon": 18.06324},
        latest_click={"lat": 59.34, "lng": 18.08},
        use_latest_click=True,
    )
    assert defaults["place_name"] == "Tunnelgatan"
    assert defaults["lat"] == 59.34
    assert defaults["lon"] == 18.08


def test_build_timestamped_geojson_points_and_lines():
    data = build_timestamped_geojson(
        [
            _obs(id=1, person="Olof Palme", time="21:15", lat=59.34057, lon=18.06024),
            _obs(id=2, person="Olof Palme", time="23:21", lat=59.33695, lon=18.06324),
            _obs(id=3, person="Stig Engström", time=None, lat=59.3374, lon=18.0632),
        ]
    )

    features = data["features"]
    points = [f for f in features if f["geometry"]["type"] == "Point"]
    lines = [f for f in features if f["geometry"]["type"] == "LineString"]

    assert data["type"] == "FeatureCollection"
    assert len(points) == 2
    assert len(lines) == 1
    assert points[0]["geometry"]["coordinates"] == [18.06024, 59.34057]
    assert points[0]["properties"]["times"] == ["1986-02-28T21:15:00"]
    assert lines[0]["properties"]["times"] == [
        "1986-02-28T21:15:00",
        "1986-02-28T23:21:00",
    ]


def test_build_timestamped_geojson_rolls_after_midnight_to_next_day():
    # Tider efter midnatt (t.ex. 02:19) hör till dygnet efter mordkvällen och
    # ska sorteras efter 23:xx, inte före.
    data = build_timestamped_geojson(
        [
            _obs(id=1, person="A", time="23:20", lat=59.337, lon=18.063),
            _obs(id=2, person="B", time="02:19", lat=59.339, lon=18.065),
        ]
    )
    times = sorted(f["properties"]["time"] for f in data["features"]
                   if f["geometry"]["type"] == "Point")
    assert times == ["1986-02-28T23:20:00", "1986-03-01T02:19:00"]


def test_build_timestamped_geojson_orders_same_person_across_midnight():
    data = build_timestamped_geojson(
        [
            _obs(id=1, person="A", time="23:20", lat=59.337, lon=18.063),
            _obs(id=2, person="A", time="02:19", lat=59.339, lon=18.065),
        ]
    )
    line = next(f for f in data["features"] if f["geometry"]["type"] == "LineString")

    assert line["properties"]["times"] == [
        "1986-02-28T23:20:00",
        "1986-03-01T02:19:00",
    ]
    assert line["geometry"]["coordinates"] == [
        [18.063, 59.337],
        [18.065, 59.339],
    ]


def test_classify_timeline_observations_separates_missing_time_from_invalid():
    classified = classify_timeline_observations(
        [
            _obs(id=1, time="21:15"),
            _obs(id=2, time=""),
            _obs(id=3, time="25:61"),
            _obs(id=4, nr="", sida=None),
        ]
    )

    assert [obs["id"] for obs in classified["valid_timed"]] == [1]
    assert [obs["id"] for obs in classified["missing_time"]] == [2]
    assert [obs["id"] for obs in classified["invalid"]] == [3, 4]


def test_observation_source_payload_requires_nr_and_sida():
    assert observation_source_payload(_obs(nr="", sida=1)) is None
    assert observation_source_payload(_obs(nr="2055", sida=None)) is None
    assert observation_source_payload(_obs(nr="2055", sida=12), source_stem="2055 — Grandbesökare") == {
        "source": "2055 — Grandbesökare.txt",
        "page": 12,
        "nr": "2055",
        "title": "Grand",
    }


def test_observation_source_payload_requires_explicit_source_stem():
    assert observation_source_payload(_obs(nr="2055", sida=12)) is None


def test_observation_source_payload_does_not_duplicate_txt_suffix():
    assert observation_source_payload(
        _obs(nr="2055", sida=12),
        source_stem="2055 — Grandbesökare.txt",
    ) == {
        "source": "2055 — Grandbesökare.txt",
        "page": 12,
        "nr": "2055",
        "title": "Grand",
    }


def test_observation_source_payloads_builds_multiple_cards_from_realistic_stems():
    payloads = observation_source_payloads(
        _obs(nr="2055", sida=7),
        source_stems=[
            "2055 — Grandbesökare",
            "2055 — Grandbesökare.txt",
            "2055 — Förhör med vittne",
        ],
    )

    assert payloads == [
        {
            "source": "2055 — Grandbesökare.txt",
            "page": 7,
            "nr": "2055",
            "title": "Grand",
        },
        {
            "source": "2055 — Förhör med vittne.txt",
            "page": 7,
            "nr": "2055",
            "title": "Grand",
        },
    ]


def test_db_observation_payload_normalizes_blank_time_to_none():
    payload = _obs(time="   ")

    normalized = db_observation_payload(payload)

    assert normalized["time"] is None
    assert payload["time"] == "   "


def test_db_observation_payload_keeps_valid_time_unchanged():
    payload = _obs(time="21:15")

    normalized = db_observation_payload(payload)

    assert normalized["time"] == "21:15"


def test_candidate_source_payload_uses_exact_source_stem():
    candidate = {
        "pdf_stem": "2055 — Grandbesökare",
        "nr": "2055",
        "sida": 3,
        "person": "Olof Palme",
        "raw_place": "Grand",
    }

    assert candidate_source_payload(candidate, source_stem="2055 — Grandbesökare") == {
        "source": "2055 — Grandbesökare.txt",
        "page": 3,
        "nr": "2055",
        "title": "Olof Palme vid Grand",
    }


def test_search_sources_filters_by_nr_or_title_and_caps():
    index = [
        ("100", "100 — Skandiaförhör"),
        ("2055", "2055 — Grandbesökare"),
        ("300", "300 — Tunnelgatan"),
    ]
    assert search_sources(index, "grand") == [("2055", "2055 — Grandbesökare")]
    assert search_sources(index, "2055") == [("2055", "2055 — Grandbesökare")]
    assert {nr for nr, _ in search_sources(index, "")} == {"100", "2055", "300"}
    assert len(search_sources(index, "", limit=2)) == 2


def test_observations_at_coord_matches_within_tolerance():
    obs = [
        _obs(id=1, person="A", lat=59.337, lon=18.063),
        _obs(id=2, person="B", lat=59.33700000004, lon=18.06300000004),
        _obs(id=3, person="C", lat=59.340, lon=18.060),
    ]
    hits = observations_at_coord(obs, 59.337, 18.063)
    assert {o["id"] for o in hits} == {1, 2}
    assert observations_at_coord(obs, 59.500, 18.000) == []


def test_candidate_source_payload_requires_source_and_page():
    assert candidate_source_payload({"nr": "2055", "sida": 1}, source_stem=None) is None
    assert candidate_source_payload({"nr": "2055", "sida": None}, source_stem="2055 — X") is None


def test_candidate_review_payload_does_not_save_default_coord_for_unknown_place():
    candidate = {"lat": None, "lon": None, "place_match": "none"}
    payload = {
        "person": "Okänt vittne",
        "place_name": "Okänd gränd",
        "lat": 59.33695,
        "lon": 18.06324,
        "time": "23:21",
        "uncertainty": "",
        "note": "Citat ur sidan.",
    }

    normalized = candidate_review_payload(
        payload,
        candidate,
        default_lat=59.33695,
        default_lon=18.06324,
    )

    assert normalized["lat"] is None
    assert normalized["lon"] is None


def test_candidate_review_payload_keeps_explicit_coord_for_unknown_place():
    candidate = {"lat": None, "lon": None, "place_match": "none"}
    payload = {
        "person": "Okänt vittne",
        "place_name": "Okänd gränd",
        "lat": 59.34000,
        "lon": 18.06000,
        "time": "23:21",
        "uncertainty": "",
        "note": "Citat ur sidan.",
    }

    normalized = candidate_review_payload(
        payload,
        candidate,
        default_lat=59.33695,
        default_lon=18.06324,
    )

    assert normalized["lat"] == 59.34000
    assert normalized["lon"] == 18.06000
