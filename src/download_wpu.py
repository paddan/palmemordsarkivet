#!/usr/bin/env python3
"""
Laddar ner filer från wpu.nu som saknas i palmemordsarkivet.

Jämför DA-nummer i wpu.nu-filnamn (t.ex. DA14259-00, DA14244-09-A) mot
DA-nummer i befintliga palmemordsarkivet-filer (DA-14259, DA-14244-09-A).
Laddar bara ner det som saknas till files_wpu/.

Format-mappning:
  palmemordsarkivet     wpu.nu
  DA-14259           →  DA14259-00
  DA-14259-1         →  DA14259-01
  DA-14244-A         →  DA14244-00-A
  DA-14244-09-ABC    →  DA14244-09-A + DA14244-09-B + DA14244-09-C

Kör:
    python download_wpu.py            # ladda ner saknade PDF:er
    python download_wpu.py --dry-run  # lista utan att ladda ner
    python download_wpu.py --da-only  # bara filer med DA-nummer
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

# wpu-format: DA14259-00, DA14259-01-A, DA1160-00-ABC
# Lookbehind: DA får inte föregås av bokstav (men _ är OK, t.ex. _DA1160-00_)
WPU_DA_RE = re.compile(r"(?<![A-Za-z])DA(\d+)-(\d{2})(?:-([A-Z]+))?(?![A-Za-z\d])")

# palmemordsarkivet-format: DA-14259, DA-14244-A, DA-14244-09, DA-14244-09-ABC
# Disambiguering: siffror efter bas = version; stora bokstäver = suffix.
PALME_DA_RE = re.compile(
    r"(?<![A-Za-z])DA-(\d+)"
    r"(?:"
    r"  -(\d+)(?:-([A-Z]+))?"  # -version eller -version-suffix
    r"  |"
    r"  -([A-Z]+)"             # bara suffix (ingen version → version 0)
    r")?(?![A-Za-z\d])",
    re.VERBOSE,
)

DaKey = tuple[int, int, str]  # (base, version, suffix_char)


def _expand_suffix(suffix: str) -> list[str]:
    """'ABC' → ['A','B','C'], '' → ['']"""
    return list(suffix) if suffix else [""]


def wpu_da_keys(filename: str) -> set[DaKey]:
    """Extrahera DA-nycklar ur ett wpu-filnamn. Multi-bokstavssuffix delas upp."""
    keys: set[DaKey] = set()
    for m in WPU_DA_RE.finditer(filename):
        base = int(m.group(1))
        version = int(m.group(2))
        for ch in _expand_suffix(m.group(3) or ""):
            keys.add((base, version, ch))
    return keys


def palme_da_keys(filename: str) -> set[DaKey]:
    """Extrahera DA-nycklar ur ett palmemordsarkivet-filnamn."""
    keys: set[DaKey] = set()
    for m in PALME_DA_RE.finditer(filename):
        base = int(m.group(1))
        if m.group(2) is not None:
            version = int(m.group(2))
            suffix = m.group(3) or ""
        elif m.group(4) is not None:
            version = 0
            suffix = m.group(4)
        else:
            version = 0
            suffix = ""
        for ch in _expand_suffix(suffix):
            keys.add((base, version, ch))
    return keys


def collect_palme_keys() -> set[DaKey]:
    """Samla alla DA-nycklar från befintliga filer i files/."""
    keys: set[DaKey] = set()
    palme_dir = ROOT / "files"
    if palme_dir.is_dir():
        for f in palme_dir.glob("*.pdf"):
            keys.update(palme_da_keys(f.name))
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


def _da_label(keys: set[DaKey]) -> str:
    if not keys:
        return "(inget DA-nr)"
    parts = sorted(f"DA{b}-{v:02d}{'-' + s if s else ''}" for b, v, s in keys)
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
        "--da-only", action="store_true",
        help="ladda bara filer med DA-nummer i filnamnet",
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

    print("Läser befintliga DA-nycklar från files/…")
    palme_keys = collect_palme_keys()
    print(f"  {len(palme_keys)} DA-nycklar i palmemordsarkivet")

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

        keys = wpu_da_keys(name)

        if args.da_only and not keys:
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
            print(f"  {f['name'][:72]:72s}  [{_da_label(f['keys'])}]")
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
