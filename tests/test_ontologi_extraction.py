"""Tolkning och kontroll av LLM-svaret för mordnattens iakttagelser."""

from __future__ import annotations

import json

import pytest

from ontologi.config import SIGNALEMENT_TYPER, load_config
from ontologi.extraction import (
    PLATSHALLARE_RE,
    SYSTEM_PROMPT,
    VITTNESKATEGORIER,
    build_prompt,
    normalize_text,
    parse_response,
    text_hash,
)

PAGE = (
    "Vittnet Anna Berg såg kl 23.21 en man i mörk rock, ca 180-185 cm, springa in på "
    "Tunnelgatan. [MASKAD] uppgav att gärningsmannen var cirka 40 år."
)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def obs(**over):
    o = {
        "tid_text": "kl 23.21",
        "tid_fran": "23:21",
        "tid_till": "23:21",
        "plats": "Tunnelgatan",
        "handelse_id": "skotten",
        "vittne": {"namn": "Anna Berg", "maskad": False, "kategori": "namn", "roll": "vittne"},
        "observerade": [
            {
                "beskrivning": "man i mörk rock",
                "signalement": [
                    {"typ": "längd", "varde_text": "ca 180-185 cm", "min": 180, "max": 185, "enhet": None}
                ],
                "kan_vara": [],
            }
        ],
        "utdrag": "såg kl 23.21 en man i mörk rock",
        "osakerhet": None,
    }
    o.update(over)
    return o


def parse(cfg, *observationer, page=PAGE):
    return parse_response(json.dumps({"observationer": list(observationer)}), page, cfg)


def test_valid_observation_is_kept_with_index(cfg):
    r = parse(cfg, obs())
    assert len(r.observationer) == 1
    o = r.observationer[0]
    assert o["index"] == 0
    assert (o["tid_fran"], o["tid_till"]) == ("23:21", "23:21")
    assert o["vittne"]["namn"] == "Anna Berg"
    assert o["handelse_id"] == "skotten"


def test_invented_quote_is_dropped(cfg):
    r = parse(cfg, obs(utdrag="Han flydde därifrån med en vit bil"))
    assert r.observationer == []
    assert r.varningar and r.varningar[0].startswith("citat saknas på sidan:")


def test_missing_quote_is_dropped(cfg):
    o = obs()
    del o["utdrag"]
    assert parse(cfg, o).observationer == []


def test_index_counts_only_kept(cfg):
    r = parse(cfg, obs(utdrag="påhittat"), obs())
    assert [o["index"] for o in r.observationer] == [0]


def test_invented_name_becomes_unknown(cfg):
    r = parse(cfg, obs(vittne={"namn": "Bo Ek", "maskad": False, "kategori": "namn", "roll": None}))
    v = r.observationer[0]["vittne"]
    assert v["namn"] is None and v["maskad"] is False
    assert any(w.startswith("namn saknas på sidan:") for w in r.varningar)


@pytest.mark.parametrize("namn", ["[MASKAD]", "[ maskad ]", "NN", "N.N.", "okänd", "okänd person", "X", "XX", "xxx", "Maskad", "MASKAD]", "[MASKAD] [MASKAD]", "Anna Ekberg [MASKAD]", "[MASKAD] Berg", "Okänd kvinna", "okänt", "NN NN", "[maskerad]", "[Redacted]", "okänd man i rock", "Okänd äldre kvinna", "Maskerad", "maskerat", "Redacted", "X X", "x x x"])
def test_placeholder_name_becomes_masked(cfg, namn):
    r = parse(cfg, obs(vittne={"namn": namn, "maskad": False, "kategori": "namn", "roll": None}))
    v = r.observationer[0]["vittne"]
    assert v["maskad"] is True and v["namn"] is None


