"""
zillow_ingestion.py — Zillow & Trulia Rental Listing Ingestion Engine

Fetches active rental listings directly from Zillow using Chrome TLS impersonation (curl_cffi)
and extracts structured JSON payload from Zillow's internal __NEXT_DATA__ page state.
"""

from __future__ import annotations

import logging
import re
import json
import time
from datetime import datetime, timezone
from typing import Optional

from curl_cffi import requests

from ingestion import RawListing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ZILLOW_ZIP_SEARCH_TEMPLATE = (
    "https://www.zillow.com/san-francisco-ca-{zip_code}/rentals/2-_beds/2400-4400_mp/"
)

TARGET_ZIP_CODES: list[str] = ["94110", "94117", "94102", "94107", "94114"]
MINIMUM_PRICE_FLOOR: int = 2_400
MAXIMUM_PRICE_CAP: int = 4_400
REQUEST_DELAY_SECONDS: float = 1.5

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_price(price_str: str) -> Optional[int]:
    """Extract numeric integer price from '$3,500/mo' or '$2,450+'."""
    match = re.search(r"\$\s?([\d,]+)", str(price_str))
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _extract_zip(address_str: str) -> str:
    """Extract 5-digit zip code from address string."""
    match = re.search(r"\b(9\d{4})\b", str(address_str))
    return match.group(1) if match else ""


def _fetch_zillow_zip(zip_code: str) -> list[RawListing]:
    """Fetch and parse Zillow rental listings for a single SF zip code."""
    url = ZILLOW_ZIP_SEARCH_TEMPLATE.format(zip_code=zip_code)
    try:
        resp = requests.get(url, impersonate="chrome120", timeout=15)
        if resp.status_code != 200:
            logger.info("Zillow cloud IP restricted (HTTP %d) for zip %s — skipping.", resp.status_code, zip_code)
            return []

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text,
            re.DOTALL,
        )
        if not match:
            logger.warning("Zillow __NEXT_DATA__ payload missing for zip %s", zip_code)
            return []

        data = json.loads(match.group(1))
        page_props = data.get("props", {}).get("pageProps", {})
        search_state = page_props.get("searchPageState", {})
        list_results = (
            search_state.get("cat1", {})
            .get("searchResults", {})
            .get("listResults", [])
        )

        listings: list[RawListing] = []
        for item in list_results:
            zpid = str(item.get("zpid", "")) or str(item.get("id", ""))
            if not zpid:
                continue

            address = str(item.get("address", ""))
            price_raw = item.get("price", "") or item.get("unformattedPrice", "")
            price = _extract_price(price_raw)

            # Check beds count if available
            beds = item.get("beds")
            if beds is not None and isinstance(beds, (int, float)) and beds < 2:
                continue

            if price and (price < MINIMUM_PRICE_FLOOR or price > MAXIMUM_PRICE_CAP):
                continue

            detail_url = str(item.get("detailUrl", ""))
            if detail_url and not detail_url.startswith("http"):
                detail_url = f"https://www.zillow.com{detail_url}"

            title = f"Zillow 2+BR: {address}" if address else "Zillow Rental Listing"
            extracted_zip = _extract_zip(address) or zip_code

            listings.append(
                RawListing(
                    id=f"zillow_{zpid}",
                    title=title,
                    price=price or 3800,  # default to target if price unstated
                    zip_code=extracted_zip,
                    url=detail_url or url,
                    description=f"Zillow rental property located at {address}. Beds: {beds or '2+'}.",
                    published_at=datetime.now(tz=timezone.utc),
                )
            )

        logger.info("Parsed %d Zillow listings for zip %s", len(listings), zip_code)
        return listings

    except Exception as exc:
        logger.warning("Failed to process Zillow for zip %s: %s", zip_code, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_zillow_listings() -> list[RawListing]:
    """
    Fetch all active 2+BR Zillow listings across SF target zip codes.

    Returns deduplicated list of RawListing objects.
    """
    seen_ids: set[str] = set()
    all_listings: list[RawListing] = []

    for zip_code in TARGET_ZIP_CODES:
        found = _fetch_zillow_zip(zip_code)
        for item in found:
            if item.id not in seen_ids:
                seen_ids.add(item.id)
                all_listings.append(item)
        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info("Zillow Ingestion Complete: %d total listings fetched.", len(all_listings))
    return all_listings
