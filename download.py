#!/usr/bin/env python3
"""
Ladda ner alla filer från palmemordsarkivet.se (publikt Google Sheet)
via länkarna i kolumnen "Länk till kopia".

Användning:
    pip install gdown requests
    python download.py [målmapp]
"""

import csv
import io
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

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
]


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


def drive_download(file_id: str, dest: Path, session: requests.Session) -> None:
    """Hämta en publik Drive-fil. Hanterar virusscan-bekräftelsen för stora filer."""
    url = "https://drive.usercontent.google.com/download"
    params = {"id": file_id, "export": "download"}
    r = session.get(url, params=params, stream=True, timeout=120)
    r.raise_for_status()

    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        # Virusscan-bekräftelse: parsa formulärets dolda fält och POST:a
        body = r.content.decode("utf-8", errors="replace")
        action = re.search(r'action="([^"]+)"', body)
        fields = dict(re.findall(r'name="([^"]+)"\s+value="([^"]*)"', body))
        if not action or not fields:
            raise RuntimeError("kunde inte hantera Drive-bekräftelsesidan")
        r = session.get(action.group(1), params=fields, stream=True, timeout=300)
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError("fick HTML i andra svaret också (rate limit?)")

    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 15):
            if chunk:
                f.write(chunk)


def sniff_extension(path: Path) -> str:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return ""
    for sig, ext in MAGIC:
        if head.startswith(sig):
            return ext
    return ""


def main() -> int:
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "files")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Hämtar kalkylbladet från {CSV_URL}")
    r = requests.get(CSV_URL, timeout=60)
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

    print(f"Hittade {len(todo)} filer att ladda ner till {out_dir}/")

    # Snabb-sök redan nedladdade (matchar på basnamnet utan ext)
    existing_stems = {p.with_suffix("").name for p in out_dir.iterdir()}

    session = requests.Session()
    failed = []
    total = len(todo)
    n_done = sum(1 for prefix, _ in todo if prefix in existing_stems)
    n_new = 0
    n_fail = 0
    bytes_total = 0
    t0 = time.monotonic()

    def fmt_dur(secs: float) -> str:
        secs = int(secs)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"

    def progress(tag: str, prefix: str, extra: str = "") -> None:
        i = n_done + n_new + n_fail
        pct = 100 * i / total if total else 0
        elapsed = time.monotonic() - t0
        rate = n_new / elapsed if elapsed > 0 and n_new else 0
        remaining = total - i
        if rate > 0:
            eta_secs = remaining / rate
            finish = datetime.now() + timedelta(seconds=eta_secs)
            # visa datum bara om det inte är idag
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
        if prefix in existing_stems:
            progress("[hoppar]   ", prefix)
            continue
        progress("[hämtar]   ", prefix, f"  id={file_id}")
        tmp = out_dir / f"_tmp_{file_id}"
        try:
            drive_download(file_id, tmp, session)
            bytes_total += tmp.stat().st_size
            ext = sniff_extension(tmp) or ".bin"
            dst = out_dir / f"{prefix}{ext}"
            i = 2
            while dst.exists():
                dst = out_dir / f"{prefix} ({i}){ext}"
                i += 1
            tmp.rename(dst)
            existing_stems.add(dst.with_suffix("").name)
            n_new += 1
        except Exception as e:
            n_fail += 1
            print(f"  FEL: {e}", file=sys.stderr, flush=True)
            failed.append((prefix, file_id, str(e)))
            if tmp.exists():
                tmp.unlink()

    if failed:
        print(f"\n{len(failed)} filer misslyckades:")
        for prefix, fid, err in failed:
            print(f"  {fid}  {prefix}: {err}")
        return 2
    print("Klart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
