"""
auditor.py — AI Audit & Structured Data Extraction

Uses Google Gemini with Pydantic structured output to extract
rich metadata from each listing and filter scams, room-shares, and sub-2BR units.

Rate-limit aware: throttles to stay under free-tier RPM limits, retries on 429s,
and rotates across multiple models as fallback.
"""

from __future__ import annotations

import logging
import os
import re
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

# Model rotation pool — ordered by preference (best free-tier limits first)
# gemini-3.5-flash-lite: 15 RPM, 500 RPD (primary workhorse)
# gemini-3.5-flash:       5 RPM,  20 RPD (fallback #1)
# gemini-3.6-flash:       5 RPM,  20 RPD (fallback #2)
MODEL_POOL: list[str] = [
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]

# Low temperature for factual extraction — minimize hallucination
TEMPERATURE: float = 0.1

# Throttle: 5 seconds between calls = 12 RPM (safely under the 15 RPM limit)
API_CALL_DELAY_SECONDS: float = 5.0

# Retry config for 429 rate-limit errors
MAX_RETRIES: int = 3
BASE_RETRY_WAIT: float = 60.0  # seconds — aligns with the per-minute quota reset

# ---------------------------------------------------------------------------
# Pydantic schema — Gemini will populate this with structured output
# ---------------------------------------------------------------------------


class ListingAnalysis(BaseModel):
    is_in_sf_proper: bool = Field(
        description=(
            "True ONLY if the property is physically located inside San Francisco city limits. "
            "MUST BE FALSE for Mill Valley, Oakland, Berkeley, Sausalito, Daly City, Marin, or outside SF."
        )
    )
    is_preferred_neighborhood: bool = Field(
        description=(
            "True if located in Mission, Bernal Heights, Potrero Hill, Dogpatch, "
            "Noe Valley, Castro, Duboce Triangle, Haight, Cole Valley, or true Hayes Valley. "
            "MUST BE FALSE if located in Tenderloin, Civic Center, TenderNob, Downtown, Mid-Market, or Financial District."
        )
    )
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
    available_date: str = Field(
        description=(
            "Move-in / availability date explicitly stated in the title or text "
            "(e.g., 'Sept 15', 'August 1', 'Immediate', 'Now', 'Oct 1'). "
            "If not mentioned, default to 'Immediate / Unspecified'."
        )
    )
    move_in_window: str = Field(
        description=(
            "Availability category. Must be exactly one of: "
            "'target_fall' (September or October move-in — perfect match), "
            "'august_or_immediate' (August or immediate move-in), "
            "'late_fall' (November or later move-in), or "
            "'unspecified'."
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
# Retry-aware helper to extract wait time from 429 error messages
# ---------------------------------------------------------------------------


def _extract_retry_seconds(error_msg: str) -> float:
    """Parse 'Please retry in XX.XXs' from a Gemini 429 error message."""
    match = re.search(r"retry in ([\d.]+)s", str(error_msg))
    if match:
        return float(match.group(1)) + 2.0  # add 2s buffer
    return BASE_RETRY_WAIT


# ---------------------------------------------------------------------------
# Core audit function with retry + model rotation
# ---------------------------------------------------------------------------


def _audit_single(listing: RawListing) -> Optional[ListingAnalysis]:
    """
    Call Gemini to analyze one listing. Retries on 429 rate-limit errors
    and rotates through MODEL_POOL as fallback.

    Returns ListingAnalysis or None if all attempts fail.
    """
    client = _get_client()
    user_prompt = _build_user_prompt(listing)
    config = types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        temperature=TEMPERATURE,
        response_mime_type="application/json",
        response_schema=ListingAnalysis,
    )

    for model_id in MODEL_POOL:
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = client.models.generate_content(
                    model=model_id,
                    contents=user_prompt,
                    config=config,
                )
                analysis: ListingAnalysis = response.parsed
                return analysis

            except Exception as exc:
                error_str = str(exc)
                is_rate_limit = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str

                if is_rate_limit and attempt < MAX_RETRIES:
                    wait = _extract_retry_seconds(error_str)
                    logger.warning(
                        "  Rate limited on %s (attempt %d/%d). "
                        "Waiting %.0fs before retry...",
                        model_id, attempt, MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue

                elif is_rate_limit:
                    logger.warning(
                        "  Rate limited on %s — exhausted %d retries. "
                        "Trying next model...",
                        model_id, MAX_RETRIES,
                    )
                    break  # move to next model in pool

                else:
                    # Non-rate-limit error (e.g. 404, schema error) — skip this model
                    logger.warning(
                        "  Gemini error on %s: %s. Trying next model...",
                        model_id, error_str[:120],
                    )
                    break  # move to next model in pool

    # All models exhausted
    logger.warning("  All models failed for listing %s.", listing.id)
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

    logger.info(
        "Starting AI audit of %d listings. Throttle: %.0fs between calls "
        "(~%.0f RPM). Model pool: %s",
        total, API_CALL_DELAY_SECONDS,
        60.0 / API_CALL_DELAY_SECONDS,
        ", ".join(MODEL_POOL),
    )

    for idx, listing in enumerate(listings, start=1):
        logger.info("Auditing listing %d/%d: %s", idx, total, listing.title[:60])

        analysis = _audit_single(listing)
        if analysis is None:
            logger.warning("  → Skipped (all audit attempts failed).")
            continue

        # Apply AI-derived filters
        if not analysis.is_in_sf_proper:
            logger.info("  → Dropped: outside San Francisco city limits.")
            continue
        if not analysis.is_preferred_neighborhood:
            logger.info("  → Dropped: excluded neighborhood (e.g., Tenderloin, Civic Center, Downtown).")
            continue
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

        # Throttle between calls to stay under RPM limit
        if idx < total:
            time.sleep(API_CALL_DELAY_SECONDS)

    logger.info(
        "AI audit complete: %d/%d listings qualified.",
        len(results),
        total,
    )
    return results
