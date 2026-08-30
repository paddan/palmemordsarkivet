"""Sätt upp projekt-lokal tessdata-katalog med swe_best.traineddata.

Root-katalogen bestäms av --root, env ROOT eller (default) repo-roten
(föräldern till scripts/). Målkatalogen bestäms av --dest, env DEST eller
<root>/tessdata.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
SWE_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/swe.traineddata"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sätt upp projekt-lokal tessdata-katalog med swe_best.traineddata.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.environ["ROOT"]) if os.environ.get("ROOT") else DEFAULT_ROOT,
        help=f"projektrot (env ROOT eller default: {DEFAULT_ROOT})",
    )
    parser.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="målkatalog för tessdata (env DEST eller <root>/tessdata)",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    root: Path = args.root
    dest = args.dest
    if dest is None:
        dest = Path(os.environ["DEST"]) if os.environ.get("DEST") else root / "tessdata"
    dest.mkdir(parents=True, exist_ok=True)

    sys_tessdata = Path(subprocess.check_output(["brew", "--prefix"]).decode().strip()) / "share" / "tessdata"

    for src in sorted(sys_tessdata.iterdir()):
        if src.name in (".", "..", "swe.traineddata"):
            continue
        link = dest / src.name
        if not link.exists():
            link.symlink_to(src)
    print(f"symlänkade systemfiler från {sys_tessdata}")

    swe = dest / "swe.traineddata"
    if swe.is_file() and not swe.is_symlink():
        print(f"swe.traineddata finns redan ({swe.stat().st_size} byte)")
    else:
        swe.unlink(missing_ok=True)
        print("Hämtar swe_best.traineddata från tessdata_best…")
        urllib.request.urlretrieve(SWE_URL, swe)
        print(f"Klart: {swe.stat().st_size} byte")

    print(f"\ntessdata-katalog: {dest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
