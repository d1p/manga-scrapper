#!/usr/bin/env python3
"""Scrape manga from sites that serve chapter pages with image tags (non-SPA).

This module is importable (add_arguments / run) and also runs standalone.
"""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import platformdirs

from bs4 import BeautifulSoup
from tqdm import tqdm

from core import (
    setup_logging, sanitize_name, ProgressTracker,
    download_images, clean_chapter_images, optimize_image,
    build_cbz_volumes, chapter_sort_label,
)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

logger = logging.getLogger("manga_scraper")

# --------------------------------------------------------------------------
# Chapter number extraction
# --------------------------------------------------------------------------
CHAPTER_NUM_PATTERNS = [
    r"chapter[\s\-_]*(\d+(?:\.\d+)?)",
    r"\bch[\s\-_.]*(\d+(?:\.\d+)?)\b",
    r"[/\-_](\d+(?:\.\d+)?)(?:[/\-]|$)",
]


def _try_extract_num(pattern: str, source: str) -> Optional[float]:
    m = re.search(pattern, source)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def extract_chapter_number(url: str, text: str) -> Optional[float]:
    path = urlparse(url).path.lower()
    text_lower = text.lower()

    for pattern in CHAPTER_NUM_PATTERNS:
        num = _try_extract_num(pattern, text_lower)
        if num is not None:
            return num

    for pattern in CHAPTER_NUM_PATTERNS[:2]:
        num = _try_extract_num(pattern, path)
        if num is not None:
            return num

    return _try_extract_num(CHAPTER_NUM_PATTERNS[2], path)


def chapter_display_title(num: Optional[float], text: str, idx: int) -> str:
    return f"Chapter {num:g}" if num is not None else (text.strip() or f"Chapter {idx}")


# --------------------------------------------------------------------------
# Page scraping
# --------------------------------------------------------------------------
def find_matching_links(page, base_url: str, match_string: str) -> list[dict]:
    logger.info(f"Searching for links matching '{match_string}'...")
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
    heading = next(
        (soup.select_one(sel) for sel in ["h1.entry-title", "h1", "h2", "h3", "h4", "h5", "h6"]
         if soup.select_one(sel)), None
    )
    candidates = soup.find_all("img") if heading is None else [
        el for el in heading.find_all_next() if el.name == "img"
    ]

    unique, seen = [], set()
    for img in candidates:
        src = img.get("data-src") or img.get("data-lazy-src") or img.get("src")
        if src and src.strip() and not src.startswith("data:"):
            u = urljoin(page_url, src)
            if u not in seen:
                seen.add(u)
                unique.append(u)
    return unique


def scrape_chapter(page, chapter_url: str, chapter_dir: Path, concurrency: int) -> list[Path]:
    chapter_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.goto(chapter_url, wait_until="networkidle", timeout=60000)
    except PWTimeout:
        logger.warning(f"  Page load timed out for {chapter_url}, proceeding with partial content")

    prev_height = 0
    for _ in range(15):
        page.mouse.wheel(0, 2000)
        time.sleep(0.3)
        height = page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height

    img_urls = get_images_after_heading(page.content(), chapter_url)
    if not img_urls:
        return []
    return download_images(img_urls, chapter_dir, chapter_url, concurrency)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def display_dashboard(name: str, links: list, tracker: ProgressTracker, raw_dir: Path):
    done, queued = [], []
    for idx, link in enumerate(links, start=1):
        num = extract_chapter_number(link["url"], link["text"])
        label = chapter_sort_label(num)
        c_dir = raw_dir / f"chapter-{label}"
        (done if tracker.is_chapter_done(c_dir) else queued).append(link["text"])

    print(f"\n{'=' * 60}")
    print(f"  {name}")
    print(f"{'=' * 60}")
    print(f"  Total: {len(links)}  |  Completed: {len(done)}  |  Queued: {len(queued)}")
    print("-" * 60)
    if queued:
        for ch in queued[:5]:
            print(f"  [WAIT] {ch.strip()}")
        if len(queued) > 5:
            print(f"  ... and {len(queued) - 5} more")
    else:
        print("  All chapters up to date")
    print(f"{'=' * 60}\n")
    return len(queued) == 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def _name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    parts = [p for p in path.split("/") if p and len(p) > 2]
    if parts:
        return parts[-1].replace("-", " ").replace("_", " ")
    return ""


