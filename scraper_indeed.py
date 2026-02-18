"""
Indeed scraper — STUBBED OUT.

Indeed requires login to view past the first page of results and uses
aggressive CAPTCHA/bot detection that blocks both headless and visible
Chrome sessions. Not viable for automated scraping.

Returns empty results so main.py can still reference it without errors.
"""
from models import JobListing, ExcludedJob


def scrape_indeed(known_urls: set[str] = None) -> tuple[list[JobListing], list[ExcludedJob]]:
    """Stub — Indeed is not scrapable without login + CAPTCHA bypass."""
    print("[Indeed] Skipped — site requires login and blocks automated access.")
    return [], []
