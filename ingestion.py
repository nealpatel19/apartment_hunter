"""
ingestion.py — Craigslist RSS Ingestion Engine

Fetches 2-bedroom rental listings for target SF zip codes via Craigslist RSS feeds,
normalizes the data, and applies a minimum price floor to filter room-shares.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import feedparser
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Craigslist SF RSS endpoint for 2-bedroom apartments
# category: apa = apartments/housing, bedrooms=2
CRAIGSLIST_RSS_TEMPLATE = (
    "https://sfbay.craigslist.org/search/apa/rss.xml"
    "?bedrooms=2&bathrooms=1&postal={zip_code}&search_distance=1"
)

# Additional broad search to capture listings without zip attached
CRAIGSLIST_RSS_BROAD = (
    "https://sfbay.craigslist.org/search/sfc/apa/rss.xml"
    "?bedrooms=2&bathrooms=1"
)

TARGET_ZIP_CODES: list[str] = ["94110", "94117", "94102", "94107"]

# Anything below this in SF is almost certainly a room-share or scam
MINIMUM_PRICE_FLOOR: int = 2_400
# Absolute max — don't even fetch listings above this
MAXIMUM_PRICE_CAP: int = 4_400

# Seconds to wait between RSS feed requests (be polite to Craigslist)
REQUEST_DELAY_SECONDS: float = 1.5

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RawListing:
    id: str
    title: str
    price: int                     # monthly rent in USD
    zip_code: str                  # best-effort extracted zip code
    url: str
    description: str
    published_at: datetime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_price(text: str) -> Optional[int]:
    """Extract the first dollar amount found in a string."""
    match = re.search(r"\$\s?([\d,]+)", text)
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _extract_zip(text: str) -> str:
    """Extract the first 5-digit zip code found in a string, or return empty."""
    match = re.search(r"\b(9\d{4})\b", text)
    return match.group(1) if match else ""


def _parse_published(entry) -> datetime:
    """Convert feedparser time struct to UTC datetime."""
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return datetime.now(tz=timezone.utc)


def _fetch_feed(url: str, timeout: int = 15) -> list[dict]:
    """Fetch a single RSS feed and return its entries as a list."""
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        entries = feed.get("entries", [])
        logger.info("Fetched %d entries from %s", len(entries), url)
        return entries
    except requests.RequestException as exc:
        logger.warning("Failed to fetch feed %s: %s", url, exc)
        return []


def _entry_to_raw_listing(entry: dict) -> Optional[RawListing]:
    """Convert a single feedparser entry into a RawListing, or None if invalid."""
    url: str = getattr(entry, "link", "") or entry.get("link", "")
    title: str = getattr(entry, "title", "") or entry.get("title", "")

    # Craigslist uses the <id> tag or falls back to link
    listing_id: str = (
        getattr(entry, "id", "")
        or entry.get("id", "")
        or url
    )

    summary: str = (
        getattr(entry, "summary", "")
        or entry.get("summary", "")
        or ""
    )

    # Price extraction: try title first, then summary
    price = _extract_price(title) or _extract_price(summary)
    if price is None:
        logger.debug("Skipping entry (no price found): %s", title)
        return None

    # Apply price floor and cap early to save AI calls
    if price < MINIMUM_PRICE_FLOOR:
        logger.debug("Skipping entry (price $%d below floor): %s", price, title)
        return None
    if price > MAXIMUM_PRICE_CAP:
        logger.debug("Skipping entry (price $%d above cap): %s", price, title)
        return None

    # Zip code: try to extract from title, summary, or URL
    zip_code = (
        _extract_zip(title)
        or _extract_zip(summary)
        or _extract_zip(url)
    )

    published_at = _parse_published(entry)

    return RawListing(
        id=listing_id,
        title=title.strip(),
        price=price,
        zip_code=zip_code,
        url=url.strip(),
        description=summary.strip(),
        published_at=published_at,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_listings() -> list[RawListing]:
    """
    Fetch all 2BR listings for SF target zip codes from Craigslist RSS feeds.

    Returns a deduplicated list of RawListing objects, pre-filtered by price.
    """
    seen_ids: set[str] = set()
    listings: list[RawListing] = []

    # 1) Per-zip-code targeted searches
    for zip_code in TARGET_ZIP_CODES:
        url = CRAIGSLIST_RSS_TEMPLATE.format(zip_code=zip_code)
        entries = _fetch_feed(url)
        for entry in entries:
            raw = _entry_to_raw_listing(entry)
            if raw and raw.id not in seen_ids:
                # If zip wasn't in the listing text, use the search zip
                if not raw.zip_code:
                    raw.zip_code = zip_code
                seen_ids.add(raw.id)
                listings.append(raw)
        time.sleep(REQUEST_DELAY_SECONDS)

    # 2) Broad SF-wide search to capture any missed listings
    entries = _fetch_feed(CRAIGSLIST_RSS_BROAD)
    for entry in entries:
        raw = _entry_to_raw_listing(entry)
        if raw and raw.id not in seen_ids:
            seen_ids.add(raw.id)
            listings.append(raw)

    logger.info(
        "Ingestion complete: %d listings after price filtering (floor=$%d, cap=$%d)",
        len(listings),
        MINIMUM_PRICE_FLOOR,
        MAXIMUM_PRICE_CAP,
    )
    return listings