def test_maskad_flag_wins_over_name(cfg):
    r = parse(cfg, obs(vittne={"namn": "Anna Berg", "maskad": True, "kategori": "namn", "roll": None}))
    v = r.observationer[0]["vittne"]
    assert v["maskad"] is True and v["namn"] is None


def test_placeholder_regex_does_not_match_real_names():
    assert not PLATSHALLARE_RE.match("Xavier Nilsson")
    assert PLATSHALLARE_RE.match("[MASKAD]")


def test_bad_category_becomes_unknown(cfg):
    r = parse(cfg, obs(vittne={"namn": "Anna Berg", "maskad": False, "kategori": "hobby", "roll": None}))
    assert r.observationer[0]["vittne"]["kategori"] == "okänd"


def test_kan_vara_requires_alias_in_quote(cfg):
    page = PAGE
    ok = obs(
        utdrag="uppgav att gärningsmannen var cirka 40 år",
        observerade=[{"beskrivning": "x", "signalement": [], "kan_vara": ["garningsmannen", "olof-palme", "påhittad"]}],
    )
    r = parse(cfg, ok, page=page)
    assert r.observationer[0]["observerade"][0]["kan_vara"] == ["garningsmannen"]
    nej = obs(observerade=[{"beskrivning": "x", "signalement": [], "kan_vara": ["garningsmannen"]}])
    r = parse(cfg, nej)
    assert r.observationer[0]["observerade"][0]["kan_vara"] == []


def test_height_interval_and_reversed_interval(cfg):
    r = parse(cfg, obs())
    s = r.observationer[0]["observerade"][0]["signalement"][0]
    assert (s["min"], s["max"], s["enhet"]) == (180, 185, "cm")
    rev = obs(
        observerade=[
            {
                "beskrivning": "x",
                "signalement": [{"typ": "längd", "varde_text": "t", "min": 190, "max": 180, "enhet": None}],
                "kan_vara": [],
            }
        ]
    )
    r = parse(cfg, rev)
    s = r.observationer[0]["observerade"][0]["signalement"][0]
    assert s["min"] is None and s["max"] is None
    assert any(w.startswith("omvänt intervall:") for w in r.varningar)


def test_signalement_type_and_non_numeric(cfg):
    o = obs(
        observerade=[
            {
                "beskrivning": "x",
                "signalement": [
                    {"typ": "skor", "varde_text": "stövlar", "min": 5, "max": 6, "enhet": "m"},
                    {"typ": "hår", "varde_text": "mörkt", "min": 1, "max": 2, "enhet": "cm"},
                    {"typ": "ålder", "varde_text": "40", "min": "40", "max": 40.5, "enhet": None},
                ],
                "kan_vara": [],
            }
        ]
    )
    sig = parse(cfg, o).observationer[0]["observerade"][0]["signalement"]
    assert sig[0]["typ"] == "övrigt" and sig[0]["min"] is None and sig[0]["enhet"] is None
    assert sig[1]["min"] is None and sig[1]["max"] is None and sig[1]["enhet"] is None
    assert sig[2]["min"] is None and sig[2]["max"] == 40.5 and sig[2]["enhet"] == "år"


def test_unknown_event_and_bad_time(cfg):
    r = parse(cfg, obs(handelse_id="påhittad", tid_fran="kvart över", tid_till="kvart över"))
    o = r.observationer[0]
    assert o["handelse_id"] is None
    assert o["tid_fran"] is None and o["tid_till"] is None
    assert any(w.startswith("okänd händelse:") for w in r.varningar)
    assert any(w.startswith("ogiltig tid:") for w in r.varningar)


def test_single_time_fills_other_and_reversed_order(cfg):
    o = parse(cfg, obs(tid_fran="23:21", tid_till=None)).observationer[0]
    assert (o["tid_fran"], o["tid_till"]) == ("23:21", "23:21")
    r = parse(cfg, obs(tid_fran="23:30", tid_till="23:10"))
    o = r.observationer[0]
    assert o["tid_fran"] is None and o["tid_till"] is None
    assert any(w.startswith("omvänd tid:") for w in r.varningar)


