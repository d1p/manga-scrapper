#!/usr/bin/env python3
"""
manga_scraper_cbz.py

Scrapes manga chapter images, caches them, optimizes them, and packages 
them into appropriately-sized CBZ volumes. Features a persistent JSON 
tracker and UI dashboard to drastically speed up ongoing runs.
"""

import argparse
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
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from tqdm import tqdm

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

# --------------------------------------------------------------------------
# Logging & Utilities
# --------------------------------------------------------------------------
logger = logging.getLogger("manga_scraper")
# Suppress noisy third-party logs
logging.getLogger("urllib3").setLevel(logging.WARNING)

def setup_logging(debug: bool):
    level = logging.DEBUG if debug else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.setLevel(level)
    logger.addHandler(handler)

def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)

# --------------------------------------------------------------------------
# Tracker & UI Dashboard
# --------------------------------------------------------------------------
class ProgressTracker:
    """Manages progress.json and .done markers to skip completed work."""
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
        """A chapter is done if progress.json says so AND the .done file exists."""
        json_done = self.data.get("chapters", {}).get(chapter_dir.name, {}).get("status") == "done"
        file_done = (chapter_dir / ".done").exists()
        return json_done and file_done

    def mark_chapter_done(self, chapter_dir: Path, title: str, pages: int):
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / ".done").touch(exist_ok=True)
        
        if "chapters" not in self.data:
            self.data["chapters"] = {}
            
        self.data["chapters"][chapter_dir.name] = {
            "title": title,
            "status": "done",
            "pages": pages,
            "timestamp": time.time()
        }
        self.save()

def display_dashboard(manga_name: str, links: list, tracker: ProgressTracker, raw_dir: Path):
    """Prints a clean UI showing current scrape state."""
    total = len(links)
    done_chapters = []
    queued_chapters = []

    for idx, link in enumerate(links, start=1):
        num = extract_chapter_number(link["url"], link["text"])
        label = chapter_sort_label(num, idx)
        c_dir = raw_dir / f"chapter-{label}"
        
        if tracker.is_chapter_done(c_dir):
            done_chapters.append(link["text"])
        else:
            queued_chapters.append(link["text"])

    print("\n" + "="*60)
    print(f" 📖 MANGA DASHBOARD: {manga_name}")
    print("="*60)
    print(f" Total Chapters Found : {total}")
    print(f" Previously Completed : {len(done_chapters)}")
    print(f" Queued for Download  : {len(queued_chapters)}")
    print("-" * 60)
    
    # Show a brief preview of the queue to keep the console clean
    if queued_chapters:
        print(" Next up:")
        for ch in queued_chapters[:5]:
            print(f"   [WAIT] {ch.strip()}")
        if len(queued_chapters) > 5:
            print(f"   ... and {len(queued_chapters) - 5} more.")
    else:
        print(" 🎉 All chapters are up to date!")
    print("="*60 + "\n")
    
    return len(queued_chapters) == 0

# --------------------------------------------------------------------------
# Web Scraping & Link Extraction
# --------------------------------------------------------------------------
CHAPTER_NUM_PATTERNS = [
    r"chapter[\s\-_]*(\d+(?:\.\d+)?)",
    r"\bch[\s\-_.]*(\d+(?:\.\d+)?)\b",
    r"[/\-_](\d+(?:\.\d+)?)(?:[/\-]|$)",
]

def extract_chapter_number(url: str, text: str) -> Optional[float]:
    search_targets = [text.lower(), urlparse(url).path.lower()]
    for pattern in CHAPTER_NUM_PATTERNS:
        for target in search_targets:
            m = re.search(pattern, target)
            if m:
                try: return float(m.group(1))
                except ValueError: continue
    return None

def chapter_sort_label(num: Optional[float], idx: int) -> str:
    return f"{num:07.1f}" if num is not None else f"unknown-{idx:04d}"

def chapter_display_title(num: Optional[float], text: str, idx: int) -> str:
    return f"Chapter {num:g}" if num is not None else (text.strip() or f"Chapter {idx}")

def find_matching_links(page, base_url: str, match_string: str) -> list[dict]:
    logger.info(f"🔍 Searching for links matching '{match_string}'...")
    page.goto(base_url, wait_until="networkidle", timeout=60000)
    soup = BeautifulSoup(page.content(), "html.parser")

    found, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if match_string.lower() in f"{href} {text}".lower():
            full_url = urljoin(base_url, href)
            if full_url not in seen:
                seen.add(full_url)
                found.append({"url": full_url, "text": text})
    return found

