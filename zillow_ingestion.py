"""
zillow_ingestion.py — Zillow Rental Listing Ingestion Engine via Playwright Stealth

Launches a headless Chromium browser with stealth evasions to navigate Zillow rental search pages,
bypass Cloudflare/PerimeterX challenges, and parse listings from __NEXT_DATA__ JSON state.
"""

from __future__ import annotations

import logging
import re
import json
import time
from datetime import datetime, timezone
from typing import Optional

from playwright.sync_api import sync_playwright
import playwright_stealth

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_price(price_str: str) -> Optional[int]:
    match = re.search(r"\$\s?([\d,]+)", str(price_str))
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
    Fetch active Zillow listings across SF target zip codes using Playwright Chromium with Stealth evasions.
    """
    seen_ids: set[str] = set()
    all_listings: list[RawListing] = []

    logger.info("Starting Playwright Stealth Chromium browser for Zillow ingestion...")
    stealth_engine = playwright_stealth.Stealth()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--window-size=1280,800",
                ],
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            stealth_engine.apply_stealth_sync(page)

            # Pre-visit Zillow home page to initialize cookies
            try:
                page.goto("https://www.zillow.com/", wait_until="domcontentloaded", timeout=20000)
                time.sleep(1.5)
            except Exception:
                pass

            for zip_code in TARGET_ZIP_CODES:
                url = ZILLOW_ZIP_SEARCH_TEMPLATE.format(zip_code=zip_code)
                logger.info("Playwright Stealth navigating to Zillow zip %s: %s", zip_code, url)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    time.sleep(2.5)  # allow JS hydration

                    script_element = page.query_selector('script[id="__NEXT_DATA__"]')
                    if not script_element:
                        logger.info("No __NEXT_DATA__ script found on Zillow for zip %s — skipping.", zip_code)
                        continue

                    json_text = script_element.inner_text()
                    data = json.loads(json_text)

                    page_props = data.get("props", {}).get("pageProps", {})
                    search_state = page_props.get("searchPageState", {})
                    list_results = (
                        search_state.get("cat1", {})
                        .get("searchResults", {})
                        .get("listResults", [])
                    )

                    zip_count = 0
                    for item in list_results:
                        zpid = str(item.get("zpid", "")) or str(item.get("id", ""))
                        if not zpid or zpid in seen_ids:
                            continue

                        address = str(item.get("address", ""))
                        price_raw = item.get("price", "") or item.get("unformattedPrice", "")
                        price = _extract_price(price_raw)

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

                        seen_ids.add(zpid)
                        zip_count += 1
                        all_listings.append(
                            RawListing(
                                id=f"zillow_{zpid}",
                                title=title,
                                price=price or 3800,
                                zip_code=extracted_zip,
                                url=detail_url or url,
                                description=f"Zillow property at {address}. Beds: {beds or '2+'}.",
                                published_at=datetime.now(tz=timezone.utc),
                            )
                        )

                    logger.info("Playwright Stealth parsed %d Zillow listings for zip %s", zip_count, zip_code)

                except Exception as exc:
                    logger.warning("Playwright error for zip %s: %s", zip_code, exc)

            browser.close()

    except Exception as exc:
        logger.error("Playwright browser launch error: %s", exc)

    logger.info("Playwright Zillow Ingestion Complete: %d total listings.", len(all_listings))
    return all_listings
