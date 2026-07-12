#!/usr/bin/env python3
"""Scrape manga from sites that serve chapter pages with image tags (non-SPA)."""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
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


def extract_chapter_number(url: str, text: str) -> Optional[float]:
    for pattern in CHAPTER_NUM_PATTERNS:
        for target in [text.lower(), urlparse(url).path.lower()]:
            m = re.search(pattern, target)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
    return None


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
def main():
    parser = argparse.ArgumentParser(description="Scrape manga from sites using HTML chapter/image tags")
    parser.add_argument("--name", required=True)
    parser.add_argument("--url", required=True, help="Manga listing page URL")
    parser.add_argument("--filter", default=None, help="String to match in chapter links (defaults to --name)")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files")
    parser.add_argument("--cache", default=None, help="Working directory for downloaded images and metadata (default: --out/.work)")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--max-vol-mb", type=int, default=300)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    setup_logging(args.debug)
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
        browser = p.chromium.launch(headless=args.headless)
        try:
            page = browser.new_context().new_page()

            links = find_matching_links(page, args.url, args.filter or args.name)
            if not links:
                logger.error("No matching links found")
                sys.exit(1)

            if args.max_chapters:
                links = links[:args.max_chapters]

            if display_dashboard(args.name, links, tracker, raw_dir):
                browser.close()
                return

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
                        optimize_image(img, opt_c_dir)
                    tracker.mark_chapter_done(c_dir, title, len(cleaned))
        finally:
            browser.close()

    if not opt_dir.exists() or not any(opt_dir.iterdir()):
        logger.error("No optimized images found to package")
        sys.exit(1)

    build_cbz_volumes(safe_name, opt_dir, library_dir, args.max_vol_mb)
    logger.info("All operations completed successfully")


if __name__ == "__main__":
    main()
