#!/usr/bin/env python3
"""Core utilities shared across manga scrapers."""

import json
import logging
import re
import statistics
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from PIL import Image
from tqdm import tqdm

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
logger = logging.getLogger("manga_scraper")
logging.getLogger("urllib3").setLevel(logging.WARNING)

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.setLevel(level)
    logger.addHandler(handler)


# --------------------------------------------------------------------------
# Name sanitization
# --------------------------------------------------------------------------
def sanitize_name(text: str) -> str:
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# --------------------------------------------------------------------------
# Progress tracking
# --------------------------------------------------------------------------
class ProgressTracker:
    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.json_path = out_dir / "progress.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.json_path.exists():
            try:
                with open(self.json_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        return {"chapters": {}}

    def save(self):
        with open(self.json_path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=4)

    def is_chapter_done(self, chapter_dir: Path) -> bool:
        json_done = self.data.get("chapters", {}).get(chapter_dir.name, {}).get("status") == "done"
        file_done = (chapter_dir / ".done").exists()
        return json_done and file_done

    def mark_chapter_done(self, chapter_dir: Path, title: str, pages: int):
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / ".done").touch(exist_ok=True)
        self.data.setdefault("chapters", {})[chapter_dir.name] = {
            "title": title,
            "status": "done",
            "pages": pages,
            "timestamp": time.time(),
        }
        self.save()


# --------------------------------------------------------------------------
# Chapter label formatting
# --------------------------------------------------------------------------
def compute_chapter_padding(chapters: list[dict]) -> int:
    if not chapters:
        return 3
    max_num = max(ch["number"] for ch in chapters)
    return max(len(str(int(max_num))), 3)


def chapter_sort_label(num: Optional[float], pad: int = 4) -> str:
    if num is None:
        return "unknown"
    if num == int(num):
        return f"{int(num):0{pad}d}"
    return f"{num:0{pad + 2}.1f}"


# --------------------------------------------------------------------------
# Image download
# --------------------------------------------------------------------------
_thread_local = threading.local()


def get_thread_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session


def download_image(url: str, dest_path: Path, referer: str) -> bool:
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    try:
        resp = get_thread_session().get(
            url,
            headers={"Referer": referer, "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                f.write(chunk)
        return dest_path.stat().st_size > 0
    except Exception as e:
        logger.debug(f"  Download failed: {url[:80]}... - {e}")
        return False


def download_images(page_urls: list[dict], chapter_dir: Path, referer: str, concurrency: int) -> list[Path]:
    chapter_dir.mkdir(parents=True, exist_ok=True)
    dest_paths = [None] * len(page_urls)

    def _task(i: int, page: dict):
        url = page if isinstance(page, str) else page["url"]
        ext = Path(urlparse(url).path).suffix
        ext = ext if ext and len(ext) <= 5 else ".jpg"
        dest = chapter_dir / f"{i + 1:03d}{ext}"
        return i, (dest if download_image(url, dest, referer) else None)

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_task, i, p) for i, p in enumerate(page_urls)]
        with tqdm(total=len(page_urls), desc="  Downloading", leave=False) as pbar:
            for future in as_completed(futures):
                try:
                    i, path = future.result()
                    dest_paths[i] = path
                except Exception:
                    pass
                finally:
                    pbar.update(1)

    return [p for p in dest_paths if p is not None]


# --------------------------------------------------------------------------
# Image cleanup & optimization
# --------------------------------------------------------------------------
MIN_DIM = 300
MIN_SZ_RATIO = 0.15
MIN_AREA_RATIO = 0.15
MAX_ASPECT = 4.0


def clean_chapter_images(chapter_dir: Path, image_paths: list[Path]) -> list[Path]:
    if len(image_paths) < 3:
        return image_paths

    infos = []
    for p in image_paths:
        try:
            with Image.open(p) as img:
                infos.append({
                    "path": p, "size": p.stat().st_size,
                    "w": img.width, "h": img.height,
                    "area": img.width * img.height,
                })
        except Exception:
            pass

    if not infos:
        return image_paths

    med_sz = statistics.median([i["size"] for i in infos])
    med_area = statistics.median([i["area"] for i in infos])
    kept, rem_dir = [], chapter_dir / "_removed"

    for i in infos:
        p, w, h, sz, area = i["path"], i["w"], i["h"], i["size"], i["area"]
        is_junk = (
            (w < MIN_DIM and h < MIN_DIM)
            or (max(w, h) / max(min(w, h), 1) > MAX_ASPECT)
            or (sz < med_sz * MIN_SZ_RATIO)
            or (area < med_area * MIN_AREA_RATIO)
        )
        if is_junk:
            rem_dir.mkdir(exist_ok=True)
            try:
                p.rename(rem_dir / p.name)
            except Exception:
                pass
        else:
            kept.append(p)
    return kept


