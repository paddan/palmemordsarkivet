#!/usr/bin/env python3
"""
Laddar ner alla PDF-filer från wpu.nu till downloaded/wpu_files/.

Idempotent — hoppar över redan nedladdade filer. Merging av text mot
palmemordsarkivet görs separat av merge_wpu.py.

Kör:
    python download_wpu.py            # ladda ner alla PDF:er
    python download_wpu.py --dry-run  # lista utan att ladda ner
    python download_wpu.py --rebuild  # ladda ner igen även om filen finns
"""

from __future__ import annotations

import argparse
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

import db as state_db

# ID-hjälparna bor i dokumentmeta; uttrycklig re-export så merge_wpu och
# testerna fortsätter importera dem härifrån.
from dokumentmeta import PALME_ID_RE as PALME_ID_RE
from dokumentmeta import WPU_ID_RE as WPU_ID_RE
from dokumentmeta import IdKey as IdKey
from dokumentmeta import palme_id_keys as palme_id_keys
from dokumentmeta import wpu_id_keys as wpu_id_keys

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "downloaded" / "wpu_files"
WPU_API = "https://wpu.nu/api.php"
USER_AGENT = "palmemordsarkivet-wpu-downloader/1.0"


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


def _is_transient(exc: BaseException) -> bool:
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


def _display_out_dir(out_dir: Path) -> str:
    try:
        return str(out_dir.relative_to(ROOT)) + "/"
    except ValueError:
        return str(out_dir) + "/"


def _record_wpu_download(conn, file_info: dict, dest: Path) -> None:
    state_db.record_download(
        conn,
        source="wpu",
        url=file_info["url"],
        filename=file_info["name"],
        bytes_=file_info.get("size"),
    )
    state_db.upsert_pdf_file(
        conn,
        pdf_stem=dest.stem,
        source="wpu",
        pdf_path=str(dest),
    )


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


def run_download_wpu(
    *, out: Path, dry_run: bool, limit: int | None, rebuild: bool, context=None
) -> int:
    """Ladda ned wpu.nu-PDF:er till ``out``. Idempotent via state.db."""
    ctx = _ctx(context)
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)

    conn = None
    if not dry_run:
        conn = state_db.connect()
        state_db.init_schema(conn)

    ctx.log(f"Laddar ned wpu.nu-PDF:er → {_display_out_dir(out)}")
    ctx.log("Hämtar fillista från wpu.nu…")
    all_wpu = fetch_all_wpu_files()
    pdfs = [f for f in all_wpu if f["name"].lower().endswith(".pdf")]
    ctx.log(f"Hittade {len(pdfs)} PDF-filer")
    if limit:
        pdfs = pdfs[:limit]
        ctx.log(f"Test-läge: begränsar till {limit} filer")

    local_pdfs = (
        {f.name.lower(): f for f in out.iterdir() if f.is_file() and f.suffix.lower() == ".pdf"}
        if out.is_dir() else {}
    )
    already_local = set(local_pdfs)

    if conn is not None and not rebuild:
        for f in pdfs:
            local = local_pdfs.get(f["name"].lower())
            if local is not None:
                _record_wpu_download(conn, f, local)

    to_download = [
        f for f in pdfs
        if rebuild or f["name"].lower() not in already_local
    ]
    n_skip = len(pdfs) - len(to_download)

    if not to_download:
        ctx.log(f"Klart. {n_skip} redan hämtade." if n_skip else "Klart.")
        return 0

    if dry_run:
        for f in to_download[:50]:
            keys = wpu_id_keys(f["name"])
            id_str = ", ".join(
                f"{p}{b}-{v:02d}{'-'+s if s else ''}" for p, b, v, s in sorted(keys)
            ) if keys else ""
            ctx.log(f"  {f['name'][:70]:70s}  {id_str}")
        if len(to_download) > 50:
            ctx.log(f"  … och {len(to_download) - 50} till")
        return 0

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    t0 = time.monotonic()
    done = 0
    errors = 0

    for i, f in enumerate(to_download, 1):
        ctx.check_cancelled()
        dest = out / f["name"]
        elapsed = time.monotonic() - t0
        rate = i / elapsed if elapsed else 0
        eta = (len(to_download) - i) / rate if rate else 0
        ctx.progress(i, len(to_download), f["name"])
        ctx.log(
            f"  [{i:>4}/{len(to_download)}] {f['name'][:65]:65s} "
            f"(eta {int(eta // 60)}m{int(eta % 60):02d}s)"
        )
        try:
            _download(session, f["url"], dest)
            if conn is not None:
                _record_wpu_download(conn, f, dest)
            done += 1
        except Exception as e:
            errors += 1
            ctx.log(f"  FEL: {e}", level="error")

    summary_parts = []
    if done:
        summary_parts.append(f"{done} nya")
    if n_skip:
        summary_parts.append(f"{n_skip} redan hämtade")
    if errors:
        summary_parts.append(f"{errors} fel")
    ctx.log(f"\nKlart. {', '.join(summary_parts)}." if summary_parts else "\nKlart.")
    return 0 if not errors else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="lista filer utan att ladda ner",
    )
    ap.add_argument(
        "--rebuild", action="store_true",
        help="ladda ner igen även om filen redan finns",
    )
    ap.add_argument(
        "--out", default=str(OUT_DIR),
        help=f"utdatakatalog (default: {OUT_DIR})",
    )
    ap.add_argument(
        "--limit", type=int, default=0,
        help="begränsa till N filer (0 = alla, används för testkörningar)",
    )
    args = ap.parse_args()

    return run_download_wpu(
        out=Path(args.out),
        dry_run=args.dry_run,
        limit=args.limit if args.limit else None,
        rebuild=args.rebuild,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
