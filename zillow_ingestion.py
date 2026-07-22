"""
zillow_ingestion.py — Zillow Rental Listing Ingestion Engine via Apify API

Uses Apify's cloud Zillow scraper (apify/zillow-scraper) with residential proxies
to bypass PerimeterX/Cloudflare anti-bot blocks and fetch structured Zillow listings.
"""

from __future__ import annotations

import logging
import os
import re

from datetime import datetime, timezone
from typing import Optional

import requests

from ingestion import RawListing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APIFY_TOKEN: str = os.environ.get("APIFY_TOKEN", "")

# Apify Zillow ZIP Search Scraper endpoint
APIFY_ACTOR_URL = (
    "https://api.apify.com/v2/acts/maxcopell~zillow-zip-search/run-sync-get-dataset-items"
)

TARGET_ZIP_CODES: list[str] = ["94110", "94117", "94102", "94107", "94114"]
MINIMUM_PRICE_FLOOR: int = 2_400
MAXIMUM_PRICE_CAP: int = 4_400

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_price(price_val: str | int | float | None) -> Optional[int]:
    if isinstance(price_val, (int, float)):
        return int(price_val)
    if not price_val:
        return None
    match = re.search(r"\$\s?([\d,]+)", str(price_val))
    if match:
        return int(match.group(1).replace(",", ""))
    return None


def _extract_zip(address_str: str) -> str:
    match = re.search(r"\b(9\d{4})\b", str(address_str))
    return match.group(1) if match else ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_zillow_listings() -> list[RawListing]:
    """
    Fetch active Zillow 2+BR listings in SF target zip codes via Apify API.

    Returns deduplicated list of RawListing objects.
    """
    if not APIFY_TOKEN:
        logger.info("APIFY_TOKEN secret not set — skipping Zillow ingestion.")
        return []

    logger.info("Connecting to Apify Zillow ZIP Scraper API...")
    url = f"{APIFY_ACTOR_URL}?token={APIFY_TOKEN}"

    payload = {
        "zipCodes": TARGET_ZIP_CODES,
        "category": "rent",
        "status": "FOR_RENT",
        "isForRent": True,
        "isForSale": False,
        "minPrice": MINIMUM_PRICE_FLOOR,
        "maxPrice": MAXIMUM_PRICE_CAP,
        "minBeds": 2,
        "maxItems": 40,
    }

    try:
        resp = requests.post(url, json=payload, timeout=90)
        if resp.status_code != 200 and resp.status_code != 201:
            logger.warning("Apify API returned HTTP %d: %s", resp.status_code, resp.text[:200])
            return []

        dataset_items = resp.json()
        if not isinstance(dataset_items, list):
            logger.warning("Apify dataset items invalid response format.")
            return []

        seen_ids: set[str] = set()
        listings: list[RawListing] = []

        for item in dataset_items:
            zpid = (
                str(item.get("zpid", ""))
                or str(item.get("id", ""))
                or str(item.get("hdpData", {}).get("homeInfo", {}).get("zpid", ""))
            )
            if not zpid or zpid in seen_ids:
                continue

            # Ensure listing is FOR_RENT and NOT FOR_SALE
            status_check = f"{item.get('homeStatus')} {item.get('statusType')} {item.get('statusText')} {item.get('hdpData', {}).get('homeInfo', {}).get('homeStatus')}".upper()
            if "SALE" in status_check or "SOLD" in status_check or "PENDING" in status_check:
                logger.debug("Skipping non-rental Zillow listing %s: %s", zpid, status_check)
                continue

            address = str(item.get("address", "")) or str(item.get("streetAddress", ""))
            price_raw = (
                item.get("price")
                or item.get("unformattedPrice")
                or item.get("hdpData", {}).get("homeInfo", {}).get("price")
            )
            price = _extract_price(price_raw)

            beds = item.get("bedrooms") or item.get("beds") or item.get("hdpData", {}).get("homeInfo", {}).get("bedrooms")
            if beds is not None and isinstance(beds, (int, float)) and beds < 2:
                continue

            if price and (price < MINIMUM_PRICE_FLOOR or price > MAXIMUM_PRICE_CAP):
                continue

            detail_url = (
                str(item.get("url", ""))
                or str(item.get("detailUrl", ""))
                or f"https://www.zillow.com/homedetails/{zpid}_zpid/"
            )
            if detail_url and not detail_url.startswith("http"):
                detail_url = f"https://www.zillow.com{detail_url}"

            title = f"Zillow 2+BR: {address}" if address else "Zillow Rental Listing"
            extracted_zip = _extract_zip(address) or _extract_zip(str(item))

            # Filter by target zip codes if zip is detected
            if extracted_zip and extracted_zip not in TARGET_ZIP_CODES:
                continue

            description = str(item.get("description", "")) or f"Zillow property at {address}. Beds: {beds or '2+'}."

            seen_ids.add(zpid)
            listings.append(
                RawListing(
                    id=f"zillow_{zpid}",
                    title=title,
                    price=price or 3800,
                    zip_code=extracted_zip or "94110",
                    url=detail_url,
                    description=description,
                    published_at=datetime.now(tz=timezone.utc),
                )
            )

        logger.info("Apify Zillow Ingestion Complete: %d total qualified listings.", len(listings))
        return listings

    except Exception as exc:
        logger.warning("Apify Zillow ingestion error: %s", exc)
        return []
