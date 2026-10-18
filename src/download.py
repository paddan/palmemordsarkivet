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
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

import db as state_db

try:  # valfri snyggare format-sniff
    import filetype as _filetype
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


# Drive svarar 403 både vid rate limit (userRateLimitExceeded/downloadQuotaExceeded)
# och vid verklig nekad åtkomst. Att stämpla 403 som permanent tappar dokument
# för gott, så det behandlas som transient i stället.
_TRANSIENT_STATUS = {403, 408, 429, 500, 502, 503, 504}
_RETRYABLE_FAILURE_NOTES = {"failed:html-response", "failed:403"}


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code in _TRANSIENT_STATUS
    return False


def _permanent_failure_note(exc: BaseException) -> str | None:
    """Returnera state.db-note för fel som säkert ska hoppas över framöver.

    Drive kan svara med HTML både vid borttagna filer och throttling/rate limit.
    De RuntimeError-fallen ska därför inte registreras permanent: hellre ett
    nytt försök i nästa körning än att arkivet tappar ett dokument för gott.
    """
    if not isinstance(exc, requests.HTTPError) or _is_transient(exc):
        return None
    status = getattr(getattr(exc, "response", None), "status_code", "?")
    return f"failed:{status}"


def _is_retryable_failure_note(note: str | None) -> bool:
    return note in _RETRYABLE_FAILURE_NOTES


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
                ext: str = "." + kind.extension
                # Normalisera tiff -> tif så vi matchar magic-listans val
                if ext == ".tiff":
                    ext = ".tif"
                if ext == ".jpeg":
                    ext = ".jpg"
                return ext
        except Exception as exc:
            log_error(
                "download",
                path.name,
                f"filetype kunde inte identifiera filen, faller tillbaka på signaturer: {type(exc).__name__}: {exc}",
            )
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


def _ctx(context):
    """Returnera ``context`` eller en terminal-context för förgrundskörning."""
    if context is not None:
        return context
    from operations.context import ensure_terminal_context

    return ensure_terminal_context(None)


@dataclass
class _Nedladdningslage:
    """Räknare och manifest som följer en körning av ``run_download``."""

    total: int
    t0: float
    n_done: int
    manifest_ids: set[str]
    perm_failed_ids: set[str]
    manifest_sha1s: dict[str, str]
    existing_stems: set[str]
    n_new: int = 0
    n_fail: int = 0
    n_dup: int = 0
    bytes_total: int = 0
    failed: list[tuple[str, str, str]] = field(default_factory=list)


def _formatera_varaktighet(secs: float) -> str:
    """Formatera en varaktighet i sekunder som ``1h02m`` eller ``3m07s``."""
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _logga_framsteg(
    lage: _Nedladdningslage, ctx: Any, tag: str, prefix: str, extra: str = ""
) -> None:
    """Rapportera framsteg med procent, återstående tid och beräknad klar tid."""
    i = lage.n_done + lage.n_new + lage.n_fail + lage.n_dup
    pct = 100 * i / lage.total if lage.total else 0
    elapsed = time.monotonic() - lage.t0
    rate = lage.n_new / elapsed if elapsed > 0 and lage.n_new else 0
    remaining = lage.total - i
    if rate > 0:
        eta_secs = remaining / rate
        finish = datetime.now() + timedelta(seconds=eta_secs)
        today = datetime.now().date()
        finish_s = finish.strftime(
            "%H:%M" if finish.date() == today else "%m-%d %H:%M"
        )
        eta_s = f" kvar {_formatera_varaktighet(eta_secs)}, klar {finish_s}"
    else:
        eta_s = ""
    mb = lage.bytes_total / 1_048_576
    ctx.progress(i, lage.total, f"{tag} {prefix[:80]}")
    ctx.log(
        f"[{i:>5}/{lage.total} {pct:5.1f}%] {tag} {prefix[:80]}"
        f"  ({mb:.1f} MB{eta_s}){extra}"
    )


def _hamta_kalkylrader(sheet_id: str, out: Path, ctx: Any) -> list[list[str]]:
    """Hämta arkets CSV från Google Sheets och dela upp den i rader."""
    csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    ctx.log(f"Laddar ned palmemordsarkivet-PDF:er → {out}/")
    ctx.log("Hämtar kalkylbladet…")
    r = requests.get(csv_url, timeout=60)
    r.raise_for_status()
    r.encoding = "utf-8"
    return list(csv.reader(io.StringIO(r.text)))


