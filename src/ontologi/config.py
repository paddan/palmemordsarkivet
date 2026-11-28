"""Ontologins konfiguration: läser och validerar filerna i ``data/ontologi/``.

Filerna är ägarens redigerbara underlag (ankarhändelser, nyckelpersoner,
urval, svarsschema). Laddaren höjer ``ValueError`` med filnamn och orsak så
att ett fel i en JSON-fil syns direkt i stället för att ge fel längre fram.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SIGNALEMENT_TYPER = ("längd", "ålder", "kroppsbyggnad", "hår", "klädsel", "färg", "övrigt")
NUMERISKA = {"längd": "cm", "ålder": "år"}

_TID = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


@dataclass(frozen=True)
class Ankarhandelse:
    id: str
    namn: str
    ordning: int
    tid_fran: str
    tid_till: str
    beskrivning: str


@dataclass(frozen=True)
class Nyckelperson:
    id: str
    namn: str
    alias: tuple[str, ...]


@dataclass(frozen=True)
class Urval:
    serier: tuple[str, ...]
    sokfragor: tuple[str, ...]
    stammar: tuple[str, ...]
    sidor: tuple[tuple[str, int], ...]
    max_sidor: int


@dataclass(frozen=True)
class OntologiConfig:
    schemaversion: int
    schema: dict
    handelser: tuple[Ankarhandelse, ...]
    nyckelpersoner: tuple[Nyckelperson, ...]
    urval: Urval
    platser: list[dict]
    # Sant när ankarhandelser.json har "utkast": true — tiderna är då inte
    # kontrollerade av ägaren och körningen varnar.
    handelser_utkast: bool = False


def _fel(fil: str, orsak: str) -> ValueError:
    return ValueError(f"{fil}: {orsak}")


def _las(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _fel(path.name, "filen saknas") from None
    except json.JSONDecodeError as exc:
        raise _fel(path.name, f"ogiltig JSON ({exc})") from exc


def _objekt(fil: str, data: object, kravda: set[str], valfria: set[str] | None = None) -> dict:
    """Kräver ett objekt med exakt de kända nycklarna."""
    if not isinstance(data, dict):
        raise _fel(fil, "förväntade ett JSON-objekt")
    okanda = set(data) - kravda - (valfria or set())
    if okanda:
        raise _fel(fil, f"okänd nyckel: {sorted(okanda)[0]}")
    saknas = kravda - set(data)
    if saknas:
        raise _fel(fil, f"nyckeln saknas: {sorted(saknas)[0]}")
    return data


def _strang(fil: str, varde: object, namn: str) -> str:
    if not isinstance(varde, str) or not varde.strip():
        raise _fel(fil, f"{namn} måste vara en icke-tom sträng")
    return varde


def _strangar(fil: str, varde: object, namn: str) -> tuple[str, ...]:
    if not isinstance(varde, list) or not all(isinstance(x, str) and x.strip() for x in varde):
        raise _fel(fil, f"{namn} måste vara en lista med icke-tomma strängar")
    return tuple(varde)


def _tid(fil: str, varde: object, namn: str) -> str:
    if not isinstance(varde, str) or not _TID.match(varde):
        raise _fel(fil, f"{namn} måste vara på formen HH:MM (fick {varde!r})")
    return varde


def _utkast(fil: str, data: object) -> bool:
    """Värdet av ``utkast`` i ankarhändelserna (saknas = inget utkast)."""
    if not isinstance(data, dict) or "utkast" not in data:
        return False
    if not isinstance(data["utkast"], bool):
        raise _fel(fil, f"utkast måste vara true eller false (fick {data['utkast']!r})")
    return data["utkast"]


def _handelser(fil: str, data: object) -> tuple[Ankarhandelse, ...]:
    rot = _objekt(fil, data, {"handelser"}, {"utkast"})
    lista = rot["handelser"]
    if not isinstance(lista, list) or not lista:
        raise _fel(fil, "handelser måste vara en icke-tom lista")
    ut: list[Ankarhandelse] = []
    sedda: set[str] = set()
    for rad in lista:
        h = _objekt(fil, rad, {"id", "namn", "ordning", "tid_fran", "tid_till", "beskrivning"})
        hid = _strang(fil, h["id"], "id")
        if hid in sedda:
            raise _fel(fil, f"dubblett-id: {hid}")
        sedda.add(hid)
        ordning = h["ordning"]
        if not isinstance(ordning, int) or isinstance(ordning, bool):
            raise _fel(fil, f"ordning måste vara ett heltal ({hid})")
        fran = _tid(fil, h["tid_fran"], f"tid_fran ({hid})")
        till = _tid(fil, h["tid_till"], f"tid_till ({hid})")
        if fran > till:
            raise _fel(fil, f"tid_fran {fran} är senare än tid_till {till} ({hid})")
        ut.append(
            Ankarhandelse(
                hid,
                _strang(fil, h["namn"], "namn"),
                ordning,
                fran,
                till,
                _strang(fil, h["beskrivning"], "beskrivning"),
            )
        )
    return tuple(ut)


def _nyckelpersoner(fil: str, data: object) -> tuple[Nyckelperson, ...]:
    rot = _objekt(fil, data, {"nyckelpersoner"})
    lista = rot["nyckelpersoner"]
    if not isinstance(lista, list) or not lista:
        raise _fel(fil, "nyckelpersoner måste vara en icke-tom lista")
    ut: list[Nyckelperson] = []
    sedda: set[str] = set()
    for rad in lista:
        p = _objekt(fil, rad, {"id", "namn", "alias"})
        pid = _strang(fil, p["id"], "id")
        if pid in sedda:
            raise _fel(fil, f"dubblett-id: {pid}")
        sedda.add(pid)
        ut.append(
            Nyckelperson(pid, _strang(fil, p["namn"], "namn"), _strangar(fil, p["alias"], "alias"))
        )
    return tuple(ut)


def _urval(fil: str, data: object) -> Urval:
    u = _objekt(fil, data, {"serier", "sokfragor", "stammar", "sidor", "max_sidor"})
    sidor_raw = u["sidor"]
    if not isinstance(sidor_raw, list):
        raise _fel(fil, "sidor måste vara en lista")
    sidor: list[tuple[str, int]] = []
    for s in sidor_raw:
        if (
            not isinstance(s, list)
            or len(s) != 2
            or not isinstance(s[0], str)
            or not isinstance(s[1], int)
            or isinstance(s[1], bool)
            or s[1] < 1
        ):
            raise _fel(fil, f"sidor ska vara [stam, sidnummer ≥ 1] (fick {s!r})")
        sidor.append((s[0], s[1]))
    mx = u["max_sidor"]
    if not isinstance(mx, int) or isinstance(mx, bool) or mx < 1:
        raise _fel(fil, f"max_sidor måste vara ett heltal ≥ 1 (fick {mx!r})")
    return Urval(
        _strangar(fil, u["serier"], "serier"),
        _strangar(fil, u["sokfragor"], "sokfragor"),
        _strangar(fil, u["stammar"], "stammar"),
        tuple(sidor),
        mx,
    )


def load_config(root: Path = ROOT) -> OntologiConfig:
    """Läser och validerar ontologins fyra filer samt kartans platser."""
    mapp = root / "data" / "ontologi"
    fil = "schema.json"
    sch = _objekt(fil, _las(mapp / fil), {"schemaversion", "schema"})
    version = sch["schemaversion"]
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise _fel(fil, f"schemaversion måste vara ett positivt heltal (fick {version!r})")
    if not isinstance(sch["schema"], dict):
        raise _fel(fil, "schema måste vara ett JSON-objekt")

    handelser_raw = _las(mapp / "ankarhandelser.json")
    handelser = _handelser("ankarhandelser.json", handelser_raw)
    utkast = _utkast("ankarhandelser.json", handelser_raw)
    personer = _nyckelpersoner("nyckelpersoner.json", _las(mapp / "nyckelpersoner.json"))
    urval = _urval("urval.json", _las(mapp / "urval.json"))

    platser = _las(root / "data" / "karta" / "platser.json")
    if not isinstance(platser, list):
        raise _fel("platser.json", "förväntade en lista")
    return OntologiConfig(version, sch["schema"], handelser, personer, urval, platser,
                          handelser_utkast=utkast)
