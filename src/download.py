#!/usr/bin/env python3
"""
Ladda ner alla filer från palmemordsarkivet.se (publikt Google Sheet)
via länkarna i kolumnen "Länk till kopia".

Användning:
    pip install gdown requests
    python download.py [målmapp]
    python download.py --out files --sheet-id <ID>

Idempotens spåras via state.db (tabellen ``downloads``).
"""

import argparse
import csv
import hashlib
import io
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

import db as state_db

try:  # valfri snyggare format-sniff
    import filetype as _filetype  # type: ignore
except Exception:  # pragma: no cover
    _filetype = None

try:
    from errors_log import log_error
except Exception:  # pragma: no cover
    def log_error(component: str, item: str, message: str) -> None:
        pass

SHEET_ID = "1O37mhN5bMt5nd-CaO7ue_3KMbip6eVETWKXwfILsf3E"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"

LINK_COLUMN = "Länk till kopia"
NAME_COLUMNS = [
    ("Nummer", "nr"),
    ("Titel", "titel"),
    ("Beställt", "best"),
    ("Upplagt/uppdaterat", "uppl"),
    ("Anmärkning", "anm"),
    ("Antal sidor", "sid"),
]

DRIVE_ID_RE = re.compile(r"/d/([a-zA-Z0-9_-]+)|[?&]id=([a-zA-Z0-9_-]+)")

# Magic-bytes -> extension. Räcker för det mesta i arkivet (PDF dominerar).
MAGIC = [
    (b"%PDF", ".pdf"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"PK\x03\x04", ".zip"),  # även docx/xlsx, men ovanligt här
    (b"{\\rtf", ".rtf"),
    (b"\xd0\xcf\x11\xe0", ".doc"),  # gamla MS Office
    (b"II*\x00", ".tif"),
    (b"MM\x00*", ".tif"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),  # WebP börjar med RIFF....WEBP — snäll fallback
    (b"\x00\x00\x00\x18ftypheic", ".heic"),
    (b"\x00\x00\x00\x18ftypheix", ".heic"),
    (b"\x00\x00\x00\x20ftypheic", ".heic"),
]

HTML_MARKERS = (b"<!DOCTYPE html", b"<html", b"<HTML")


def extract_drive_id(url: str) -> str | None:
    m = DRIVE_ID_RE.search(url or "")
    if not m:
        return None
    return m.group(1) or m.group(2)


def clean_part(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "-", s)
    return s


def build_filename(values: dict[str, str], maxlen: int = 180) -> str:
    parts = []
    for col, _short in NAME_COLUMNS:
        v = clean_part(values.get(col, ""))
        if v:
            parts.append(v)
    name = " — ".join(parts) if parts else "fil"
    name = name.strip(". ")
    return name[:maxlen]


_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code in _TRANSIENT_STATUS
    return False


@retry(
    stop=stop_after_attempt(5),
    wait=wait_random_exponential(multiplier=1, max=16),
    retry=retry_if_exception(_is_transient),
    reraise=True,
)
def _request_with_retry(session: requests.Session, url: str, **kwargs):
    """GET med random-exponential backoff på transienta fel. Max 5 försök."""
    r = session.get(url, **kwargs)
    if r.status_code in _TRANSIENT_STATUS:
        raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
    return r


def drive_download(file_id: str, dest: Path, session: requests.Session) -> str:
    """Hämta en publik Drive-fil. Hanterar virusscan-bekräftelsen för stora filer.

    Returnerar sha1 av nedladdad fil (hex). Kastar RuntimeError vid HTML-svar
    (typiskt borttagen fil eller rate limit).
    """
    url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download"}
    r = _request_with_retry(session, url, params=params, stream=True, timeout=120)
    r.raise_for_status()

    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        # Virusscan-bekräftelse: parsa formulärets dolda fält och POST:a
        body = r.content.decode("utf-8", errors="replace")
        action = re.search(r'action="([^"]+)"', body)
        fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', body))
        if not action or not fields:
            raise RuntimeError("kunde inte hantera Drive-bekräftelsesidan (möjligen borttagen)")
        r = _request_with_retry(session, action.group(1), params=fields,
                                stream=True, timeout=300)
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError("fick HTML i andra svaret också (rate limit eller borttagen fil)")

    h = hashlib.sha1()
    first = True
    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 15):
            if not chunk:
                continue
            if first:
                # Sniffa HTML i första chunken — vägrar spara borttagen-fil-sida
                if any(chunk.lstrip()[:64].startswith(m) for m in HTML_MARKERS):
                    raise RuntimeError("svaret var HTML — sannolikt borttagen fil")
                first = False
            h.update(chunk)
            f.write(chunk)
    return h.hexdigest()