@pytest.mark.parametrize("raw", ["Tyvärr kan jag inte", "{trasig", '{"observationer": 3}', "[]"])
def test_garbage_response_gives_no_observations(cfg, raw):
    r = parse_response(raw, PAGE, cfg)
    assert r.observationer == [] and r.varningar


def test_json_in_code_fence_is_accepted(cfg):
    raw = "```json\n" + json.dumps({"observationer": [obs()]}) + "\n```"
    assert len(parse_response(raw, PAGE, cfg).observationer) == 1


def test_place_is_matched_to_catalog(cfg):
    p = parse(cfg, obs()).observationer[0]["plats"]
    assert p["match"] in ("exact", "fuzzy")
    assert p["lat"] is not None and p["lon"] is not None
    assert p["namn_ra"] == "Tunnelgatan" and p["kartplats_namn"]
    p = parse(cfg, obs(plats=None)).observationer[0]["plats"]
    assert p["match"] == "none" and p["lat"] is None


def test_non_dict_observation_is_skipped(cfg):
    r = parse_response(json.dumps({"observationer": ["x", obs()]}), PAGE, cfg)
    assert len(r.observationer) == 1 and r.varningar


def test_text_hash_ignores_whitespace():
    assert text_hash("a  b\n") == text_hash("A b")
    assert normalize_text(" A\n\tB ") == "a b"


def test_prompt_lists_events_and_key_persons(cfg):
    p = build_prompt(PAGE, cfg)
    assert "skotten" in p and "23:21" in p and "olof-palme" in p and PAGE in p
    assert p.index("skotten") < p.index(PAGE)
    assert "[MASKAD]" in SYSTEM_PROMPT and "300" in SYSTEM_PROMPT


def test_empty_name_is_no_witness_not_masked(cfg):
    for namn in ("", None, "  "):
        r = parse(cfg, obs(vittne={"namn": namn, "maskad": False, "kategori": "namn", "roll": None}))
        v = r.observationer[0]["vittne"]
        assert v["namn"] is None and v["maskad"] is False


@pytest.mark.parametrize("utdrag", ["a", "Han sprang"])
def test_short_quote_is_dropped(cfg, utdrag):
    page = PAGE + " a Han sprang"
    r = parse(cfg, obs(utdrag=utdrag), page=page)
    assert r.observationer == []
    assert r.varningar[0].startswith("citat för kort:")


def test_name_needs_word_boundary_and_length(cfg):
    r = parse(cfg, obs(vittne={"namn": "Ek", "maskad": False, "kategori": "namn", "roll": None}), page=PAGE + " Ekberg")
    v = r.observationer[0]["vittne"]
    assert v["namn"] is None and v["maskad"] is False and r.varningar
    r = parse(cfg, obs(vittne={"namn": "Berg", "maskad": False, "kategori": "namn", "roll": None}), page=PAGE + " Bergman")
    assert r.observationer[0]["vittne"]["namn"] == "Berg"  # "Anna Berg" på sidan
    page = "Vittnet Bergman såg kl 23.21 en man i mörk rock springa."
    r = parse(cfg, obs(utdrag="såg kl 23.21 en man i mörk rock", vittne={"namn": "Berg", "maskad": False, "kategori": "namn", "roll": None}), page=page)
    assert r.observationer[0]["vittne"]["namn"] is None


@pytest.mark.parametrize(
    "utdrag,pid",
    [("Palmeutredningen leddes av en man i mörk rock", "olof-palme"), ("en sportskytten i mörk rock sprang", "garningsmannen")],
)
def test_alias_needs_word_boundary(cfg, utdrag, pid):
    o = obs(utdrag=utdrag, observerade=[{"beskrivning": "x", "signalement": [], "kan_vara": [pid]}])
    r = parse(cfg, o, page=utdrag)
    assert r.observationer[0]["observerade"][0]["kan_vara"] == []