def _bygg_nedladdningslista(rows: list[list[str]], ctx: Any) -> list[tuple[str, str]] | None:
    """Plocka ut ``(filnamn, drive-id)`` för varje rad med en giltig länk.

    Returnerar ``None`` och loggar ett fel om länkkolumnen saknas i arket.
    """
    header_idx = next(
        (i for i, row in enumerate(rows) if LINK_COLUMN in row), None
    )
    if header_idx is None:
        ctx.log(f"Kunde inte hitta kolumnen {LINK_COLUMN!r}", level="error")
        return None

    header = rows[header_idx]
    link_col = header.index(LINK_COLUMN)
    name_cols = {col: header.index(col) for col, _ in NAME_COLUMNS if col in header}

    todo: list[tuple[str, str]] = []
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
    return todo


def _las_manifest(
    conn, source: str, out: Path, rebuild: bool, ctx: Any
) -> tuple[set[str], set[str], dict[str, str], set[str]]:
    """Läs manifestet från state.db och befintliga filstammar i ``out``.

    Returnerar drive-id:n, permanent misslyckade drive-id:n, sha1 → filnamn och
    befintliga filstammar. ``rebuild`` tömmer manifestet och filstammarna.
    """
    download_rows = state_db.list_downloads_for_source(conn, source)
    manifest_ids = {
        r["drive_id"] for r in download_rows
        if r["drive_id"] is not None and not _is_retryable_failure_note(r["note"])
    }
    perm_failed_ids = {
        r["drive_id"] for r in download_rows
        if (
            r["drive_id"] is not None
            and (r["note"] or "").startswith("failed:")
            and not _is_retryable_failure_note(r["note"])
        )
    }
    manifest_sha1s = {
        r["sha1"]: r["filename"] for r in download_rows
        if r["sha1"] is not None
    }

    # Filnamns-stem-fallback för befintliga nedladdningar (pre-manifest)
    existing_stems = (
        {p.with_suffix("").name for p in out.iterdir() if p.is_file()}
        if out.is_dir() else set()
    )

    if rebuild:
        # --rebuild: hämta om allt, även poster i manifestet, filer som redan
        # ligger på disk och tidigare permanenta felnoter.
        ctx.log("Ladda ned igen: manifestet ignoreras — alla filer hämtas om.")
        manifest_ids = set()
        existing_stems = set()
    return manifest_ids, perm_failed_ids, manifest_sha1s, existing_stems


def _hamta_fil(
    conn,
    source: str,
    out: Path,
    prefix: str,
    file_id: str,
    session: requests.Session,
    lage: _Nedladdningslage,
    ctx: Any,
) -> None:
    """Hämta en PDF till en temporär fil och spara den under sitt namn, eller registrera felet.

    Dubbletter (samma sha1 som en tidigare fil) tas bort och noteras i state.db.
    Permanenta fel stämplas så att filen hoppas över vid nästa körning.
    """
    tmp = out / f"_tmp_{file_id}"
    try:
        sha1 = drive_download(file_id, tmp, session)
        size = tmp.stat().st_size
        lage.bytes_total += size

        # Dubblett-check: samma sha1 finns redan under annat namn
        if sha1 in lage.manifest_sha1s:
            other = lage.manifest_sha1s[sha1]
            ctx.log(f"  [dubblett] {prefix} == {other} (sha1={sha1[:10]})")
            log_error("download", prefix, f"duplicate sha1 of {other}")
            tmp.unlink()
            # Notera ändå i state.db så vi inte kör om
            state_db.record_download(
                conn, source=source, drive_id=file_id,
                filename=other, sha1=sha1, bytes_=size,
                note=f"dup-of:{other}",
            )
            lage.manifest_ids.add(file_id)
            lage.n_dup += 1
        else:
            ext = sniff_extension(tmp) or ".bin"
            dst = out / f"{prefix}{ext}"
            i = 2
            while dst.exists():
                dst = out / f"{prefix} ({i}){ext}"
                i += 1
            tmp.rename(dst)
            lage.existing_stems.add(dst.with_suffix("").name)
            state_db.record_download(
                conn, source=source, drive_id=file_id,
                filename=dst.name, sha1=sha1, bytes_=size,
            )
            state_db.upsert_pdf_file(
                conn, pdf_stem=dst.stem, source=source,
                pdf_path=str(dst),
            )
            lage.manifest_ids.add(file_id)
            lage.manifest_sha1s[sha1] = dst.name
            lage.n_new += 1
    except Exception as e:
        lage.n_fail += 1
        err_str = str(e)
        ctx.log(f"  FEL: {err_str}", level="error")
        log_error("download", prefix, err_str)
        lage.failed.append((prefix, file_id, err_str))
        if tmp.exists():
            with suppress(OSError):
                tmp.unlink()
        # Permanenta fel registreras i state.db så att filen hoppas över
        # vid nästa körning. HTML-svar från Drive är avsiktligt retrybara:
        # de kan vara rate-limit lika gärna som borttagna filer.
        note = _permanent_failure_note(e)
        if note:
            state_db.record_download(
                conn, source=source, drive_id=file_id,
                filename=prefix, note=note,
            )
            lage.manifest_ids.add(file_id)