def sniff_extension(path: Path) -> str:
    if _filetype is not None:
        try:
            kind = _filetype.guess(str(path))
            if kind is not None:
                ext = "." + kind.extension
                # Normalisera tiff -> tif så vi matchar magic-listans val
                if ext == ".tiff":
                    ext = ".tif"
                if ext == ".jpeg":
                    ext = ".jpg"
                return ext
        except Exception:
            pass
    try:
        with path.open("rb") as f:
            head = f.read(32)
    except OSError:
        return ""
    # WebP: RIFF<4>WEBP
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp"
    for sig, ext in MAGIC:
        if head.startswith(sig):
            return ext
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out", nargs="?",
                    default=os.environ.get("OUT", "downloaded/files"),
                    help="målmapp (positionellt eller via env OUT, default: downloaded/files)")
    ap.add_argument("--out", dest="out_flag",
                    help="alternativ till positionellt argument")
    ap.add_argument("--sheet-id",
                    default=os.environ.get("SHEET_ID", SHEET_ID),
                    help=f"Google Sheets-ID (default: {SHEET_ID[:12]}…)")
    ap.add_argument("--limit", type=int, default=0,
                    help="begränsa till N filer (0 = alla, används för testkörningar)")
    args = ap.parse_args()

    out_dir = Path(args.out_flag or args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = state_db.connect()
    state_db.init_schema(conn)
    source = "wpu" if out_dir.name == "wpu_files" else "files"

    csv_url = f"https://docs.google.com/spreadsheets/d/{args.sheet_id}/export?format=csv"
    print(f"Laddar ned palmemordsarkivet-PDF:er → {out_dir}/")
    print("Hämtar kalkylbladet…")
    r = requests.get(csv_url, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"

    rows = list(csv.reader(io.StringIO(r.text)))
    header_idx = next(
        (i for i, row in enumerate(rows) if LINK_COLUMN in row), None
    )
    if header_idx is None:
        print(f"Kunde inte hitta kolumnen {LINK_COLUMN!r}", file=sys.stderr)
        return 1

    header = rows[header_idx]
    link_col = header.index(LINK_COLUMN)
    name_cols = {col: header.index(col) for col, _ in NAME_COLUMNS if col in header}

    todo = []
    for row in rows[header_idx + 1 :]:
        if len(row) <= link_col:
            continue
        file_id = extract_drive_id(row[link_col].strip())
        if not file_id:
            continue
        values = {
            col: (row[idx].strip() if len(row) > idx else "")
            for col, idx in name_cols.items()
        }
        todo.append((build_filename(values), file_id))

    print(f"Hittade {len(todo)} filer")
    if args.limit:
        todo = todo[:args.limit]
        print(f"Test-läge: begränsar till {args.limit} filer")

    manifest_ids = {
        r["drive_id"] for r in conn.execute(
            "SELECT drive_id FROM downloads WHERE source=? AND drive_id IS NOT NULL",
            (source,),
        )
    }
    perm_failed_ids = {
        r["drive_id"] for r in conn.execute(
            "SELECT drive_id FROM downloads"
            " WHERE source=? AND note LIKE 'failed:%' AND drive_id IS NOT NULL",
            (source,),
        )
    }
    manifest_sha1s = {
        r["sha1"]: r["filename"] for r in conn.execute(
            "SELECT sha1, filename FROM downloads WHERE source=? AND sha1 IS NOT NULL",
            (source,),
        )
    }

    # Filnamns-stem-fallback för befintliga nedladdningar (pre-manifest)
    existing_stems = {p.with_suffix("").name for p in out_dir.iterdir() if p.is_file()}

    session = requests.Session()
    failed = []
    total = len(todo)
    n_done = sum(
        1 for prefix, fid in todo
        if fid in manifest_ids or prefix in existing_stems
    )
    n_new = 0
    n_fail = 0
    n_dup = 0
    bytes_total = 0
    t0 = time.monotonic()

    def fmt_dur(secs: float) -> str:
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    def progress(tag: str, prefix: str, extra: str = "") -> None:
        i = n_done + n_new + n_fail + n_dup
        pct = 100 * i / total if total else 0
        elapsed = time.monotonic() - t0
        rate = n_new / elapsed if elapsed > 0 and n_new else 0
        remaining = total - i
        if rate > 0:
            eta_secs = remaining / rate
            finish = datetime.now() + timedelta(seconds=eta_secs)
            today = datetime.now().date()
            finish_s = finish.strftime(
                "%H:%M" if finish.date() == today else "%m-%d %H:%M"
            )
            eta_s = f" kvar {fmt_dur(eta_secs)}, klar {finish_s}"
        else:
            eta_s = ""
        mb = bytes_total / 1_048_576
        print(
            f"[{i:>5}/{total} {pct:5.1f}%] {tag} {prefix[:80]}"
            f"  ({mb:.1f} MB{eta_s}){extra}",
            flush=True,
        )

    for prefix, file_id in todo:
        # Idempotency 1: manifestet (drive_id)
        if file_id in manifest_ids:
            continue
        # Idempotency 2: filnamns-stem (pre-manifest)
        if prefix in existing_stems:
            continue

        progress("[hämtar]   ", prefix, f"  id={file_id}")
        tmp = out_dir / f"_tmp_{file_id}"
        try:
            sha1 = drive_download(file_id, tmp, session)
            size = tmp.stat().st_size
            bytes_total += size

            # Dubblett-check: samma sha1 finns redan under annat namn
            if sha1 in manifest_sha1s:
                other = manifest_sha1s[sha1]
                print(f"  [dubblett] {prefix} == {other} (sha1={sha1[:10]})", flush=True)
                log_error("download", prefix, f"duplicate sha1 of {other}")
                tmp.unlink()
                # Notera ändå i state.db så vi inte kör om
                state_db.record_download(
                    conn, source=source, drive_id=file_id,
                    filename=other, sha1=sha1, bytes_=size,
                    note=f"dup-of:{other}",
                )
                manifest_ids.add(file_id)
                n_dup += 1
            else:
                ext = sniff_extension(tmp) or ".bin"
                dst = out_dir / f"{prefix}{ext}"
                i = 2
                while dst.exists():
                    dst = out_dir / f"{prefix} ({i}){ext}"
                    i += 1
                tmp.rename(dst)
                existing_stems.add(dst.with_suffix("").name)
                state_db.record_download(
                    conn, source=source, drive_id=file_id,
                    filename=dst.name, sha1=sha1, bytes_=size,
                )
                state_db.upsert_pdf_file(
                    conn, pdf_stem=dst.stem, source=source,
                    pdf_path=str(dst),
                )
                manifest_ids.add(file_id)
                manifest_sha1s[sha1] = dst.name
                n_new += 1
        except Exception as e:
            n_fail += 1
            err_str = str(e)
            print(f"  FEL: {err_str}", file=sys.stderr, flush=True)
            log_error("download", prefix, err_str)
            failed.append((prefix, file_id, err_str))
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
            # Permanenta fel registreras i state.db så att filen hoppas över
            # vid nästa körning. Två fall:
            # 1. HTTP-fel som inte är transienta (t.ex. 404).
            # 2. RuntimeError från drive_download när Drive svarar med HTML
            #    (borttagen fil eller oväntat svar).
            if isinstance(e, requests.HTTPError) and not _is_transient(e):
                status = getattr(getattr(e, "response", None), "status_code", "?")
                note = f"failed:{status}"
            elif isinstance(e, RuntimeError):
                note = "failed:html-response"
            else:
                note = None
            if note:
                state_db.record_download(
                    conn, source=source, drive_id=file_id,
                    filename=prefix, note=note,
                )
                manifest_ids.add(file_id)
        # Liten paus för att undvika throttling
        time.sleep(0.1)

    n_perm_skip = sum(1 for _, fid in todo if fid in perm_failed_ids)
    n_already = n_done - n_perm_skip

    summary_parts = []
    if n_new:        summary_parts.append(f"{n_new} nya")
    if n_dup:        summary_parts.append(f"{n_dup} dubbletter")
    if n_already:    summary_parts.append(f"{n_already} redan hämtade")
    if n_perm_skip:  summary_parts.append(f"{n_perm_skip} permanent misslyckade (skippas)")

    if failed:
        print(f"\n{len(failed)} filer misslyckades:")
        for prefix, fid, err in failed:
            print(f"  {fid}  {prefix}: {err}")
        if summary_parts:
            print(f"Övrigt: {', '.join(summary_parts)}.")
        return 2
    print(f"Klart. {', '.join(summary_parts)}." if summary_parts else "Klart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
