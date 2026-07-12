#!/usr/bin/env python3
"""Organize CBZ files into a Kavita-compatible library structure.

    python kavita.py --input ./manga_output/MySeries --output D:/manga
"""

import argparse
import shutil
import sys
from pathlib import Path

from core import sanitize_name

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def organize(input_dir: str | Path, output_dir: str | Path):
    input_dir = Path(input_dir)
    output_dir = Path(output_dir)
    series = sanitize_name(input_dir.name)

    cbz_files = sorted(input_dir.glob("*.cbz"))
    if not cbz_files:
        print(f"No CBZ files found in {input_dir}")
        return

    dest = output_dir / series
    dest.mkdir(parents=True, exist_ok=True)
    for cbz in cbz_files:
        shutil.copy2(cbz, dest / cbz.name)

    print(f"Exported {len(cbz_files)} file(s) to {output_dir.resolve()}")
    for f in sorted(dest.glob("*.cbz")):
        print(f"  {f.relative_to(output_dir)}")


def main():
    parser = argparse.ArgumentParser(description="Organize CBZ files for Kavita")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    organize(args.input, args.output)


if __name__ == "__main__":
    main()
