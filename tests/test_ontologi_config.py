"""Ontologins konfiguration i data/ontologi/ och dess laddare."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ontologi.config import ROOT, load_config


def _kopiera(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "data", tmp_path / "data")
    return tmp_path


def _skriv(root: Path, fil: str, data: object) -> None:
    (root / "data" / "ontologi" / fil).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _las(root: Path, fil: str) -> dict:
    return json.loads((root / "data" / "ontologi" / fil).read_text(encoding="utf-8"))


def test_repo_config_loads():
    cfg = load_config()
    assert cfg.schemaversion == 1 and len(cfg.handelser) == 11
    assert cfg.urval.max_sidor == 100 and "EAE" in cfg.urval.serier
    assert {p.id for p in cfg.nyckelpersoner} == {
        "garningsmannen",
        "olof-palme",
        "lisbet-palme",
    }
    assert cfg.platser and "name" in cfg.platser[0]


def test_duplicate_event_id_is_rejected(tmp_path):
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d["handelser"].append(dict(d["handelser"][0]))
    _skriv(root, "ankarhandelser.json", d)
    with pytest.raises(ValueError, match="ankarhandelser.json"):
        load_config(root)


def test_bad_time_is_rejected(tmp_path):
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d["handelser"][0]["tid_fran"] = "23.21"
    _skriv(root, "ankarhandelser.json", d)
    with pytest.raises(ValueError, match="HH:MM"):
        load_config(root)


def test_reversed_window_is_rejected(tmp_path):
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d["handelser"][0]["tid_fran"] = "23:30"
    d["handelser"][0]["tid_till"] = "23:20"
    _skriv(root, "ankarhandelser.json", d)
    with pytest.raises(ValueError, match="ankarhandelser.json"):
        load_config(root)


def test_unknown_key_is_rejected(tmp_path):
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d["handelser"][0]["extra"] = 1
    _skriv(root, "ankarhandelser.json", d)
    with pytest.raises(ValueError, match="extra"):
        load_config(root)


def test_max_sidor_below_one_is_rejected(tmp_path):
    root = _kopiera(tmp_path)
    d = _las(root, "urval.json")
    d["max_sidor"] = 0
    _skriv(root, "urval.json", d)
    with pytest.raises(ValueError, match="urval.json"):
        load_config(root)


@pytest.mark.parametrize("varde", [0, -1, "1", 1.5, True])
def test_bad_schemaversion_is_rejected(tmp_path, varde):
    root = _kopiera(tmp_path)
    d = _las(root, "schema.json")
    d["schemaversion"] = varde
    _skriv(root, "schema.json", d)
    with pytest.raises(ValueError, match="schema.json"):
        load_config(root)


def test_draft_flag_is_exposed(tmp_path):
    assert load_config().handelser_utkast is True
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d.pop("utkast", None)
    _skriv(root, "ankarhandelser.json", d)
    assert load_config(root).handelser_utkast is False


@pytest.mark.parametrize("varde", ["ja", 1, None])
def test_draft_flag_must_be_bool(tmp_path, varde):
    root = _kopiera(tmp_path)
    d = _las(root, "ankarhandelser.json")
    d["utkast"] = varde
    _skriv(root, "ankarhandelser.json", d)
    with pytest.raises(ValueError, match="utkast"):
        load_config(root)