def _avsluta_nedladdning(
    lage: _Nedladdningslage, todo: list[tuple[str, str]], ctx: Any
) -> int:
    """Logga sammanfattningen och returnera exitkoden: 2 om någon fil misslyckades."""
    n_perm_skip = sum(1 for _, fid in todo if fid in lage.perm_failed_ids)
    n_already = lage.n_done - n_perm_skip

    summary_parts = []
    if lage.n_new:
        summary_parts.append(f"{lage.n_new} nya")
    if lage.n_dup:
        summary_parts.append(f"{lage.n_dup} dubbletter")
    if n_already:
        summary_parts.append(f"{n_already} redan hämtade")
    if n_perm_skip:
        summary_parts.append(f"{n_perm_skip} permanent misslyckade (skippas)")

    if lage.failed:
        ctx.log(f"\n{len(lage.failed)} filer misslyckades:", level="error")
        for prefix, fid, err in lage.failed:
            ctx.log(f"  {fid}  {prefix}: {err}", level="error")
        if summary_parts:
            ctx.log(f"Övrigt: {', '.join(summary_parts)}.")
        return 2
    ctx.log(f"Klart. {', '.join(summary_parts)}." if summary_parts else "Klart.")
    return 0


def run_download(*, out: Path, sheet_id: str, limit: int, dry_run: bool = False,
                 rebuild: bool = False, context=None) -> int:
    """Ladda ned arkivets PDF:er till ``out``. Idempotent via state.db.

    ``dry_run`` listar bara vad som skulle hämtas. ``rebuild`` ignorerar
    manifestet och hämtar om filerna — även sådana som saknas på disk eller
    tidigare stämplats som permanent misslyckade.
    """
    ctx = _ctx(context)
    if not dry_run:
        out.mkdir(parents=True, exist_ok=True)

    conn = state_db.connect()
    state_db.init_schema(conn)
    source = "wpu" if out.name == "wpu_files" else "files"

    rows = _hamta_kalkylrader(sheet_id, out, ctx)
    todo = _bygg_nedladdningslista(rows, ctx)
    if todo is None:
        return 1

    ctx.log(f"Hittade {len(todo)} filer")
    if limit:
        todo = todo[:limit]
        ctx.log(f"Test-läge: begränsar till {limit} filer")

    manifest_ids, perm_failed_ids, manifest_sha1s, existing_stems = _las_manifest(
        conn, source, out, rebuild, ctx
    )
    total = len(todo)
    n_done = sum(
        1 for prefix, fid in todo
        if fid in manifest_ids or prefix in existing_stems
    )
    if dry_run:
        remaining = [
            prefix for prefix, fid in todo
            if fid not in manifest_ids and prefix not in existing_stems
        ]
        ctx.log(
            f"Lista: {len(remaining)} av {total} filer skulle hämtas "
            f"({n_done} redan hämtade)."
        )
        for prefix in remaining:
            ctx.log(f"  [skulle hämtas] {prefix}")
        return 0

    lage = _Nedladdningslage(
        total=total,
        t0=time.monotonic(),
        n_done=n_done,
        manifest_ids=manifest_ids,
        perm_failed_ids=perm_failed_ids,
        manifest_sha1s=manifest_sha1s,
        existing_stems=existing_stems,
    )
    session = requests.Session()
    for prefix, file_id in todo:
        ctx.check_cancelled()
        # Idempotency 1: manifestet (drive_id)
        if file_id in lage.manifest_ids:
            continue
        # Idempotency 2: filnamns-stem (pre-manifest)
        if prefix in lage.existing_stems:
            continue

        _logga_framsteg(lage, ctx, "[hämtar]   ", prefix, f"  id={file_id}")
        _hamta_fil(conn, source, out, prefix, file_id, session, lage, ctx)
        # Liten paus för att undvika throttling
        time.sleep(0.1)

    return _avsluta_nedladdning(lage, todo, ctx)


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

    return run_download(
        out=Path(args.out_flag or args.out),
        sheet_id=args.sheet_id,
        limit=args.limit,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
