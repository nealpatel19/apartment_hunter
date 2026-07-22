"""
ingestion.py — Craigslist Search Ingestion Engine

Fetches 2-bedroom rental listings directly from Craigslist's modern HTML search endpoints,
normalizes the data, applies price floor/cap pre-filters, and retrieves full description text.
"""

from __future__ import annotations

import logging
import re
import html
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Target SF Zip Codes & broad SF search URLs
ZIP_SEARCH_TEMPLATE = (
    "https://sfbay.craigslist.org/search/sfc/apa"
    "?postal={zip_code}&search_distance=1&min_bedrooms=2&max_bedrooms=2"
)
BROAD_SEARCH_URL = (
    "https://sfbay.craigslist.org/search/sfc/apa"
    "?min_bedrooms=2&max_bedrooms=2"
)

TARGET_ZIP_CODES: list[str] = ["94110", "94117", "94102", "94107", "94114"]

# Non-SF cities to exclude immediately from search results
NON_SF_LOCATIONS = [
    "mill valley", "oakland", "berkeley", "sausalito", "daly city",
    "san mateo", "alameda", "marin", "san rafael", "walnut creek",
    "richmond", "palo alto", "redwood city", "burlingame", "pacifica",
    "tiburon", "corte madera", "novato"
]

# Anything below this in SF is almost certainly a room-share or scam
MINIMUM_PRICE_FLOOR: int = 2_400
# Absolute max — don't even fetch listings above this
MAXIMUM_PRICE_CAP: int = 4_400

REQUEST_DELAY_SECONDS: float = 1.0

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RawListing:
    id: str
    title: str
    price: int                     # monthly rent in USD
    zip_code: str                  # extracted zip code
    url: str
    description: str
    published_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_price(text: str) -> Optional[int]:
    """Extract numeric price from '$2,600' or similar strings."""
    match = re.search(r"\$\s?([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _extract_zip(text: str) -> str:
    """Extract 5-digit SF zip code from text."""
    match = re.search(r"\b(9\d{4})\b", text)
    return match.group(1) if match else ""


def _fetch_page(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch URL with browser User-Agent headers."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def _fetch_description(listing_url: str) -> str:
    """Fetch full body text for a single listing detail page."""
    html_text = _fetch_page(listing_url)
    if not html_text:
        return ""

    match = re.search(r'<section id="postingbody"[^>]*>(.*?)</section>', html_text, re.DOTALL)
    if match:
        # Strip HTML tags and clean up whitespace
        text = re.sub(r"<[^>]+>", " ", match.group(1))
        text = html.unescape(text)
        text = re.sub(r"QR Code Link to This Post", "", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()
    return ""


def _parse_search_page(html_content: str, default_zip: str = "") -> list[RawListing]:
    """Parse listing cards from Craigslist search HTML."""
    pattern = (
        r'<a\s+href="(https://[^\"]*craigslist\.org/view/d/[^\"]+)">\s*'
        r'<div\s+class="title">(.*?)</div>\s*'
        r'<div\s+class="details">\s*'
        r'<div\s+class="price">(.*?)</div>\s*'
        r'<div\s+class="location">(.*?)</div>'
    )

    matches = re.findall(pattern, html_content, re.DOTALL)
    listings: list[RawListing] = []

    for item in matches:
        url, title_raw, price_raw, loc_raw = item

        title = html.unescape(title_raw.strip())
        price = _extract_price(price_raw)
        if price is None:
            continue

        if price < MINIMUM_PRICE_FLOOR or price > MAXIMUM_PRICE_CAP:
            continue

        # Extract unique listing ID from URL (e.g. xyrudhTD8quXVLNcijc4e4)
        listing_id = url.rstrip("/").split("/")[-1]

        location = html.unescape(loc_raw.strip())
        full_text_check = f"{url} {title} {location}".lower()
        if any(non_sf in full_text_check for non_sf in NON_SF_LOCATIONS):
            logger.debug("Skipping non-SF listing: %s (%s)", title, location)
            continue

        zip_code = _extract_zip(title) or _extract_zip(location) or default_zip

        listings.append(
            RawListing(
                id=listing_id,
                title=title,
                price=price,
                zip_code=zip_code,
                url=url,
                description="",  # populated lazily for qualified candidates
                published_at=datetime.now(tz=timezone.utc),
            )
        )

    return listings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_listings() -> list[RawListing]:
    """
    Fetch all 2BR listings for SF target zip codes from Craigslist search endpoints.

    Returns deduplicated list of RawListing objects pre-filtered by price and loaded
    with full description text.
    """
    seen_ids: set[str] = set()
    raw_candidates: list[RawListing] = []

    # 1) Per-zip-code targeted searches
    for zip_code in TARGET_ZIP_CODES:
        url = ZIP_SEARCH_TEMPLATE.format(zip_code=zip_code)
        page_html = _fetch_page(url)
        if page_html:
            found = _parse_search_page(page_html, default_zip=zip_code)
            logger.info("Found %d price-matched listings for zip %s", len(found), zip_code)
            for item in found:
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    raw_candidates.append(item)
        time.sleep(REQUEST_DELAY_SECONDS)

    # 2) Broad SF-wide search to capture any un-zipped listings
    broad_html = _fetch_page(BROAD_SEARCH_URL)
    if broad_html:
        found_broad = _parse_search_page(broad_html)
        logger.info("Found %d price-matched listings in broad SF search", len(found_broad))
        for item in found_broad:
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                raw_candidates.append(item)

    # 3) Fetch full descriptions for candidate listings
    final_listings: list[RawListing] = []
    for idx, listing in enumerate(raw_candidates, start=1):
        logger.info("Fetching full description %d/%d: %s", idx, len(raw_candidates), listing.title[:50])
        listing.description = _fetch_description(listing.url) or listing.title
        final_listings.append(listing)
        time.sleep(0.5)

    logger.info(
        "Ingestion complete: %d total qualified candidate listings (floor=$%d, cap=$%d)",
        len(final_listings),
        MINIMUM_PRICE_FLOOR,
        MAXIMUM_PRICE_CAP,
    )
    return final_listings
