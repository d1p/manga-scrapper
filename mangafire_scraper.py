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
TITLE_ID_RE = re.compile(r"/title/([a-z0-9]+)(?:-.*)?")


def extract_title_id(url: str) -> str:
    m = TITLE_ID_RE.search(url)
    if not m:
        logger.error("Could not extract title ID from URL")
        sys.exit(1)
    return m.group(1)


def _name_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    m = re.search(r"/title/[a-z0-9]+(?:[.-]|/)(.+)$", path)
    if m:
        return m.group(1).replace("-", " ")
    return ""


# --------------------------------------------------------------------------
# Chapter list scraping (DOM-based to avoid vrf-protected API)
# --------------------------------------------------------------------------
def _scrape_chapters_from_dom(page, language: str = "en") -> list[dict]:
    """Scrape chapter data from the rendered DOM (avoids vrf-protected API)."""
    logger.info("Scraping chapter list from DOM...")

    result = page.evaluate(
        """() => {
            const chapters = [];
            const seen = new Set();
            const links = document.querySelectorAll('a[href*="/chapter/"]');
            links.forEach(link => {
                const href = link.getAttribute('href') || '';
                const m = href.match(/\\/chapter\\/(\\d+)$/);
                if (!m) return;
                const id = parseInt(m[1]);
                if (seen.has(id)) return;
                seen.add(id);

                const text = (link.textContent || '').trim();
                const numMatch = text.match(/Ch\\.\\s*([\\d.]+)/i);
                const number = numMatch ? parseFloat(numMatch[1]) : id;

                let name = '';
                const subEl = link.querySelector('.title-detail__row-sub');
                if (subEl) name = (subEl.textContent || '').trim();

                let type = 'unofficial';
                const row = link.closest('.title-detail__row');
                if (row) {
                    const badge = row.querySelector('[title="Official"]');
                    if (badge) type = 'official';
                }

                chapters.push({id, number, name, type, url: href});
            });
            return chapters;
        }""",
    )
    if not result:
        return []
    for ch in result:
        ch["language"] = language
    return result


def fetch_chapters(page, title_id: str, base_url: str, language: str = "en") -> list[dict]:
    return _scrape_chapters_from_dom(page, language)


def fetch_chapter_pages(page, chapter_url_path: str, base_url: str) -> Optional[list[dict]]:
    """Navigate to a chapter page and capture the chapter-pages API response."""
    m = re.search(r"/chapter/(\d+)$", chapter_url_path)
    if not m:
        logger.warning(f"  Could not parse chapter ID from {chapter_url_path}")
        return None
    chapter_id = int(m.group(1))
    full_url = f"{base_url}{chapter_url_path}"

    captured = []

    def _on_response(response):
        if f"/api/chapters/{chapter_id}" in response.url and response.status == 200:
            captured.append(response)

    page.on("response", _on_response)
    try:
        page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
        for _ in range(40):
            if captured:
                break
            page.wait_for_timeout(500)

        if captured:
            data = captured[0].json()
            return data.get("data", {}).get("pages", [])
        return None
    finally:
        page.remove_listener("response", _on_response)


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
def add_arguments(parser: argparse.ArgumentParser):
    parser.add_argument("--language", default="en")
    parser.add_argument("--prefer", choices=["official", "unofficial", "all"], default="official")


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
                by_number = {}
                for ch in chapters:
                    n = ch["number"]
                    if n not in by_number or ch.get("type") == args.prefer:
                        by_number[n] = ch

            chapters = sorted(by_number.values(), key=lambda c: c["number"])

            if not chapters:
                logger.error("No chapters found")
                sys.exit(1)

            if args.max_chapters:
                chapters = chapters[:args.max_chapters]

            pad = compute_chapter_padding(chapters)
            logger.debug(f"  Padding: {pad} digits (max ch: {max(ch['number'] for ch in chapters):g})")

            all_done = display_dashboard(chapters, tracker, raw_dir, pad)

            if not all_done:
                logger.info("Starting download...")
                for ch in tqdm(chapters, desc="Processing"):
                    ch_num = ch["number"]
                    ch_url = ch.get("url", "")
                    label = chapter_sort_label(ch_num, pad)
                    c_dir = raw_dir / f"chapter-{label}"
                    opt_c_dir = opt_dir / f"chapter-{label}"

                    if tracker.is_chapter_done(c_dir):
                        continue

                    page_urls = fetch_chapter_pages(page, ch_url, base_url)

                    if not page_urls:
                        logger.warning(f"  No pages for Ch. {ch_num:g}")
                        continue

                    chapter_ref = f"{base_url}{ch_url}"
                    saved = download_images(page_urls, c_dir, chapter_ref, args.concurrency)
                    if not saved:
                        logger.warning(f"  No images downloaded for Ch. {ch_num:g}")
                        continue

                    cleaned = saved if args.no_cleanup else clean_chapter_images(c_dir, saved)
                    if cleaned:
                        opt_c_dir.mkdir(parents=True, exist_ok=True)
                        for img in cleaned:
                            optimize_image(img, opt_c_dir, kindle=args.kindle)
                        tracker.mark_chapter_done(c_dir, f"Chapter {ch_num:g}", len(cleaned))

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
    parser.add_argument("--url", required=True, help="Title page URL")
    parser.add_argument("--name", default="", help="Series name for output (auto-detected from URL if omitted)")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files")
    parser.add_argument("--cache", default=platformdirs.user_cache_dir("manga-scrapper"), help="Working directory for downloaded images and metadata")
    parser.add_argument("--max-chapters", type=int, default=None)
    parser.add_argument("--max-vol-mb", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--no-cleanup", action="store_true")
    parser.add_argument("--no-headless", action="store_true")
    parser.add_argument("--kindle", action="store_true")
    parser.add_argument("--debug", action="store_true")


def main():
    _delegate_if_mismatch("mangafire_scraper", "mangafire.to")
    parser = argparse.ArgumentParser(description="Scrape manga from SPA sites with a REST API")
    _add_common_args(parser)
    add_arguments(parser)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