def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--filter", default=None, help="String to match in chapter links (defaults to --name)")


def run(args):
    setup_logging(args.debug)
    if not args.name:
        args.name = _name_from_url(args.url)
    safe_name = sanitize_name(args.name)
    if not safe_name:
        logger.error("Name is empty after sanitization")
        sys.exit(1)

    cache_root = Path(args.cache) if args.cache else (Path(args.out) / ".work")
    work_dir = cache_root / safe_name
    library_dir = Path(args.out) / safe_name
    raw_dir = work_dir / "raw_images"
    opt_dir = work_dir / "optimized_images"
    tracker = ProgressTracker(work_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.no_headless)
        try:
            page = browser.new_context().new_page()

            links = find_matching_links(page, args.url, args.filter or args.name or _name_from_url(args.url))
            if not links:
                logger.error("No matching links found")
                sys.exit(1)

            if args.max_chapters:
                links = links[:args.max_chapters]

            all_done = display_dashboard(args.name, links, tracker, raw_dir)

            if not all_done:
                logger.info("Starting download for queued chapters...")
                for idx, link in enumerate(tqdm(links, desc="Processing"), start=1):
                    num = extract_chapter_number(link["url"], link["text"])
                    label = chapter_sort_label(num)
                    title = chapter_display_title(num, link["text"], idx)
                    c_dir = raw_dir / f"chapter-{label}"
                    opt_c_dir = opt_dir / f"chapter-{label}"

                    if tracker.is_chapter_done(c_dir):
                        continue

                    saved = scrape_chapter(page, link["url"], c_dir, args.concurrency)
                    if not saved:
                        continue

                    cleaned = saved if args.no_cleanup else clean_chapter_images(c_dir, saved)
                    if cleaned:
                        opt_c_dir.mkdir(parents=True, exist_ok=True)
                        for img in cleaned:
                            optimize_image(img, opt_c_dir, kindle=args.kindle)
                        tracker.mark_chapter_done(c_dir, title, len(cleaned))
        finally:
            browser.close()

    if not opt_dir.exists() or not any(opt_dir.iterdir()):
        logger.error("No optimized images found to package")
        sys.exit(1)

    build_cbz_volumes(safe_name, opt_dir, library_dir, args.max_vol_mb)
    logger.info("All operations completed successfully")


# --------------------------------------------------------------------------
# CLI argument helpers (shared)
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# CLI argument helpers (shared)
# --------------------------------------------------------------------------
def _delegate_generic():
    raw_url = None
    for i, arg in enumerate(sys.argv):
        if arg == "--url" and i + 1 < len(sys.argv):
            raw_url = sys.argv[i + 1]
            break
        if arg.startswith("--url="):
            raw_url = arg.split("=", 1)[1]
            break
    if not raw_url:
        return
    url_lower = raw_url.lower()
    if any(d in url_lower for d in ("mangadex.org", "mangafire.to")):
        from app import main as app_main
        app_main()
        sys.exit(0)


def _add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--name", default="", help="Series name for output (auto-detected from URL if omitted)")
    parser.add_argument("--url", required=True, help="Manga listing page URL")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files")
    parser.add_argument("--cache", default=platformdirs.user_cache_dir("manga-scrapper"), help="Working directory for downloaded images and metadata")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-vol-mb", type=int, default=300)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--concurrency", type=int, default=8)


def main():
    _delegate_generic()
    parser = argparse.ArgumentParser(description="Scrape manga from sites using HTML chapter/image tags")
    _add_common_args(parser)
    add_arguments(parser)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
