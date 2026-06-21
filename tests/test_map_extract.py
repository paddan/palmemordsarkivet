from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from map_extract import (  # noqa: E402
    build_place_index,
    candidate_payload,
    match_place,
    normalize_observation_time,
    parse_map_observation_extraction,
)


def _places():
    return build_place_index([
        {"name": "Biografen Grand", "lat": 59.34057, "lon": 18.06024},
        {"name": "Mordplatsen / Dekorima", "lat": 59.33695, "lon": 18.06324},
    ])


def test_parse_map_observation_extraction_accepts_json_object():
    raw = """
    {"observationer": [{
      "person": "Olof Palme",
      "plats": "Grand",
      "tid": "ca 21.15",
      "citat": "Olof Palme kom till Grand omkring kl 21.15.",
      "notering": "ankomst",
      "confidence": "high"
    }]}
    """

    observations = parse_map_observation_extraction(raw)

    assert observations == [{
        "person": "Olof Palme",
        "plats": "Grand",
        "tid": "ca 21.15",
        "citat": "Olof Palme kom till Grand omkring kl 21.15.",
        "notering": "ankomst",
        "confidence": "high",
    }]


def test_parse_map_observation_extraction_filters_incomplete_rows():
    raw = """
    ```json
    {"observationer": [
      {"person": "Olof Palme", "plats": "Grand", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "", "plats": "Grand", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "Olof Palme", "plats": "", "tid": "21:15", "citat": "rad", "confidence": "medium"},
      {"person": "Olof Palme", "plats": "Grand", "tid": "21:15", "citat": "", "confidence": "medium"}
    ]}
    ```
    """

    observations = parse_map_observation_extraction(raw)

    assert len(observations) == 1
    assert observations[0]["person"] == "Olof Palme"


def test_normalize_observation_time_handles_common_forms():
    assert normalize_observation_time("21:15") == ("21:15", None)
    assert normalize_observation_time("21.15") == ("21:15", None)
    assert normalize_observation_time("ca 21.15") == ("21:15", "ca")
    assert normalize_observation_time("omkring kl 23 21") == ("23:21", "omkring")
    assert normalize_observation_time("efter midnatt") == (None, "efter midnatt")
    assert normalize_observation_time("") == (None, None)


def test_match_place_exact_and_fuzzy():
    places = _places()

    assert match_place("Biografen Grand", places) == {
        "place_name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "place_match": "exact",
    }
    assert match_place("Grand", places)["place_name"] == "Biografen Grand"
    assert match_place("Dekorima", places)["place_name"] == "Mordplatsen / Dekorima"
    assert match_place("okänd gränd", places) == {
        "place_name": None,
        "lat": None,
        "lon": None,
        "place_match": "none",
    }


def test_candidate_payload_maps_source_place_and_time():
    payload = candidate_payload(
        {
            "person": "Olof Palme",
            "plats": "Grand",
            "tid": "ca 21.15",
            "citat": "Olof Palme kom till Grand omkring kl 21.15.",
            "notering": "ankomst",
            "confidence": "high",
        },
        pdf_stem="2055 — Grandbesökare",
        page_num=3,
        nr="2055",
        model="test-model",
        place_index=_places(),
    )

    assert payload == {
        "pdf_stem": "2055 — Grandbesökare",
        "page_num": 3,
        "person": "Olof Palme",
        "raw_place": "Grand",
        "place_name": "Biografen Grand",
        "lat": 59.34057,
        "lon": 18.06024,
        "time": "21:15",
        "uncertainty": "ca",
        "nr": "2055",
        "sida": 3,
        "quote": "Olof Palme kom till Grand omkring kl 21.15.",
        "note": "ankomst",
        "confidence": "high",
        "place_match": "fuzzy",
        "model": "test-model",
    }