def test_malformed_lists_do_not_crash(cfg):
    o = obs(observerade=[
        {"beskrivning": "x", "signalement": 5, "kan_vara": "olof-palme"},
        {"beskrivning": "y", "signalement": "abc", "kan_vara": 7},
        "skräp",
        {"beskrivning": "z", "signalement": ["x", 3], "kan_vara": []},
    ])
    r = parse(cfg, o)
    ob = r.observationer[0]["observerade"]
    assert len(ob) == 3 and all(x["signalement"] == [] and x["kan_vara"] == [] for x in ob)
    assert any(w.startswith("ogiltig post:") for w in r.varningar)
    r = parse(cfg, obs(observerade=5))
    assert r.observationer[0]["observerade"] == []


def test_prompt_mentions_alias_and_placeholder_rules():
    assert "alias" in SYSTEM_PROMPT and "NN" in SYSTEM_PROMPT and "okänd" in SYSTEM_PROMPT


@pytest.mark.parametrize("namn", ["***", "---", "..."])
def test_letterless_name_is_unknown(cfg, namn):
    r = parse(cfg, obs(vittne={"namn": namn, "maskad": False, "kategori": "namn", "roll": None}))
    v = r.observationer[0]["vittne"]
    assert v["namn"] is None and v["maskad"] is False
    assert any(w.startswith("namn saknar bokstäver:") for w in r.varningar)


def test_inflected_aliases_in_config(cfg):
    alias = {a for p in cfg.nyckelpersoner for a in p.alias}
    assert {"Palmes", "gärningsmannens", "mördarens", "skyttens", "statsministerns", "Lisbet Palmes"} <= alias


_SCHEMANYCKLAR = (
    "observationer", "tid_text", "tid_fran", "tid_till", "plats", "handelse_id", "vittne",
    "namn", "maskad", "kategori", "roll", "observerade", "beskrivning", "signalement", "typ",
    "varde_text", "min", "max", "enhet", "kan_vara", "utdrag", "osakerhet",
)


def test_prompt_contains_the_response_schema(cfg):
    prompt = build_prompt("Sidans text.", cfg)
    for nyckel in _SCHEMANYCKLAR:
        assert f'"{nyckel}"' in prompt, nyckel
    assert prompt.index('"observationer"') < prompt.index("Sidans text.")
    assert json.dumps(cfg.schema, ensure_ascii=False) in prompt
    assert "exakt" in prompt


def test_schema_enums_match_code(cfg):
    obs_schema = cfg.schema["properties"]["observationer"]["items"]["properties"]
    typ = obs_schema["observerade"]["items"]["properties"]["signalement"]["items"][
        "properties"]["typ"]["enum"]
    kategori = obs_schema["vittne"]["properties"]["kategori"]["enum"]
    assert tuple(typ) == SIGNALEMENT_TYPER
    assert tuple(kategori) == VITTNESKATEGORIER


def test_masked_witness_role_must_stand_on_page(cfg):
    r = parse(cfg, obs(vittne={"namn": "[MASKAD]", "maskad": True, "kategori": "namn",
                               "roll": "granne, Karin Ek"}))
    v = r.observationer[0]["vittne"]
    assert v["maskad"] is True and v["roll"] is None
    assert any(w.startswith("roll saknas på sidan:") for w in r.varningar)


def test_masked_witness_role_on_page_is_kept(cfg):
    r = parse(cfg, obs(vittne={"namn": "[MASKAD]", "maskad": True, "kategori": "namn",
                               "roll": "Vittnet"}))
    assert r.observationer[0]["vittne"]["roll"] == "Vittnet"
    r = parse(cfg, obs(vittne={"namn": "[MASKAD]", "maskad": True, "kategori": "namn",
                               "roll": "vitt"}))
    assert r.observationer[0]["vittne"]["roll"] is None
