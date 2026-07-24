#!/usr/bin/env python3
"""Build CBZ volumes from local manga chapter folders."""

import os
import re
import shutil
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image

from core import build_cbz_volumes

SUPPORTED_FORMATS = (".jpg", ".jpeg", ".png", ".webp")
lock = threading.Lock()


class AppState:
    def __init__(self):
        self.chapters = {}
        self.volumes = {}
        self.total_pages = 0
        self.done_pages = 0


STATE = AppState()


def find_chapter_dirs(input_dir: Path) -> list[tuple[float, Path]]:
    chapters = []
    for path in sorted(input_dir.iterdir()):
        if path.is_dir():
            m = re.search(r"chapter[\s\-_]*(\d+(?:\.\d+)?)", path.name.lower())
            if m:
                chapters.append((float(m.group(1)), path))
    return sorted(chapters, key=lambda x: x[0])


def process_image(src: Path, dst: Path) -> str:
    if dst.exists():
        return "cached"
    try:
        with Image.open(src) as img:
            has_alpha = img.mode == "RGBA"
            out = dst.with_suffix(".png") if has_alpha else dst
            img.save(out, "PNG" if has_alpha else "JPEG", quality=90)
        return "processed"
    except Exception:
        shutil.copy(src, dst)
        return "processed"


def render_ui():
    os.system("cls" if os.name == "nt" else "clear")
    pct = STATE.done_pages / STATE.total_pages * 100 if STATE.total_pages else 0
    print(f"=== Manga Builder ===\n\nProgress: {pct:.1f}% ({STATE.done_pages}/{STATE.total_pages})\n")
    for ch in sorted(STATE.chapters.values(), key=lambda c: c["num"]):
        icon = {"done": "OK", "processing": "...", "cached": "C", "pending": "-"}.get(ch["status"], "?")
        print(f"  Ch.{ch['num']:g} [{icon}]")


def process_chapter(ch_num: float, folder: Path, cache_dir: Path):
    images = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_FORMATS)
    all_cached = True
    for src in images:
        dst = cache_dir / f"ch{ch_num}_{src.name}"
        result = process_image(src, dst)
        with lock:
            STATE.chapters[ch_num]["done"] += 1
            STATE.done_pages += 1
            STATE.chapters[ch_num]["status"] = "processing"
        if result != "cached":
            all_cached = False
        render_ui()
    with lock:
        STATE.chapters[ch_num]["status"] = "cached" if all_cached else "done"
        render_ui()


def main():
    parser = argparse.ArgumentParser(description="Build CBZ volumes from chapter folders")
    parser.add_argument("--input", default="input_manga")
    parser.add_argument("--output", default="output_cbz")
    parser.add_argument("--cache", default="cache_images")
    parser.add_argument("--max-vol-mb", type=int, default=150)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    input_dir = Path(args.input)
    output_dir = Path(args.output)
    cache_dir = Path(args.cache)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    chapters = find_chapter_dirs(input_dir)
    for ch_num, folder in chapters:
        images = sorted(p for p in folder.iterdir() if p.suffix.lower() in SUPPORTED_FORMATS)
        STATE.chapters[ch_num] = {"num": ch_num, "total": len(images), "done": 0, "status": "pending"}
        STATE.total_pages += len(images)

    render_ui()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(process_chapter, n, d, cache_dir) for n, d in chapters]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"Error: {e}")

    series = input_dir.name
    build_cbz_volumes(series, cache_dir, output_dir, args.max_vol_mb)

    print("\nDone.")


if __name__ == "__main__":
    main()
