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

from core import build_cbz_volumes  # noqa: F401 - used if ever CLI-less

SUPPORTED_FORMATS = (".jpg", ".jpeg", ".png", ".webp")
lock = threading.Lock()


class AppState:
    def __init__(self):
        self.chapters = {}
        self.volumes = {}
        self.total_pages = 0
        self.done_pages = 0


STATE = AppState()


def find_chapter_dirs(input_dir):
    chapters = []
    for folder in sorted(os.listdir(input_dir)):
        path = os.path.join(input_dir, folder)
        if os.path.isdir(path):
            m = re.search(r"chapter[\s\-_]*(\d+(?:\.\d+)?)", folder.lower())
            if m:
                chapters.append((float(m.group(1)), path))
    return sorted(chapters, key=lambda x: x[0])


def process_image(src, dst):
    if os.path.exists(dst):
        return "cached"
    try:
        img = Image.open(src)
        has_alpha = img.mode == "RGBA"
        if has_alpha:
            dst = os.path.splitext(dst)[0] + ".png"
        img.save(dst, "PNG" if has_alpha else "JPEG", quality=90)
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


def process_chapter(ch_num, folder, cache_dir):
    images = sorted(f for f in os.listdir(folder) if f.lower().endswith(SUPPORTED_FORMATS))
    all_cached = True
    for name in images:
        src = os.path.join(folder, name)
        dst = os.path.join(cache_dir, f"ch{ch_num}_{name}")
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
    parser.add_argument("--max-vol-mb", type=int, default=300)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    os.makedirs(args.cache, exist_ok=True)

    chapters = find_chapter_dirs(args.input)
    for ch_num, folder in chapters:
        images = sorted(f for f in os.listdir(folder) if f.lower().endswith(SUPPORTED_FORMATS))
        STATE.chapters[ch_num] = {"num": ch_num, "total": len(images), "done": 0, "status": "pending"}
        STATE.total_pages += len(images)

    render_ui()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(process_chapter, n, d, args.cache) for n, d in chapters]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"Error: {e}")

    # Copy cached images to an opt-style directory for build_cbz_volumes
    opt_dir = Path(args.cache)
    series = Path(args.input).name
    build_cbz_volumes(series, opt_dir, Path(args.output), args.max_vol_mb)

    print("\nDone.")


if __name__ == "__main__":
    main()
