"""Engångsmigrering: läs befintliga filer i downloaded/ och generated/ och
fyll state.db. Idempotent (UPSERT) — kan köras flera gånger.

Källor:
  downloaded/{files,wpu_files}/manifest.csv → downloads-tabellen
  downloaded/{files,wpu_files}/*.pdf        → pdf_files-tabellen
  generated/text/<stem>.txt                 → pdf_files.text_mtime + merged_at
  generated/text/.normalize_stamp           → pdf_files.normalized_at (best-effort)
  generated/text/<stem>.redact              → pdf_files.has_redactions
  generated/text_pages/<stem>/page-*.json   → pdf_pages-tabellen
  generated/quality.csv                     → quality-tabellen
  generated/quality_pages.jsonl             → quality_pages-tabellen
  generated/lancedb/chunks                  → ingest-tabellen
  generated/text_wpu/*.done                 → wpu_decisions-tabellen
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

import db

ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parents[1])


def _migrate_downloads(conn, root: Path) -> int:
    n = 0
    for source, subdir in (("files", "files"), ("wpu", "wpu_files")):
        manifest = root / "downloaded" / subdir / "manifest.csv"
        if not manifest.exists():
            continue
        with manifest.open(encoding="utf-8", newline="") as fp:
            for row in csv.DictReader(fp):
                drive_id = row.get("drive_id") or None
                url = row.get("url") or None
                if not drive_id and not url:
                    continue
                db.record_download(
                    conn, source=source, drive_id=drive_id, url=url,
                    filename=row.get("filename", ""),
                    sha1=row.get("sha1") or None,
                    bytes_=int(row["bytes"]) if row.get("bytes") else None,
                )
                n += 1
    return n


def _migrate_pdf_files(conn, root: Path) -> int:
    n = 0
    for source, subdir in (("files", "files"), ("wpu", "wpu_files")):
        pdf_dir = root / "downloaded" / subdir
        if not pdf_dir.exists():
            continue
        for pdf in sorted(pdf_dir.glob("*.pdf")):
            db.upsert_pdf_file(
                conn, pdf_stem=pdf.stem, source=source,
                pdf_path=str(pdf.relative_to(root)),
            )
            n += 1
    return n


def _migrate_text_and_redactions(conn, root: Path) -> None:
    """Sätt text_mtime + merged_at från text/<stem>.txt, normalized_at
    från .normalize_stamp (best-effort), has_redactions från .redact-markörer.
    Måste köras före _migrate_quality (som läser pdf_files.text_mtime).
    """
    text_dir = root / "generated" / "text"
    if not text_dir.exists():
        return
    stamp = text_dir / ".normalize_stamp"
    stamp_mtime = stamp.stat().st_mtime if stamp.exists() else None

    for txt in text_dir.glob("*.txt"):
        stem = txt.stem
        if db.get_pdf_file(conn, stem) is None:
            continue
        mtime = txt.stat().st_mtime
        db.mark_merged(conn, stem, text_mtime=mtime)
        if stamp_mtime is not None and mtime <= stamp_mtime:
            db.mark_normalized(conn, stem, text_mtime=mtime)

    for r in text_dir.glob("*.redact"):
        stem = r.stem
        if db.get_pdf_file(conn, stem) is not None:
            db.mark_redaction_checked(conn, stem, has_redactions=True)


def _migrate_pages(conn, root: Path) -> int:
    pages_root = root / "generated" / "text_pages"
    if not pages_root.exists():
        return 0
    n = 0
    for stem_dir in sorted(pages_root.iterdir()):
        if not stem_dir.is_dir():
            continue
        stem = stem_dir.name
        if db.get_pdf_file(conn, stem) is None:
            continue
        for j in sorted(stem_dir.glob("page-*.json")):
            try:
                meta = json.loads(j.read_text(encoding="utf-8"))
            except Exception:
                continue
            page_num = int(meta.get("page", 0))
            if page_num < 1:
                continue
            txt_path = j.with_suffix(".txt")
            text = txt_path.read_text(encoding="utf-8", errors="replace") \
                if txt_path.exists() else None
            db.record_page(
                conn, pdf_stem=stem, page_num=page_num,
                engine=meta.get("engine", "tesseract"),
                text=text, score=meta.get("score"),
            )
            n += 1
    return n


def _migrate_quality(conn, root: Path) -> int:
    csv_path = root / "generated" / "quality.csv"
    if not csv_path.exists():
        return 0
    text_dir = root / "generated" / "text"
    n = 0
    with csv_path.open(encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            fname = row.get("file", "")
            stem = Path(fname).stem
            if db.get_pdf_file(conn, stem) is None:
                continue
            txt = text_dir / fname
            text_mtime = txt.stat().st_mtime if txt.exists() else 0.0
            extras = {k: (float(row[k]) if row.get(k) else None)
                      for k in ("pct_swe", "junk_ratio", "short_word_ratio",
                                "long_word_ratio", "digit_in_word_ratio",
                                "avg_word_len", "vowel_ratio")}
            extras["source_type"] = row.get("source")
            db.record_quality(
                conn, pdf_stem=stem,
                score=float(row.get("score") or 0),
                chars=int(row.get("chars") or 0),
                text_mtime=text_mtime,
                extras=extras,
            )
            n += 1
    return n


def _migrate_quality_pages(conn, root: Path) -> int:
    jsonl = root / "generated" / "quality_pages.jsonl"
    if not jsonl.exists():
        return 0
    n = 0
    with jsonl.open(encoding="utf-8") as fp:
        for line in fp:
            try:
                d = json.loads(line)
            except Exception:
                continue
            stem = Path(d.get("file", "")).stem
            page = int(d.get("page", 0))
            if not stem or page < 1:
                continue
            if db.get_pdf_file(conn, stem) is None:
                continue
            db.record_quality_page(
                conn, pdf_stem=stem, page_num=page,
                score=float(d.get("score") or 0),
                chars=int(d.get("chars") or 0) if d.get("chars") else None,
                image_page=bool(d.get("image_page")),
                payload=d,
            )
            n += 1
    return n


def _migrate_ingest(conn, root: Path) -> int:
    """Läs (source, mtime) från LanceDB-tabellen om den finns."""
    db_dir = root / "generated" / "lancedb"
    if not db_dir.exists():
        return 0
    try:
        import lancedb  # type: ignore
    except ImportError:
        return 0
    try:
        ldb = lancedb.connect(str(db_dir))
        listing = ldb.list_tables()
        table_names = listing.tables if hasattr(listing, "tables") else list(listing)
        if "chunks" not in table_names:
            return 0
        tbl = ldb.open_table("chunks")
        # Försök först snabba lance-scanner-vägen, fallback till pandas om
        # `pylance` saknas (LanceDB lazy-importerar lance internt).
        try:
            arrow = tbl.to_lance().to_table(columns=["source", "mtime"])
            sources = arrow.column("source").to_pylist()
            mtimes = arrow.column("mtime").to_pylist()
        except ImportError:
            df = tbl.to_pandas()
            sources = df["source"].tolist()
            mtimes = df["mtime"].tolist()
    except Exception as e:
        print(f"  [_migrate_ingest] LanceDB-läsning misslyckades: {e}",
              file=sys.stderr)
        return 0

    per_stem: dict[str, tuple[float, int]] = {}
    for s, m in zip(sources, mtimes):
        stem = Path(s).stem
        prev = per_stem.get(stem)
        if not prev:
            per_stem[stem] = (float(m), 1)
        else:
            mtime_max = max(prev[0], float(m))
            per_stem[stem] = (mtime_max, prev[1] + 1)

    n = 0
    for stem, (m, chunks) in per_stem.items():
        if db.get_pdf_file(conn, stem) is None:
            continue
        if m <= 0:
            continue
        db.record_ingest(conn, pdf_stem=stem, text_mtime=m, chunks=chunks)
        n += 1
    return n


def _migrate_wpu_decisions(conn, root: Path) -> int:
    """Migrera generated/text_wpu/<stem>.done → wpu_decisions-tabellen."""
    wpu_dir = root / "generated" / "text_wpu"
    if not wpu_dir.exists():
        return 0
    n = 0
    for m in wpu_dir.glob("*.done"):
        db.mark_wpu_decided(conn, m.stem)
        n += 1
    return n


def _migrate_ocr_markers(conn, root: Path) -> int:
    """Migrera generated/ocr/<stem>.ocr-done/.ocr-failed → pdf_files och radera filerna."""
    ocr_dir = root / "generated" / "ocr"
    if not ocr_dir.exists():
        return 0
    in_dir = root / "downloaded" / "files"
    ts = db.now()
    n = 0
    for marker in ocr_dir.glob("*.ocr-done"):
        stem = marker.stem
        pdf_path = str((in_dir / f"{stem}.pdf").relative_to(root))
        source = db.source_for_path(pdf_path)
        conn.execute(
            """
            INSERT INTO pdf_files(pdf_stem, source, pdf_path, tesseract_done_at, tesseract_failed)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(pdf_stem) DO UPDATE SET
                tesseract_done_at = COALESCE(tesseract_done_at, excluded.tesseract_done_at)
            """,
            (stem, source, pdf_path, ts),
        )
        marker.unlink()
        n += 1
    for marker in ocr_dir.glob("*.ocr-failed"):
        stem = marker.stem
        pdf_path = str((in_dir / f"{stem}.pdf").relative_to(root))
        source = db.source_for_path(pdf_path)
        conn.execute(
            """
            INSERT INTO pdf_files(pdf_stem, source, pdf_path, tesseract_failed)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(pdf_stem) DO UPDATE SET tesseract_failed = 1
            """,
            (stem, source, pdf_path),
        )
        marker.unlink()
        n += 1
    conn.commit()
    return n


def migrate(conn, root: Path) -> dict[str, int]:
    """Kör alla migreringssteg i rätt ordning."""
    n_downloads = _migrate_downloads(conn, root)
    n_pdf_files = _migrate_pdf_files(conn, root)
    _migrate_text_and_redactions(conn, root)  # biverkningar på pdf_files
    n_pages = _migrate_pages(conn, root)
    n_quality = _migrate_quality(conn, root)
    n_quality_pages = _migrate_quality_pages(conn, root)
    n_ingest = _migrate_ingest(conn, root)
    n_wpu = _migrate_wpu_decisions(conn, root)
    n_ocr_markers = _migrate_ocr_markers(conn, root)
    return {
        "downloads": n_downloads,
        "pdf_files": n_pdf_files,
        "pdf_pages": n_pages,
        "quality": n_quality,
        "quality_pages": n_quality_pages,
        "ingest": n_ingest,
        "wpu_decisions": n_wpu,
        "ocr_markers": n_ocr_markers,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--root", default=str(ROOT), help="projektrot")
    ap.add_argument("--db", default=None,
                    help="path till state.db (default: <root>/generated/db/state.db)")
    args = ap.parse_args()

    root = Path(args.root)
    db_path = Path(args.db) if args.db else root / "generated" / "db" / "state.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    stats = migrate(conn, root)
    print(f"Migrerade till {db_path}:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAvbrutet.", file=sys.stderr)
        sys.exit(130)
