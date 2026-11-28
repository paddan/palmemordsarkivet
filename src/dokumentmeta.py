"""Dokumentmetadata ur arkivets filnamnsstammar.

Rena funktioner utan Streamlit, nätverk eller loggning: varningar returneras
och loggas av anroparen (ingest). Palme-stammarna byggs av kalkylarket som
``nr — titel — [beställt —] [upplagt —] [anmärkning —] [sidor]``.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Any, Literal

# Samma gräns som download.build_filename(maxlen=180): en stam av den längden
# är sannolikt avklippt.
MAX_STEM_CHARS = 180

SEP = " — "

# Dokument-ID i wpu-filnamn: PREFIX{digits}-{NN}[-{SUFFIX}]
# Exempel: DA14259-00, EDE9980-00-A, IVA16636-00-B
WPU_ID_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{1,4})(\d+)-(\d{2})(?:-([A-Z]+))?(?![A-Za-z\d])"
)

# Dokument-ID i palmemordsarkivet-filnamn: PREFIX-{digits}[-version|-suffix|-version-suffix]
# Exempel: DA-14259, DA-14244-A, DA-14244-09, EDE-9980, IVA-16636-B
PALME_ID_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{1,4})-(\d+)"
    r"(?:"
    r"  -(\d+)(?:-([A-Z]+))?"   # -version eller -version-suffix
    r"  |"
    r"  -([A-Z]+)"              # bara suffix (version → 0)
    r")?(?![A-Za-z\d])",
    re.VERBOSE,
)

IdKey = tuple[str, int, int, str]  # (prefix, base, version, suffix_char)


def _expand_suffix(suffix: str) -> list[str]:
    return list(suffix) if suffix else [""]


def wpu_id_keys(filename: str) -> set[IdKey]:
    """Extrahera dokument-ID-nycklar ur ett wpu-filnamn."""
    keys: set[IdKey] = set()
    for m in WPU_ID_RE.finditer(filename):
        prefix = m.group(1)
        base = int(m.group(2))
        version = int(m.group(3))
        for ch in _expand_suffix(m.group(4) or ""):
            keys.add((prefix, base, version, ch))
    return keys


def palme_id_keys(filename: str) -> set[IdKey]:
    """Extrahera dokument-ID-nycklar ur ett palmemordsarkivet-filnamn."""
    keys: set[IdKey] = set()
    for m in PALME_ID_RE.finditer(filename):
        prefix = m.group(1)
        base = int(m.group(2))
        if m.group(3) is not None:
            version = int(m.group(3))
            suffix = m.group(4) or ""
        elif m.group(5) is not None:
            version = 0
            suffix = m.group(5)
        else:
            version = 0
            suffix = ""
        for ch in _expand_suffix(suffix):
            keys.add((prefix, base, version, ch))
    return keys


def uppslag_key_str(key: IdKey) -> str:
    """Textform av en ID-nyckel, t.ex. ``("E", 63, 1, "")`` → ``"E63-01"``."""
    prefix, base, version, suffix = key
    text = f"{prefix}{base}-{version:02d}"
    return f"{text}-{suffix}" if suffix else text


@dataclass(frozen=True)
class DokumentMeta:
    """Metadata tolkad ur en filnamnsstam."""

    pdf_stem: str
    arkiv: Literal["palme", "wpu"]
    nr: str | None
    titel: str
    bestallt: str | None
    upplagt: str | None
    anmarkning: str | None
    antal_sidor: int | None
    dokumentdatum: str | None
    uppslag: tuple[str, ...]
    hanvisningar: tuple[str, ...]
    avkortat: bool
    varningar: tuple[str, ...]

    def to_row(self) -> dict[str, Any]:
        """Alla fält utom ``varningar``; tupler blir listor."""
        row: dict[str, Any] = {}
        for name in self.__dataclass_fields__:
            if name == "varningar":
                continue
            value = getattr(self, name)
            row[name] = list(value) if isinstance(value, tuple) else value
        return row


_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PARTIAL_DATE_RE = re.compile(r"^\d{4}(-\d{0,2}){0,2}$")
_SE_RE = re.compile(r"(?i)\bse\s+(.*)$")
_REF_RE = re.compile(r"^\d+(?:[.,]\d+)*[A-Za-z]?$")


def _hanvisningar(anmarkning: str) -> tuple[str, ...]:
    m = _SE_RE.search(anmarkning)
    if not m:
        return ()
    refs: list[str] = []
    for token in re.split(r",\s+", m.group(1).strip()):
        if not _REF_RE.match(token):
            break
        refs.append(token)
    return tuple(refs)


def _parse_palme(stem: str) -> DokumentMeta:
    parts = [p.strip() for p in stem.split(SEP)]
    nr = parts[0]
    titel = parts[1] if len(parts) > 1 else ""
    rest = parts[2:]
    avkortat = len(stem) >= MAX_STEM_CHARS
    varningar: list[str] = []

    if (
        avkortat
        and rest
        and _PARTIAL_DATE_RE.match(rest[-1])
        and not _FULL_DATE_RE.match(rest[-1])
    ):
        varningar.append(f"avkortat datum: {rest[-1]!r} släppt")
        rest = rest[:-1]

    datum: list[str] = []
    while rest and len(datum) < 2 and _FULL_DATE_RE.match(rest[0]):
        datum.append(rest.pop(0))
    bestallt = datum[0] if len(datum) == 2 else None
    upplagt = datum[-1] if datum else None

    antal_sidor: int | None = None
    hade_sidor = False
    if rest and (rest[-1].isdigit() or rest[-1] == "-"):
        last = rest.pop()
        hade_sidor = last.isdigit()
        if last.isdigit():
            if avkortat:
                varningar.append(f"sidantal kan vara avklippt: {last!r} ignorerat")
            else:
                antal_sidor = int(last)

    anmarkning = SEP.join(rest) if rest else None
    if len(rest) > 1:
        varningar.append(f"flera delar i anmärkningen: {len(rest)}")

    hanvisningar = _hanvisningar(anmarkning or "")
    if avkortat and anmarkning and not hade_sidor and hanvisningar:
        # Anmärkningen är sista delen i en kapad stam: sista token kan vara avklippt.
        varningar.append(f"sista hänvisningen kan vara avklippt: {hanvisningar[-1]!r} släppt")
        hanvisningar = hanvisningar[:-1]

    uppslag = tuple(
        sorted({uppslag_key_str(k) for k in palme_id_keys(anmarkning or "")})
    )
    return DokumentMeta(
        pdf_stem=stem,
        arkiv="palme",
        nr=nr,
        titel=titel,
        bestallt=bestallt,
        upplagt=upplagt,
        anmarkning=anmarkning,
        antal_sidor=antal_sidor,
        dokumentdatum=None,
        uppslag=uppslag,
        hanvisningar=hanvisningar,
        avkortat=avkortat,
        varningar=tuple(varningar),
    )


_POL_RE = re.compile(r"^(?i:pol)-(\d{4}|YYYY)-(\d{2}|MM)-(\d{2}|DD)[_-](.*)$", re.DOTALL)
_PLAIN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.*)$", re.DOTALL)
_TIME_PREFIX_RE = re.compile(r"^(?:\d{4}(?:-\d{4})?|\d{2}-\d{2})_")
_UNKNOWN_CODE_PREFIX = "OKÄND_UPPSLAGSKOD_"


def _pol_datum(year: str, month: str, day: str, varningar: list[str]) -> str | None:
    """Datum med reducerad precision ur ``Pol-``-stammens tre fält."""
    if year == "YYYY":
        return None
    if month == "MM":
        return year
    if day == "DD":
        return f"{year}-{month}"
    try:
        return datetime.date.fromisoformat(f"{year}-{month}-{day}").isoformat()
    except ValueError:
        varningar.append(f"ogiltigt datum: {year}-{month}-{day}")
        return None


def _parse_wpu(stem: str) -> DokumentMeta:
    varningar: list[str] = []
    uppslag: tuple[str, ...] = ()
    dokumentdatum: str | None = None
    pol = _POL_RE.match(stem)
    plain = _PLAIN_DATE_RE.match(stem)
    if pol:
        dokumentdatum = _pol_datum(pol.group(1), pol.group(2), pol.group(3), varningar)
        rest = pol.group(4)
        rest = rest.removeprefix(_UNKNOWN_CODE_PREFIX)
        tid = _TIME_PREFIX_RE.match(rest)
        start = tid.end() if tid else 0
        kod = WPU_ID_RE.match(rest, start)
        if kod:
            uppslag = tuple(
                sorted({uppslag_key_str(k) for k in wpu_id_keys(kod.group(0))})
            )
            end = kod.end()
            if rest.startswith("_", end):
                end += 1
            rest = rest[:start] + rest[end:]
    elif plain:
        dokumentdatum = plain.group(1)
        rest = plain.group(2)
    else:
        rest = stem
    return DokumentMeta(
        pdf_stem=stem,
        arkiv="wpu",
        nr=None,
        titel=rest.replace("_", " ").strip(),
        bestallt=None,
        upplagt=None,
        anmarkning=None,
        antal_sidor=None,
        dokumentdatum=dokumentdatum,
        uppslag=uppslag,
        hanvisningar=(),
        avkortat=False,
        varningar=tuple(varningar),
    )


def parse_stem(stem: str) -> DokumentMeta:
    """Tolka en stam: palme om den innehåller `` — ``, annars wpu."""
    if SEP in stem:
        return _parse_palme(stem)
    return _parse_wpu(stem)


def lance_name_fields(stem: str) -> dict[str, str]:
    """``nr``/``titel`` till LanceDB, exakt som den gamla ``parse_filename``."""
    parts = [p.strip() for p in stem.split(SEP)]
    nr = parts[0] or stem[:40]
    return {"nr": nr, "titel": parts[1] if len(parts) > 1 else ""}
