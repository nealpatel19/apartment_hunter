"""
auditor.py — AI Audit & Structured Data Extraction

Uses Google Gemini 2.5 Flash with Pydantic structured output to extract
rich metadata from each listing and filter scams, room-shares, and sub-2BR units.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from ingestion import RawListing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
MODEL_ID: str = "gemini-2.5-flash-latest"

# Low temperature for factual extraction — minimize hallucination
TEMPERATURE: float = 0.1

# Delay between Gemini calls to avoid rate-limit bursts
API_CALL_DELAY_SECONDS: float = 0.5

# ---------------------------------------------------------------------------
# Pydantic schema — Gemini will populate this with structured output
# ---------------------------------------------------------------------------


class ListingAnalysis(BaseModel):
    is_scam_or_fishy: bool = Field(
        description=(
            "True if the listing shows signs of fraud, impossibly low rent, "
            "requests wire transfers, is posted from a non-SF location, or "
            "otherwise appears illegitimate."
        )
    )
    is_room_share: bool = Field(
        description=(
            "True if the post is offering only a single bedroom in a shared "
            "unit (e.g., 'private room in 2BR flat', 'seeking roommate', "
            "'1 room available'). Must be False for a full-unit 2BR lease."
        )
    )
    true_bedroom_count: int = Field(
        description=(
            "Actual number of private bedrooms in the full unit being rented. "
            "If unclear, default to 0."
        )
    )
    laundry_type: str = Field(
        description=(
            "Laundry situation. Must be exactly one of: "
            "'in-unit', 'in-building', or 'none'."
        )
    )
    has_dishwasher: bool = Field(
        description="True if the listing explicitly mentions a dishwasher."
    )
    has_outdoor_space: bool = Field(
        description=(
            "True if the listing mentions a private or shared deck, patio, "
            "balcony, yard, or garden."
        )
    )
    parking_monthly_fee: int = Field(
        description=(
            "Monthly parking cost in USD. 0 if parking is included free or "
            "not mentioned. Use the explicit fee if stated."
        )
    )
    ai_pro_bullet: str = Field(
        description=(
            "One concise, specific sentence highlighting the single strongest "
            "reason to apply for this listing."
        )
    )
    ai_con_bullet: str = Field(
        description=(
            "One concise, specific sentence describing the biggest drawback or "
            "red flag of this listing."
        )
    )
    custom_outreach_pitch: str = Field(
        description=(
            "A professional, warm 3-sentence message from a prospective tenant "
            "to the landlord requesting a viewing. Tailor it to specific details "
            "in the listing (location, features, price). Do NOT include placeholders."
        )
    )


# ---------------------------------------------------------------------------
# Internal prompt builder
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert San Francisco rental market analyst helping a prospective tenant
evaluate Craigslist apartment listings. Analyze the listing below with precision.
Your job is to extract factual information — do NOT make up details not present in the listing.
When uncertain, prefer conservative defaults (e.g., true_bedroom_count=0 if genuinely unclear).
"""


def _build_user_prompt(listing: RawListing) -> str:
    return f"""\
Analyze this San Francisco rental listing:

TITLE: {listing.title}
PRICE: ${listing.price}/month
ZIP CODE: {listing.zip_code}
URL: {listing.url}

DESCRIPTION:
{listing.description}

Extract all requested structured fields. Be thorough but accurate.
"""


# ---------------------------------------------------------------------------
# Gemini client (initialized once)
# ---------------------------------------------------------------------------

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Core audit function
# ---------------------------------------------------------------------------


def _audit_single(listing: RawListing) -> Optional[ListingAnalysis]:
    """
    Call Gemini to analyze one listing. Returns ListingAnalysis or None on error.
    """
    client = _get_client()
    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=_build_user_prompt(listing),
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                temperature=TEMPERATURE,
                response_mime_type="application/json",
                response_schema=ListingAnalysis,
            ),
        )
        analysis: ListingAnalysis = response.parsed
        return analysis
    except Exception as exc:
        logger.warning("Gemini audit failed for listing %s: %s", listing.id, exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_listings(
    listings: list[RawListing],
) -> list[tuple[RawListing, ListingAnalysis]]:
    """
    Audit each listing with Gemini AI. Returns only listings that pass all filters:
      - Not a scam/fishy listing
      - Not a room-share
      - true_bedroom_count >= 2

    Returns a list of (RawListing, ListingAnalysis) tuples for qualified listings.
    """
    results: list[tuple[RawListing, ListingAnalysis]] = []
    total = len(listings)

    for idx, listing in enumerate(listings, start=1):
        logger.info("Auditing listing %d/%d: %s", idx, total, listing.title[:60])

        analysis = _audit_single(listing)
        if analysis is None:
            logger.warning("  → Skipped (audit error).")
            continue

        # Apply AI-derived filters
        if analysis.is_scam_or_fishy:
            logger.info("  → Dropped: flagged as scam/fishy.")
            continue
        if analysis.is_room_share:
            logger.info("  → Dropped: flagged as room-share.")
            continue
        if analysis.true_bedroom_count < 2:
            logger.info(
                "  → Dropped: only %d true bedroom(s) detected.",
                analysis.true_bedroom_count,
            )
            continue

        logger.info("  → Qualified: %d BR, laundry=%s", analysis.true_bedroom_count, analysis.laundry_type)
        results.append((listing, analysis))

        # Be a good API citizen
        if idx < total:
            time.sleep(API_CALL_DELAY_SECONDS)

    logger.info(
        "AI audit complete: %d/%d listings qualified.",
        len(results),
        total,
    )
    return results