def get_images_after_heading(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    heading = next((soup.select_one(sel) for sel in ["h1.entry-title", "h1", "h2", "h3", "h4", "h5", "h6"] if soup.select_one(sel)), None)
    candidates = soup.find_all("img") if heading is None else [el for el in heading.find_all_next() if el.name == "img"]

    unique_imgs, seen = [], set()
    for img in candidates:
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if src and not src.startswith("data:"):
            u = urljoin(page_url, src)
            if u not in seen:
                seen.add(u)
                unique_imgs.append(u)
    return unique_imgs

# --------------------------------------------------------------------------
# Downloading & Image Processing
# --------------------------------------------------------------------------
_thread_local = threading.local()

def get_thread_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = requests.Session()
    return _thread_local.session

def download_image(url: str, dest_path: Path, referer: str) -> bool:
    if dest_path.exists() and dest_path.stat().st_size > 0: return True
    try:
        resp = get_thread_session().get(url, headers={"Referer": referer}, timeout=30, stream=True)
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(8192): f.write(chunk)
        return True
    except Exception: return False

def download_images_concurrent(img_urls: list[str], chapter_dir: Path, referer: str, max_workers: int = 8) -> list[Path]:
    dest_paths = [None] * len(img_urls)
    
    def _task(i: int, url: str):
        ext = Path(urlparse(url).path).suffix
        ext = ext if ext and len(ext) <= 5 else ".jpg"
        dest = chapter_dir / f"{i + 1:03d}{ext}"
        return i, (dest if download_image(url, dest, referer) else None)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_task, i, url) for i, url in enumerate(img_urls)]
        with tqdm(total=len(img_urls), desc=f"  📥 Downloading", leave=False) as pbar:
            for future in as_completed(futures):
                i, path = future.result()
                dest_paths[i] = path
                pbar.update(1)
    return [p for p in dest_paths if p is not None]

def scrape_chapter(page, chapter_url: str, chapter_dir: Path, concurrency: int) -> list[Path]:
    chapter_dir.mkdir(parents=True, exist_ok=True)
    try: page.goto(chapter_url, wait_until="networkidle", timeout=60000)
    except PWTimeout: pass

    prev_height = 0
    for _ in range(15):
        page.mouse.wheel(0, 2000)
        time.sleep(0.3)
        height = page.evaluate("document.body.scrollHeight")
        if height == prev_height: break
        prev_height = height

    img_urls = get_images_after_heading(page.content(), chapter_url)
    if not img_urls: return []
    return download_images_concurrent(img_urls, chapter_dir, chapter_url, concurrency)

def clean_chapter_images(chapter_dir: Path, image_paths: list[Path], min_dim: int, sz_ratio: float, area_ratio: float, aspect_limit: float) -> list[Path]:
    if len(image_paths) < 3: return image_paths
    infos = []
    for p in image_paths:
        try:
            with Image.open(p) as img:
                infos.append({"path": p, "size": p.stat().st_size, "w": img.width, "h": img.height, "area": img.width * img.height})
        except Exception: pass

    if not infos: return image_paths
    med_sz, med_area = statistics.median([i["size"] for i in infos]), statistics.median([i["area"] for i in infos])
    kept, rem_dir = [], chapter_dir / "_removed"

    for i in infos:
        p, w, h, sz, area = i["path"], i["w"], i["h"], i["size"], i["area"]
        if (w < min_dim and h < min_dim) or (max(w, h)/max(min(w, h), 1) > aspect_limit) or (sz < med_sz * sz_ratio) or (area < med_area * area_ratio):
            rem_dir.mkdir(exist_ok=True)
            try: p.rename(rem_dir / p.name)
            except Exception: pass
        else: kept.append(p)
    return kept

def optimize_image(raw_path: Path, opt_dir: Path, max_width: int = 1600) -> Path:
    opt_jpg, opt_png = opt_dir / f"{raw_path.stem}.jpg", opt_dir / f"{raw_path.stem}.png"
    if opt_jpg.exists(): return opt_jpg
    if opt_png.exists(): return opt_png

    try:
        with Image.open(raw_path) as img:
            has_alpha = img.mode == "RGBA"
            img = img.convert("RGBA") if has_alpha else img.convert("RGB")
            if img.width > max_width:
                img = img.resize((max_width, int(img.height * (max_width / img.width))), Image.LANCZOS)
            out = opt_png if has_alpha else opt_jpg
            img.save(out, format="PNG" if has_alpha else "JPEG", quality=85, optimize=True)
            return out
    except Exception:
        fb = opt_dir / raw_path.name
        fb.write_bytes(raw_path.read_bytes())
        return fb

