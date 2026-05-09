#!/usr/bin/env python3
"""
Laddar ner filer från wpu.nu som saknas i palmemordsarkivet.

Jämför dokument-ID:n i wpu.nu-filnamn mot ID:n i befintliga
palmemordsarkivet-filer. Stödjer alla prefix: DA, EDE, IVA, EAD, PM m.fl.

Format-mappning (wpu saknar bindestreck mellan prefix och bas, version
är alltid tvåsiffrig, multi-bokstavssuffix representerar separata dokument):

  palmemordsarkivet       wpu.nu
  DA-14259             →  DA14259-00
  DA-14259-1           →  DA14259-01
  DA-14244-A           →  DA14244-00-A
  DA-14244-09-ABC      →  DA14244-09-A + DA14244-09-B + DA14244-09-C
  EDE-9980             →  EDE9980-00  (om wpu använder samma mönster)
  IVA-16636-B          →  IVA16636-00-B

Kör:
    python download_wpu.py            # ladda ner saknade PDF:er
    python download_wpu.py --dry-run  # lista utan att ladda ner
    python download_wpu.py --id-only  # bara filer med känt dokument-ID
    python download_wpu.py --rebuild  # ladda ner igen även om filen finns
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "files_wpu"
WPU_API = "https://wpu.nu/api.php"
USER_AGENT = "palmemordsarkivet-wpu-downloader/1.0"

# wpu-format: PREFIX{digits}-{NN}[-{SUFFIX}]
# Exempel: DA14259-00, EDE9980-00-A, IVA16636-00-B, DA14244-09-ABC
# Lookbehind: prefix får inte föregås av bokstav (undviker JDA, EXP osv.)
WPU_ID_RE = re.compile(
    r"(?<![A-Za-z])([A-Z]{1,4})(\d+)-(\d{2})(?:-([A-Z]+))?(?![A-Za-z\d])"
)

# palmemordsarkivet-format: PREFIX-{digits}[-version|-suffix|-version-suffix]
# Exempel: DA-14259, DA-14244-A, DA-14244-09, DA-14244-09-ABC, EDE-9980, IVA-16636-B
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
    """'ABC' → ['A','B','C'], '' → ['']"""
    return list(suffix) if suffix else [""]


def wpu_id_keys(filename: str) -> set[IdKey]:
    """Extrahera dokument-ID-nycklar ur ett wpu-filnamn.
    Multi-bokstavssuffix (ABC) expanderas till separata nycklar (A, B, C).
    """
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
            # PREFIX-digits-version[-suffix]
            version = int(m.group(3))
            suffix = m.group(4) or ""
        elif m.group(5) is not None:
            # PREFIX-digits-suffix (ingen explicit version → 0)
            version = 0
            suffix = m.group(5)
        else:
            # PREFIX-digits (bara bas)
            version = 0
            suffix = ""
        for ch in _expand_suffix(suffix):
            keys.add((prefix, base, version, ch))
    return keys


def collect_palme_keys() -> set[IdKey]:
    """Samla alla dokument-ID-nycklar från befintliga filer i files/."""
    keys: set[IdKey] = set()
    palme_dir = ROOT / "files"
    if palme_dir.is_dir():
        for f in palme_dir.glob("*.pdf"):
            keys.update(palme_id_keys(f.name))
    return keys


def fetch_all_wpu_files() -> list[dict]:
    """Hämta alla filer från wpu.nu via MediaWiki API (paginerat)."""
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    files: list[dict] = []
    params: dict = {
        "action": "query",
        "list": "allimages",
        "aiprop": "url|size",
        "format": "json",
        "ailimit": "500",
    }
    while True:
        resp = session.get(WPU_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        files.extend(data.get("query", {}).get("allimages", []))
        cont = data.get("continue", {}).get("aicontinue")
        if not cont:
            break
        params["aicontinue"] = cont
        time.sleep(0.3)
    return files


_TRANSIENT = {429, 500, 502, 503, 504}


def _is_transient(exc: Exception) -> bool:
    return (
        isinstance(exc, requests.HTTPError)
        and getattr(getattr(exc, "response", None), "status_code", 0) in _TRANSIENT
    )


@retry(
    stop=stop_after_attempt(5),
    wait=wait_random_exponential(multiplier=1, max=16),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)
def _download(session: requests.Session, url: str, dest: Path) -> None:
    r = session.get(url, stream=True, timeout=120)
    r.raise_for_status()
    tmp = dest.with_suffix(".tmp")
    try:
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(65536):
                fh.write(chunk)
        tmp.rename(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _id_label(keys: set[IdKey]) -> str:
    if not keys:
        return "(inget ID)"
    parts = sorted(
        f"{p}{b}-{v:02d}{'-' + s if s else ''}" for p, b, v, s in keys
    )
    return ", ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="visa vad som skulle laddas ner utan att göra det",
    )
    ap.add_argument(
        "--id-only", action="store_true",
        help="ladda bara filer med känt dokument-ID i filnamnet",
    )
    ap.add_argument(
        "--rebuild", action="store_true",
        help="ladda ner igen även om filen redan finns i files_wpu/",
    )
    ap.add_argument(
        "--out", default=str(OUT_DIR),
        help=f"utdatakatalog (default: {OUT_DIR})",
    )
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(exist_ok=True)

    print("Hämtar fillista från wpu.nu…")
    all_wpu = fetch_all_wpu_files()
    pdfs = [f for f in all_wpu if f["name"].lower().endswith(".pdf")]
    print(f"  {len(pdfs)} PDF-filer på wpu.nu (av {len(all_wpu)} totalt)")

    print("Läser befintliga dokument-ID:n från files/…")
    palme_keys = collect_palme_keys()
    prefixes = sorted({p for p, *_ in palme_keys})
    print(f"  {len(palme_keys)} ID-nycklar ({', '.join(prefixes) or 'inga'})")

    already_local = (
        {f.name for f in out_dir.glob("*.pdf")} if out_dir.is_dir() else set()
    )

    to_download: list[dict] = []
    n_covered = 0
    n_skipped_local = 0

    for f in pdfs:
        name = f["name"]

        if not args.rebuild and name in already_local:
            n_skipped_local += 1
            continue

        keys = wpu_id_keys(name)

        if args.id_only and not keys:
            continue

        if keys and keys.issubset(palme_keys):
            n_covered += 1
            continue

        to_download.append({"name": name, "url": f["url"], "keys": keys})

    print(f"  {n_covered} wpu-filer redan täckta av palmemordsarkivet")
    if n_skipped_local:
        print(f"  {n_skipped_local} redan nedladdade till {out_dir.name}/")
    print(f"  {len(to_download)} att ladda ner")

    if not to_download:
        print("Inget att göra.")
        return 0

    if args.dry_run:
        for f in to_download:
            print(f"  {f['name'][:72]:72s}  [{_id_label(f['keys'])}]")
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    t0 = time.monotonic()
    done = 0
    errors = 0

    for i, f in enumerate(to_download, 1):
        dest = out_dir / f["name"]
        elapsed = time.monotonic() - t0
        rate = i / elapsed if elapsed else 0
        eta = (len(to_download) - i) / rate if rate else 0
        print(
            f"  [{i:>4}/{len(to_download)}] {f['name'][:65]:65s} "
            f"(eta {int(eta // 60)}m{int(eta % 60):02d}s)",
            end="",
            flush=True,
        )
        try:
            _download(session, f["url"], dest)
            done += 1
            print()
        except Exception as e:
            errors += 1
            print(f"  FEL: {e}")

    print(f"\nKlart: {done} nedladdade, {errors} fel → {out_dir}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
