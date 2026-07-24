#!/usr/bin/env python3
"""Scrape manga from MangaDex (https://mangadex.org) via its public REST API."""

import argparse
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import platformdirs
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
MANGA_ID_RE = re.compile(r"/title/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})")
API_BASE = "https://api.mangadex.org"
MANGADEX_ORIGIN = "https://mangadex.org"


def extract_manga_id(url: str) -> str:
    m = MANGA_ID_RE.search(url)
    if not m:
        logger.error("Could not extract MangaDex manga ID from URL")
        sys.exit(1)
    return m.group(1)


def _name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    m = re.search(r"/title/[a-f0-9-]+/(.+)$", path)
    if m:
        return m.group(1).replace("-", " ")
    return ""


# --------------------------------------------------------------------------
# API calls (execute in browser to inherit proper TLS fingerprint)
# --------------------------------------------------------------------------
def _page_fetch(page, url: str, retries: int = 5) -> Optional[dict]:
    for attempt in range(retries):
        try:
            result = page.evaluate(
                "async (url) => {"
                "  const r = await fetch(url);"
                "  if (!r.ok) return null;"
                "  return await r.json();"
                "}",
                url,
            )
            if result is not None:
                logger.debug(f"  API: {url[:120]}")
                return result
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return None


def fetch_chapters(page, manga_id: str, language: str = "en",
                   group_filter: str | None = None) -> list[dict]:
    logger.info("Fetching chapter list...")
    all_chapters: list[dict] = []
    offset = 0
    limit = 500

    rating_params = "&contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica&contentRating[]=pornographic"

    with tqdm(desc="  Chapters", unit=" pages") as pbar:
        while True:
            url = (f"{API_BASE}/manga/{manga_id}/feed"
                   f"?translatedLanguage[]={language}"
                   f"&limit={limit}&offset={offset}"
                   f"&order[chapter]=asc"
                   f"&includes[]=scanlation_group"
                   f"{rating_params}")
            resp = _page_fetch(page, url)
            if not resp:
                break

            items = resp.get("data", [])
            all_chapters.extend(items)
            pbar.update(1)

            total = resp.get("total", 0)
            if offset + limit >= total:
                break
            offset += limit
            time.sleep(0.3)

    if group_filter:
        all_chapters = [ch for ch in all_chapters if _chapter_matches_group(ch, group_filter)]

    logger.info(f"  Found {len(all_chapters)} chapters")
    if all_chapters:
        first = all_chapters[0]
        logger.info(f"  First: Ch. {_chapter_number(first):g}")
    return all_chapters


def _chapter_number(ch: dict) -> float:
    raw = ch.get("attributes", {}).get("chapter")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (ValueError, TypeError):
        return 0.0


def _chapter_group_name(ch: dict) -> str:
    for rel in ch.get("relationships", []):
        if rel.get("type") == "scanlation_group":
            return rel.get("attributes", {}).get("name", "")
    return ""


def _chapter_matches_group(ch: dict, group_filter: str) -> bool:
    return group_filter.lower() in _chapter_group_name(ch).lower()


def deduplicate_chapters(chapters: list[dict]) -> list[dict]:
    by_number: dict[float, dict] = {}
    for ch in chapters:
        n = _chapter_number(ch)
        if n not in by_number:
            by_number[n] = ch
        else:
            existing_group = _chapter_group_name(by_number[n])
            new_group = _chapter_group_name(ch)
            if not existing_group and new_group:
                by_number[n] = ch
    return sorted(by_number.values(), key=_chapter_number)