# --------------------------------------------------------------------------
# CBZ Compilation
# --------------------------------------------------------------------------
def extract_ch_str(chapter_name: str) -> str:
    raw = chapter_name.replace("chapter-", "")
    try: return f"{float(raw):g}"
    except ValueError: return raw

def build_cbz_volumes(manga_name: str, opt_base_dir: Path, out_dir: Path, max_size_mb: int):
    logger.info(f"\n📚 Packaging CBZ Volumes (Max {max_size_mb} MB/vol)...")
    out_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max_size_mb * 1024 * 1024
    
    # Gather all completed optimized chapters
    ordered_chapters = sorted([d for d in opt_base_dir.iterdir() if d.is_dir()], key=lambda p: p.name)
    
    volumes, current_vol, current_size, current_chapters = [], 1, 0, []

    for ch_dir in ordered_chapters:
        images = sorted(list(ch_dir.glob("*.*")))
        if not images: continue
        ch_size = sum(img.stat().st_size for img in images)

        if current_chapters and (current_size + ch_size > max_bytes):
            volumes.append((current_vol, current_chapters))
            current_vol += 1; current_size = 0; current_chapters = []

        current_chapters.append((ch_dir, images))
        current_size += ch_size

    if current_chapters: volumes.append((current_vol, current_chapters))

    for vol_num, ch_data in volumes:
        start_str = extract_ch_str(ch_data[0][0].name)
        end_str = extract_ch_str(ch_data[-1][0].name)
        vol_name = f"{manga_name} - Vol {vol_num:02d} (Ch {start_str}).cbz" if start_str == end_str else f"{manga_name} - Vol {vol_num:02d} (Ch {start_str}-{end_str}).cbz"
        cbz_path = out_dir / vol_name
        
        # SMART SKIP: If the exact filename exists, this exact block of chapters is already packaged.
        if cbz_path.exists():
            logger.debug(f"  ⏭️  Skipping existing volume: {vol_name}")
            continue

        # Clean up older, incomplete versions of this volume number before creating the new one
        for existing in out_dir.glob(f"{manga_name} - Vol {vol_num:02d}*.cbz"):
            existing.unlink()

        logger.info(f"  📦 Creating {vol_name}...")
        with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as cbz:
            for c_dir, imgs in ch_data:
                folder_name = f"Chapter {extract_ch_str(c_dir.name)}"
                for img in imgs:
                    cbz.write(img, f"{folder_name}/{img.name}")

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--filter", default=None)
    parser.add_argument("--out", default="./manga_output")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--headless", action="store_true", default=True)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-vol-mb", type=int, default=300)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    setup_logging(args.debug)
    out_dir = Path(args.out) / slugify(args.name)
    out_dir.mkdir(parents=True, exist_ok=True)
    
    raw_dir, opt_dir = out_dir / "raw_images", out_dir / "optimized_images"
    tracker = ProgressTracker(out_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=args.headless)
        page = browser.new_context().new_page()

        links = find_matching_links(page, args.url, args.filter or args.name)
        if not links:
            logger.error("❌ No matching links found.")
            sys.exit(1)

        if args.max_chapters: links = links[:args.max_chapters]

        # Render Dashboard
        is_fully_synced = display_dashboard(args.name, links, tracker, raw_dir)
        
        if not is_fully_synced:
            logger.info("🚀 Starting scraper for queued chapters...")
            for idx, link in enumerate(tqdm(links, desc="Processing Chapters"), start=1):
                num = extract_chapter_number(link["url"], link["text"])
                label = chapter_sort_label(num, idx)
                title = chapter_display_title(num, link["text"], idx)
                
                c_dir = raw_dir / f"chapter-{label}"
                opt_c_dir = opt_dir / f"chapter-{label}"
                
                if tracker.is_chapter_done(c_dir):
                    continue

                saved = scrape_chapter(page, link["url"], c_dir, args.concurrency)
                if not saved: continue

                cleaned = saved if args.no_cleanup else clean_chapter_images(
                    c_dir, saved, 300, 0.15, 0.15, 4.0
                )

                if cleaned:
                    opt_c_dir.mkdir(parents=True, exist_ok=True)
                    for img in cleaned:
                        optimize_image(img, opt_c_dir)
                    
                    # Mark chapter completely done
                    tracker.mark_chapter_done(c_dir, title, len(cleaned))

        browser.close()

    # Step 3: Package logic (Reads strictly from opt_dir, bypassing scraper overhead completely)
    if not any(opt_dir.iterdir()):
        logger.error("❌ No optimized images found to package.")
        sys.exit(1)

    build_cbz_volumes(args.name, opt_dir, out_dir, args.max_vol_mb)
    logger.info("\n✅ All operations completed successfully.")

if __name__ == "__main__":
    main()