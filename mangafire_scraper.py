#!/usr/bin/env python3
"""Scrape manga from SPA sites that expose chapter/image data via a REST API."""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from tqdm import tqdm

from core import (
    setup_logging, sanitize_name, ProgressTracker,
    download_images, clean_chapter_images, optimize_image,
    build_cbz_volumes, chapter_sort_label, compute_chapter_padding,
)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("Playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)

logger = logging.getLogger("manga_scraper")

# --------------------------------------------------------------------------
# URL parsing
# --------------------------------------------------------------------------
TITLE_ID_RE = re.compile(r"/title/([a-z0-9]+)(?:-.*)?")


def extract_title_id(url: str) -> str:
    m = TITLE_ID_RE.search(url)
    if not m:
        logger.error("Could not extract title ID from URL")
        sys.exit(1)
    return m.group(1)


# --------------------------------------------------------------------------
# API calls (execute in browser to inherit Cloudflare cookies)
# --------------------------------------------------------------------------
def _page_fetch(page, url: str, retries: int = 5) -> Optional[dict]:
    cache_busted = f"{url}{'&' if '?' in url else '?'}_={int(time.time() * 1000)}"
    for attempt in range(retries):
        try:
            result = page.evaluate(
                "async (url) => {"
                "  const r = await fetch(url, {credentials: 'include'});"
                "  if (!r.ok) return null;"
                "  return await r.json();"
                "}",
                cache_busted,
            )
            if result is not None:
                logger.debug(f"  API: {url[:100]}")
                return result
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return None


def fetch_chapters(page, title_id: str, base_url: str, language: str = "en") -> list[dict]:
    logger.info("Fetching chapter list...")
    all_chapters = []
    page_num = 1

    with tqdm(desc="  Chapters", unit=" pages") as pbar:
        while True:
            url = f"{base_url}/api/titles/{title_id}/chapters?language={language}&sort=number&order=desc&page={page_num}&limit=100"
            resp = _page_fetch(page, url)
            if not resp:
                logger.warning(f"  Failed to fetch page {page_num}")
                break

            items = resp.get("items", [])
            all_chapters.extend(items)
            pbar.update(1)

            if not resp.get("meta", {}).get("hasNext"):
                break
            page_num += 1

    logger.info(f"  Found {len(all_chapters)} chapters")
    if all_chapters:
        first = all_chapters[0]
        logger.info(f"  Latest: Ch. {first['number']:g} (id={first['id']}, type={first.get('type', '?')})")
    return all_chapters


def fetch_chapter_pages(page, chapter_id: int, base_url: str) -> Optional[list[dict]]:
    url = f"{base_url}/api/chapters/{chapter_id}"
    resp = _page_fetch(page, url)
    if not resp:
        logger.warning(f"  API call failed for chapter {chapter_id}")
        return None
    return resp.get("data", {}).get("pages", [])


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def display_dashboard(chapters: list[dict], tracker: ProgressTracker, raw_dir: Path, pad: int):
    done, queued = [], []
    for ch in chapters:
        label = chapter_sort_label(ch["number"], pad)
        c_dir = raw_dir / f"chapter-{label}"
        (done if tracker.is_chapter_done(c_dir) else queued).append(ch)

    print(f"\n{'=' * 60}")
    print(f"  Chapter Dashboard")
    print(f"{'=' * 60}")
    print(f"  Total: {len(chapters)}  |  Completed: {len(done)}  |  Queued: {len(queued)}")
    print("-" * 60)
    if queued:
        for ch in queued[:5]:
            print(f"  [WAIT] Ch. {ch['number']:g} ({ch.get('type', '?')})")
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
    parser = argparse.ArgumentParser(description="Scrape manga from SPA sites with a REST API")
    parser.add_argument("--url", required=True, help="Title page URL")
    parser.add_argument("--name", required=True, help="Series name for output")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files")
    parser.add_argument("--cache", default=None, help="Working directory for downloaded images and metadata (default: --out/.work)")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--max-vol-mb", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--language", default="en")
    parser.add_argument("--prefer", choices=["official", "unofficial", "all"], default="official")
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

    title_id = extract_title_id(args.url)
    base_url = f"{urlparse(args.url).scheme}://{urlparse(args.url).netloc}"
    logger.info(f"Title ID: {title_id}  |  Base: {base_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not args.no_headless,
            args=["--disable-cache", "--disable-application-cache"],
        )
        try:
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                bypass_csp=True,
            )
            page = context.new_page()

            logger.info(f"Loading: {args.url}")
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout:
                logger.warning("  Load timeout, waiting for SPA hydration...")

            for _ in range(15):
                page.wait_for_timeout(1000)
                title = page.title()
                if title and title not in ("Just a moment...", "", "Loading...", "MangaFire - Read Manga Online Free"):
                    break

            current_url = page.url
            logger.info(f"  Loaded: {page.title()}  ({current_url})")

            # Extra settle time for API readiness
            page.wait_for_timeout(2000)

            if title_id not in current_url:
                logger.error(f"URL mismatch: expected id='{title_id}', got '{current_url}'")
                logger.error("  Cloudflare challenge may have failed. Try --no-headless to debug.")
                sys.exit(1)

            chapters = fetch_chapters(page, title_id, base_url, args.language)

            if args.prefer != "all":
                seen = {}
                for ch in chapters:
                    n = ch["number"]
                    if n not in seen or ch.get("type") == args.prefer:
                        seen[n] = ch
                chapters = sorted(seen.values(), key=lambda c: c["number"], reverse=True)

            if not chapters:
                logger.error("No chapters found")
                sys.exit(1)

            if args.max_chapters:
                chapters = chapters[:args.max_chapters]

            pad = compute_chapter_padding(chapters)
            logger.debug(f"  Padding: {pad} digits (max ch: {max(ch['number'] for ch in chapters):g})")

            if display_dashboard(chapters, tracker, raw_dir, pad):
                browser.close()
                return

            logger.info("Starting download...")
            for ch in tqdm(chapters, desc="Processing"):
                ch_num = ch["number"]
                ch_id = ch["id"]
                label = chapter_sort_label(ch_num, pad)
                c_dir = raw_dir / f"chapter-{label}"
                opt_c_dir = opt_dir / f"chapter-{label}"

                if tracker.is_chapter_done(c_dir):
                    continue

                page_urls = fetch_chapter_pages(page, ch_id, base_url)
                if not page_urls:
                    logger.warning(f"  No pages for Ch. {ch_num:g}")
                    continue

                saved = download_images(page_urls, c_dir, args.url, args.concurrency)
                if not saved:
                    logger.warning(f"  No images downloaded for Ch. {ch_num:g}")
                    continue

                cleaned = saved if args.no_cleanup else clean_chapter_images(c_dir, saved)
                if cleaned:
                    opt_c_dir.mkdir(parents=True, exist_ok=True)
                    for img in cleaned:
                        optimize_image(img, opt_c_dir)
                    tracker.mark_chapter_done(c_dir, f"Chapter {ch_num:g}", len(cleaned))

            page.close()
        finally:
            browser.close()

    if not opt_dir.exists() or not any(opt_dir.iterdir()):
        logger.error("No optimized images found to package")
        sys.exit(1)

    build_cbz_volumes(safe_name, opt_dir, library_dir, args.max_vol_mb)
    logger.info("All operations completed successfully")


if __name__ == "__main__":
    main()
