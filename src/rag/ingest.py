#!/usr/bin/env python3
"""
Indexera textfiler från ../text/ till en lokal LanceDB-vektor-DB.

Kör:
    python ingest.py            # indexera alla nya filer
    python ingest.py --rebuild  # börja om från noll
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parents[2]
TEXT_DIR = ROOT / "text"
DB_DIR = ROOT / "rag" / "lancedb"
TABLE = "chunks"
MODEL_NAME = "intfloat/multilingual-e5-large"
EMBED_DIM = 1024

CHUNK_CHARS = 800
CHUNK_OVERLAP = 150
MIN_ALNUM_RATIO = 0.55  # filtrera OCR-skräp

# Filnamn: "<Nr> — <Titel> — <Beställt> — <Upplagt> — <Anmärkning> — <Sidor>.txt"
NAME_FIELDS = ["nr", "titel", "bestallt", "upplagt", "anmarkning", "antal_sidor"]


def parse_filename(stem: str) -> dict:
    parts = [p.strip() for p in stem.split(" — ")]
    out = {f: "" for f in NAME_FIELDS}
    for f, v in zip(NAME_FIELDS, parts):
        out[f] = v
    if not out["nr"]:
        out["nr"] = stem[:40]
    return out


def split_pages(text: str) -> list[str]:
    # pdftotext -layout separerar sidor med \f
    return text.split("\f") if "\f" in text else [text]


def chunk_text(text: str, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Returnerar (start, end, chunk) — bryter helst på radslut.

    OBS: chunkar INOM en sida; ``\f`` får aldrig förekomma i text som skickas hit.
    Korta sidor blir egna (en) chunk.
    """
    text = text.strip()
    if not text:
        return []
    if "\f" in text:
        # defensiv: callern ska ha splittat först
        text = text.replace("\f", " ")
    if len(text) <= size:
        return [(0, len(text), text)]
    chunks = []
    i = 0
    n = len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            # backa till närmsta radbrytning eller mellanslag
            window = text.rfind("\n", i + size // 2, end)
            if window == -1:
                window = text.rfind(" ", i + size // 2, end)
            if window != -1 and window > i + 100:
                end = window
        chunk = text[i:end].strip()
        if chunk:
            chunks.append((i, end, chunk))
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return chunks


def is_useful(chunk: str) -> bool:
    if len(chunk) < 80:
        return False
    alnum = sum(c.isalnum() for c in chunk)
    return alnum / len(chunk) >= MIN_ALNUM_RATIO


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rebuild", action="store_true", help="börja om från noll")
    ap.add_argument("--limit", type=int, help="max antal filer att indexera")
    ap.add_argument("--text-dir",
                    default=os.environ.get("TEXT_DIR", str(TEXT_DIR)),
                    help=f"katalog med .txt-filer (default: {TEXT_DIR})")
    ap.add_argument("--db-dir",
                    default=os.environ.get("DB_DIR", str(DB_DIR)),
                    help=f"LanceDB-katalog (default: {DB_DIR})")
    ap.add_argument("--chunk-chars", type=int,
                    default=int(os.environ.get("CHUNK_CHARS", str(CHUNK_CHARS))),
                    help=f"chunk-storlek i tecken (default: {CHUNK_CHARS})")
    ap.add_argument("--chunk-overlap", type=int,
                    default=int(os.environ.get("CHUNK_OVERLAP", str(CHUNK_OVERLAP))),
                    help=f"chunk-överlapp i tecken (default: {CHUNK_OVERLAP})")
    ap.add_argument("--model",
                    default=os.environ.get("EMBED_MODEL", MODEL_NAME),
                    help=f"embedding-modell (default: {MODEL_NAME})")
    ap.add_argument("--unusable-list",
                    default=os.environ.get("UNUSABLE_LIST",
                                           str(ROOT / "unusable.txt")),
                    help="skriv filer som producerade noll användbara chunks "
                         "till denna fil (default: unusable.txt)")
    args = ap.parse_args()

    text_dir = Path(args.text_dir)
    db_dir = Path(args.db_dir)
    if not text_dir.exists():
        print(f"Saknar {text_dir}/ — kör ocr.sh först.", file=sys.stderr)
        return 1

    db_dir.mkdir(exist_ok=True)
    db = lancedb.connect(str(db_dir))

    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("page", pa.int32()),
        pa.field("chunk_idx", pa.int32()),
        *[pa.field(f, pa.string()) for f in NAME_FIELDS],
    ])

    if args.rebuild and TABLE in db.list_tables().tables:
        db.drop_table(TABLE)
    if TABLE in db.list_tables().tables:
        table = db.open_table(TABLE)
        try:
            # Effektivt: lance-scanner laddar bara source utan att läsa vektorer
            already = set(
                table.to_lance().to_table(columns=["source"]).column("source").to_pylist()
            )
        except ImportError:
            # lance inte installerat (vanligt på Python 3.14): full scan via pandas
            already = set(table.to_pandas()["source"].tolist())
    else:
        table = db.create_table(TABLE, schema=schema)
        already = set()

    print(f"Laddar embedding-modell {args.model} (första gången tar några minuter)…")
    model = SentenceTransformer(args.model)

    files = sorted(text_dir.glob("*.txt"))
    if args.limit:
        files = files[: args.limit]

    todo = [f for f in files if f.name not in already]
    print(f"Indexerar {len(todo)} av {len(files)} filer (skippar {len(files) - len(todo)} redan indexerade).")

    t0 = time.monotonic()
    total_chunks = 0
    unusable: list[str] = []

    for i, f in enumerate(todo, 1):
        meta = parse_filename(f.stem)
        try:
            raw = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  [{i}/{len(todo)}] SKIP {f.name}: {e}")
            continue

        rows = []
        chunk_idx = 0
        for page_idx, page in enumerate(split_pages(raw), start=1):
            for _, _, chunk in chunk_text(page, args.chunk_chars, args.chunk_overlap):
                if not is_useful(chunk):
                    continue
                rows.append({
                    "text": chunk,
                    "source": f.name,
                    "page": page_idx,
                    "chunk_idx": chunk_idx,
                    **meta,
                })
                chunk_idx += 1

        if not rows:
            print(f"  [{i}/{len(todo)}] {f.name}: inga användbara chunks")
            unusable.append(f.name)
            continue

        # e5 vill ha "passage: " på dokument
        embeddings = model.encode(
            [f"passage: {r['text']}" for r in rows],
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        for idx, v in enumerate(embeddings):
            if not all(math.isfinite(x) for x in v):
                raise ValueError(f"{f.name} chunk {idx}: embedding innehåller NaN/Inf")
            if not any(v):
                raise ValueError(f"{f.name} chunk {idx}: embedding är all-nollor")
        for r, v in zip(rows, embeddings):
            r["vector"] = v.tolist()

        table.add(rows)
        total_chunks += len(rows)

        elapsed = time.monotonic() - t0
        rate = i / elapsed if elapsed else 0
        eta = (len(todo) - i) / rate if rate else 0
        print(
            f"  [{i:>4}/{len(todo)}] {f.name[:60]:60s} "
            f"+{len(rows):>3} chunks (totalt {total_chunks}, eta {int(eta // 60)}m{int(eta % 60):02d}s)"
        )

    # Bygg/uppdatera FTS-index (BM25). Kräver lancedb med tantivy/native FTS.
    try:
        table.create_fts_index("text", replace=True)
        print("✓ FTS-index uppdaterat (BM25 på 'text') — hybridsök tillgängligt.")
    except Exception as e:  # noqa: BLE001
        print(
            f"⚠ FTS-index kunde inte skapas ({e}).\n"
            f"  Hybridsök (--hybrid) fungerar inte förrän det fixas.\n"
            f"  Prova: pip install --upgrade lancedb",
            file=sys.stderr,
        )

    print(f"\nKlart. Tabell '{TABLE}' har {table.count_rows()} chunks.")

    if unusable:
        unusable_path = Path(args.unusable_list)
        unusable_path.write_text("\n".join(unusable) + "\n", encoding="utf-8")
        print(
            f"\n{len(unusable)} filer producerade noll användbara chunks — "
            f"skrivna till {unusable_path}.\n"
            f"Kör om OCR med:  ./ocr.sh --redo --mode files "
            f"--from-list {unusable_path.name}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