def fetch_chapter_pages(page, chapter_id: str) -> list[str] | None:
    url = f"{API_BASE}/at-home/server/{chapter_id}"
    resp = _page_fetch(page, url)
    if not resp:
        return None

    base_url = resp.get("baseUrl")
    chapter_data = resp.get("chapter", {})
    chapter_hash = chapter_data.get("hash")
    page_files = chapter_data.get("data", [])

    if not base_url or not chapter_hash or not page_files:
        return None

    return [f"{base_url}/data/{chapter_hash}/{f}" for f in page_files]


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------
def display_dashboard(chapters: list[dict], tracker: ProgressTracker,
                      raw_dir: Path, pad: int) -> bool:
    done, queued = [], []
    for ch in chapters:
        label = chapter_sort_label(_chapter_number(ch), pad)
        c_dir = raw_dir / f"chapter-{label}"
        (done if tracker.is_chapter_done(c_dir) else queued).append(ch)

    print(f"\n{'=' * 60}")
    print(f"  Chapter Dashboard")
    print(f"{'=' * 60}")
    print(f"  Total: {len(chapters)}  |  Completed: {len(done)}  |  Queued: {len(queued)}")
    print("-" * 60)
    if queued:
        for ch in queued[:5]:
            n = _chapter_number(ch)
            attrs = ch.get("attributes", {})
            title = attrs.get("title") or ""
            group = _chapter_group_name(ch)
            info = f"Ch. {n:g}"
            if title:
                info += f" - {title}"
            if group:
                info += f" [{group}]"
            print(f"  [WAIT] {info}")
        if len(queued) > 5:
            print(f"  ... and {len(queued) - 5} more")
    else:
        print("  All chapters up to date")
    print(f"{'=' * 60}\n")
    return len(queued) == 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--language", default="en", help="Translated language code (default: en)")
    parser.add_argument("--group", default=None, help="Filter chapters by scanlation group (partial match)")


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

    manga_id = extract_manga_id(args.url)
    logger.info(f"MangaDex ID: {manga_id}")

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
                logger.warning("  Load timeout, continuing...")

            page.wait_for_timeout(2000)
            logger.info(f"  Loaded: {page.title()}")

            chapters = fetch_chapters(page, manga_id, args.language, args.group)
            if not chapters:
                logger.error("No chapters found")
                sys.exit(1)

            chapters = deduplicate_chapters(chapters)

            if args.max_chapters:
                chapters = chapters[:args.max_chapters]

            for ch in chapters:
                ch["number"] = _chapter_number(ch)

            pad = compute_chapter_padding(chapters)
            logger.debug(f"  Padding: {pad} digits (max ch: {max(ch['number'] for ch in chapters):g})")

            all_done = display_dashboard(chapters, tracker, raw_dir, pad)

            if not all_done:
                logger.info("Starting download...")
                for ch in tqdm(chapters, desc="Processing"):
                    ch_num = _chapter_number(ch)
                    ch_id = ch["id"]
                    label = chapter_sort_label(ch_num, pad)
                    c_dir = raw_dir / f"chapter-{label}"
                    opt_c_dir = opt_dir / f"chapter-{label}"

                    if tracker.is_chapter_done(c_dir):
                        continue

                    page_urls = fetch_chapter_pages(page, ch_id)
                    if not page_urls:
                        logger.warning(f"  No pages for Ch. {ch_num:g}")
                        continue

                    saved = download_images(page_urls, c_dir, MANGADEX_ORIGIN, args.concurrency)
                    if not saved:
                        logger.warning(f"  No images downloaded for Ch. {ch_num:g}")
                        continue

                    cleaned = saved if args.no_cleanup else clean_chapter_images(c_dir, saved)
                    if cleaned:
                        opt_c_dir.mkdir(parents=True, exist_ok=True)
                        for img in cleaned:
                            optimize_image(img, opt_c_dir, kindle=args.kindle)
                        attrs = ch.get("attributes", {})
                        title = attrs.get("title") or f"Chapter {ch_num:g}"
                        tracker.mark_chapter_done(c_dir, title, len(cleaned))

                    time.sleep(0.2)

            page.close()
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
def _delegate_if_mismatch(expected_module: str, domain_hint: str):
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
    if domain_hint in raw_url.lower():
        return
    from app import main as app_main
    app_main()
    sys.exit(0)


def _add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--url", required=True)
    parser.add_argument("--name", default="", help="Series name for output (auto-detected from URL if omitted)")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files")
    parser.add_argument("--cache", default=platformdirs.user_cache_dir("manga-scrapper"), help="Working directory for downloaded images and metadata")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--max-vol-mb", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--debug", action="store_true")


def main():
    _delegate_if_mismatch("mangadex_scraper", "mangadex.org")
    parser = argparse.ArgumentParser(description="Scrape manga from MangaDex")
    _add_common_args(parser)
    add_arguments(parser)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
