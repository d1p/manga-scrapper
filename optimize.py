#!/usr/bin/env python3
"""Recursively optimize CBZ files for Kindle 2022 (6-inch e-ink, 1072x1448, 300 PPI).

Usage:
  python optimize.py --input "C:/manga/library"
"""

import argparse
import shutil
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image
from tqdm import tqdm

KINDLE_MAX_W = 1072
KINDLE_MAX_H = 1448
BORDER_CROP_SENSITIVITY = 0.98
JPEG_QUALITY = 90


def crop_borders(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    threshold = int(255 * BORDER_CROP_SENSITIVITY)
    bbox = gray.point(lambda p: 255 if p >= threshold else 0).getbbox()
    if bbox and bbox != (0, 0, img.width, img.height):
        return img.crop(bbox)
    return img


def optimize_bytes(data: bytes, arcname: str) -> tuple[str, bytes]:
    """Return (arcname, optimized_bytes) for a single image entry in a CBZ."""
    import io

    with Image.open(io.BytesIO(data)) as img:
        img = crop_borders(img)
        img = img.convert("L")
        if img.width > KINDLE_MAX_W or img.height > KINDLE_MAX_H:
            ratio = min(KINDLE_MAX_W / img.width, KINDLE_MAX_H / img.height)
            img = img.resize(
                (int(img.width * ratio), int(img.height * ratio)),
                Image.LANCZOS,
            )
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        new_name = str(Path(arcname).with_suffix(".jpg"))
        return (new_name, buf.getvalue())


def process_cbz(cbz_path: Path, image_workers: int = 4) -> int:
    """Optimize a single CBZ in-place. Returns number of images processed."""
    tmp_path = cbz_path.with_suffix(".tmp")

    try:
        with zipfile.ZipFile(cbz_path, "r") as zf_in:
            names = [n for n in zf_in.namelist()
                     if not n.endswith("/") and Path(n).suffix.lower()
                     in (".jpg", ".jpeg", ".png", ".webp")]

            if not names:
                return 0

            raw_entries = [(n, zf_in.read(n)) for n in names]

        ordered = [None] * len(raw_entries)

        def _task(idx, arcname, data):
            try:
                ordered[idx] = optimize_bytes(data, arcname)
            except Exception:
                ordered[idx] = (arcname, data)

        with ThreadPoolExecutor(max_workers=image_workers) as pool:
            futures = [
                pool.submit(_task, i, n, d) for i, (n, d) in enumerate(raw_entries)
            ]
            for f in as_completed(futures):
                f.result()

        entries = [e for e in ordered if e is not None]

        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_out:
            for arcname, data in entries:
                zf_out.writestr(arcname, data)

        tmp_path.replace(cbz_path)
        return len(entries)

    except Exception as e:
        tqdm.write(f"  Failed: {cbz_path.name} - {e}")
        if tmp_path.exists():
            tmp_path.unlink()
        return 0


def main():
    parser = argparse.ArgumentParser(
        description="Optimize CBZ files for Kindle 2022 (grayscale, border-crop, 1072x1448)"
    )
    parser.add_argument("--input", required=True,
                        help="Directory containing CBZ files (recursively)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers for CBZ-level and image-level processing (default: 4)")
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        print(f"Error: '{input_dir}' is not a directory.")
        sys.exit(1)

    cbz_files = sorted(input_dir.rglob("*.cbz"))
    if not cbz_files:
        print(f"No .cbz files found in '{input_dir}'")
        return

    print(f"Found {len(cbz_files)} CBZ file(s) in {input_dir}")
    print(f"  Kindle 2022: {KINDLE_MAX_W}x{KINDLE_MAX_H}, 300 PPI, grayscale\n")

    stats = {"ok": 0, "fail": 0, "pages": 0}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_cbz, p, args.workers): p for p in cbz_files}
        with tqdm(total=len(cbz_files), desc="Optimizing", unit="cbz") as pbar:
            for f in as_completed(futures):
                path = futures[f]
                try:
                    pages = f.result()
                    if pages:
                        stats["ok"] += 1
                        stats["pages"] += pages
                    else:
                        stats["fail"] += 1
                except Exception as e:
                    tqdm.write(f"  Failed: {path.name} - {e}")
                    stats["fail"] += 1
                pbar.set_postfix(ok=stats["ok"], fail=stats["fail"])
                pbar.update(1)

    print(f"\nDone. {stats['ok']} CBZ(s) optimized ({stats['pages']} pages)."
          + (f"  {stats['fail']} failed." if stats["fail"] else ""))


if __name__ == "__main__":
    main()