def optimize_image(raw_path: Path, opt_dir: Path, max_width: int = 1600) -> Path:
    opt_jpg = opt_dir / f"{raw_path.stem}.jpg"
    opt_png = opt_dir / f"{raw_path.stem}.png"
    if opt_jpg.exists():
        return opt_jpg
    if opt_png.exists():
        return opt_png

    try:
        with Image.open(raw_path) as img:
            has_alpha = img.mode == "RGBA"
            img = img.convert("RGBA") if has_alpha else img.convert("RGB")
            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)
            out = opt_png if has_alpha else opt_jpg
            img.save(out, format="PNG" if has_alpha else "JPEG", quality=85, optimize=True)
            return out
    except Exception as e:
        logger.debug(f"  PIL optimization failed for {raw_path.name}: {e}")
        fb = opt_dir / raw_path.name
        fb.write_bytes(raw_path.read_bytes())
        return fb


# --------------------------------------------------------------------------
# CBZ packaging
# --------------------------------------------------------------------------
def extract_ch_str(chapter_name: str) -> str:
    raw = chapter_name.replace("chapter-", "")
    try:
        return f"{float(raw):g}"
    except ValueError:
        return raw


def _cbz_has_padded_chapters(cbz_path: Path) -> bool:
    try:
        with zipfile.ZipFile(cbz_path, "r") as cbz:
            for name in cbz.namelist():
                m = re.search(r"^Chapter\s+(\d+)", name)
                if m:
                    return len(m.group(1)) >= 3
        return True
    except Exception:
        return False


def _chapter_dir_sort_key(p: Path):
    raw = p.name.replace("chapter-", "")
    try:
        return (0, float(raw))
    except ValueError:
        return (1, raw)


def build_cbz_volumes(series_name: str, opt_dir: Path, out_dir: Path, max_size_mb: int):
    logger.info(f"\nPackaging CBZ volumes (max {max_size_mb} MB/vol)...")
    out_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_size_mb * 1024 * 1024

    ordered = sorted([d for d in opt_dir.iterdir() if d.is_dir()], key=_chapter_dir_sort_key)
    volumes, cur_vol, cur_sz, cur_chs = [], 1, 0, []

    for ch_dir in ordered:
        imgs = sorted(list(ch_dir.glob("*.*")))
        if not imgs:
            continue
        ch_sz = sum(img.stat().st_size for img in imgs)
        if cur_chs and (cur_sz + ch_sz > max_bytes):
            volumes.append((cur_vol, cur_chs))
            cur_vol += 1
            cur_sz = 0
            cur_chs = []
        cur_chs.append((ch_dir, imgs))
        cur_sz += ch_sz

    if cur_chs:
        volumes.append((cur_vol, cur_chs))

    for vol_num, ch_data in volumes:
        start_str = extract_ch_str(ch_data[0][0].name)
        end_str = extract_ch_str(ch_data[-1][0].name)
        if start_str == end_str:
            vol_name = f"{series_name} - Vol {vol_num:02d} (Ch {start_str}).cbz"
        else:
            vol_name = f"{series_name} - Vol {vol_num:02d} (Ch {start_str}-{end_str}).cbz"
        cbz_path = out_dir / vol_name

        if cbz_path.exists() and _cbz_has_padded_chapters(cbz_path):
            logger.debug(f"  Skipping existing: {vol_name}")
            continue

        for old in out_dir.glob(f"{series_name} - Vol {vol_num:02d}*.cbz"):
            if old.name != vol_name:
                old.unlink()

        logger.info(f"  Creating {vol_name}...")
        with zipfile.ZipFile(cbz_path, "w", zipfile.ZIP_DEFLATED) as cbz:
            for c_dir, imgs in ch_data:
                label = c_dir.name.replace("chapter-", "", 1)
                folder_name = f"Chapter {label}"
                for img in imgs:
                    cbz.write(img, f"{folder_name}/{img.name}")
