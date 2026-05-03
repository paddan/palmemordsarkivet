#!/usr/bin/env python3
"""
Indexera textfiler från ../text/ till en lokal LanceDB-vektor-DB.

Kör:
    python ingest.py            # indexera alla nya filer
    python ingest.py --rebuild  # börja om från noll
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

ROOT = Path(__file__).resolve().parent.parent
TEXT_DIR = ROOT / "text"
DB_DIR = Path(__file__).resolve().parent / "lancedb"
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
    """Returnerar (start, end, chunk) — bryter helst på radslut."""
    text = text.strip()
    if not text:
        return []
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="börja om från noll")
    ap.add_argument("--limit", type=int, help="max antal filer att indexera")
    args = ap.parse_args()

    if not TEXT_DIR.exists():
        print(f"Saknar {TEXT_DIR}/ — kör ocr.sh först.", file=sys.stderr)
        return 1

    DB_DIR.mkdir(exist_ok=True)
    db = lancedb.connect(str(DB_DIR))

    schema = pa.schema([
        pa.field("vector", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("text", pa.string()),
        pa.field("source", pa.string()),
        pa.field("page", pa.int32()),
        pa.field("chunk_idx", pa.int32()),
        *[pa.field(f, pa.string()) for f in NAME_FIELDS],
    ])

    if args.rebuild and TABLE in db.table_names():
        db.drop_table(TABLE)
    if TABLE in db.table_names():
        table = db.open_table(TABLE)
        already = {r["source"] for r in table.search().select(["source"]).limit(10**9).to_list()}
    else:
        table = db.create_table(TABLE, schema=schema)
        already = set()

    print(f"Laddar embedding-modell {MODEL_NAME} (första gången tar några minuter)…")
    model = SentenceTransformer(MODEL_NAME)

    files = sorted(TEXT_DIR.glob("*.txt"))
    if args.limit:
        files = files[: args.limit]

    todo = [f for f in files if f.name not in already]
    print(f"Indexerar {len(todo)} av {len(files)} filer (skippar {len(files) - len(todo)} redan indexerade).")

    t0 = time.monotonic()
    total_chunks = 0

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
            for _, _, chunk in chunk_text(page, CHUNK_CHARS, CHUNK_OVERLAP):
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
            continue

        # e5 vill ha "passage: " på dokument
        embeddings = model.encode(
            [f"passage: {r['text']}" for r in rows],
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
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

    print(f"\nKlart. Tabell '{TABLE}' har {table.count_rows()} chunks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
