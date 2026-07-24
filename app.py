#!/usr/bin/env python3
"""Unified manga scraper — auto-detects site and dispatches to the right backend.

Usage:
  python app.py --name "Series Name" --url "https://..."
"""

import argparse
import logging
import re
import sys
from urllib.parse import urlparse

import platformdirs

logger = logging.getLogger("manga_scraper")

# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
_SITE_DETECTORS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"mangadex\.org", re.IGNORECASE), "mangadex_scraper"),
    (re.compile(r"mangafire\.to", re.IGNORECASE), "mangafire_scraper"),
]

_SITE_LABELS = {
    "mangadex_scraper": "MangaDex API scraper",
    "mangafire_scraper": "MangaFire API scraper",
    "generic_scraper": "Generic HTML scraper",
}


def _find_url_in_argv() -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--url" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--url="):
            return arg.split("=", 1)[1]
    return None


def _detect_scraper(url: str) -> str:
    for pattern, module_name in _SITE_DETECTORS:
        if pattern.search(url):
            return module_name
    return "generic_scraper"


# ---------------------------------------------------------------------------
# Common CLI arguments
# ---------------------------------------------------------------------------
def _add_common_args(parser: argparse.ArgumentParser):
    parser.add_argument("--url", required=True, help="Manga page URL")
    parser.add_argument("--name", default="", help="Series name for output (auto-detected from URL if omitted)")
    parser.add_argument("--out", default="./manga_output", help="Output directory for final CBZ files (default: ./manga_output)")
    parser.add_argument("--cache", default=platformdirs.user_cache_dir("manga-scrapper"), help="Working directory for downloads and metadata")
    parser.add_argument("--max-chapters", type=int, default=None, help="Limit number of chapters to download")
    parser.add_argument("--max-vol-mb", type=int, default=200, help="Maximum CBZ volume size in MB (default: 200)")
    parser.add_argument("--concurrency", type=int, default=12, help="Parallel image downloads (default: 8)")
    parser.add_argument("--no-cleanup", action="store_true", help="Skip junk-image removal")
    parser.add_argument("--no-headless", action="store_true", help="Show browser window instead of running headless")
    parser.add_argument("--kindle", action="store_true", help="Optimize images for Kindle 2022 (6-inch e-ink, 1072x1448, 300 PPI)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------
def main():
    raw_url = _find_url_in_argv()
    module_name = _detect_scraper(raw_url) if raw_url else "generic_scraper"
    label = _SITE_LABELS.get(module_name, module_name)

    parser = argparse.ArgumentParser(
        description=f"Unified manga scraper  |  Detected: {label}\n"
                     "Auto-selects the best backend based on the --url value.",
    )
    _add_common_args(parser)

    try:
        scraper = __import__(module_name)
        if hasattr(scraper, "add_arguments"):
            scraper.add_arguments(parser)
    except ImportError:
        logger.error(f"Failed to import {module_name}. Check dependencies.")
        sys.exit(1)

    args = parser.parse_args()

    if not hasattr(scraper, "run"):
        logger.error(f"Scraper module '{module_name}' does not export a 'run(args)' function.")
        sys.exit(1)

    scraper.run(args)


if __name__ == "__main__":
    main()
