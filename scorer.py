"""
scorer.py — Recommendation Scoring Engine

Computes a composite score S ∈ [0, 100] for each qualified listing using
deterministic, mathematically-defined weighted sub-scores.

Score weights:
  - Layout Score    35% — Bedroom count
  - Price Score     30% — Rent vs target / cap
  - Location Score  20% — Neighborhood tier
  - Amenities Score 15% — Laundry, outdoor space, dishwasher
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from auditor import ListingAnalysis
from ingestion import RawListing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_RENT: int = 3_800   # 100 pts at or below this price
MAX_RENT: int = 4_400      # 0 pts at or above this price

# Location tiers: zip_code -> score (0-100)
LOCATION_TIER: dict[str, int] = {
    "94110": 100,  # Mission / Bernal Heights
    "94117": 100,  # Haight / Cole Valley / Lower Haight
    "94114": 100,  # Noe Valley / Castro / Duboce Triangle
    "94102": 90,   # Hayes Valley
    "94107": 80,   # SOMA / Potrero Hill / Dogpatch
}
DEFAULT_LOCATION_SCORE: int = 50  # Other SF zip codes

# Score threshold to qualify for email digest
SCORE_THRESHOLD: float = 60.0

# ---------------------------------------------------------------------------
# Score result
# ---------------------------------------------------------------------------


@dataclass
class ScoredListing:
    listing: RawListing
    analysis: ListingAnalysis
    total_score: float
    layout_score: float
    price_score: float
    location_score: float
    amenities_score: float


# ---------------------------------------------------------------------------
# Sub-score calculators
# ---------------------------------------------------------------------------


def _layout_score(analysis: ListingAnalysis) -> float:
    """100 pts if true 2+ bedrooms, 0 otherwise."""
    return 100.0 if analysis.true_bedroom_count >= 2 else 0.0


def _price_score(price: int) -> float:
    """
    100 pts if rent <= TARGET_RENT
     0 pts if rent >= MAX_RENT
    Linear interpolation in between.
    """
    if price <= TARGET_RENT:
        return 100.0
    if price >= MAX_RENT:
        return 0.0
    # Linear: 100 * (1 - (price - target) / (max - target))
    return 100.0 * (1.0 - (price - TARGET_RENT) / (MAX_RENT - TARGET_RENT))


def _location_score(zip_code: str) -> float:
    """Return tier score for the given zip, or default for unknown SF zips."""
    return float(LOCATION_TIER.get(zip_code, DEFAULT_LOCATION_SCORE))


def _amenities_score(analysis: ListingAnalysis) -> float:
    """
    Additive amenities score, capped at 100:
      +35  in-unit laundry
      +35  outdoor space (deck/patio/yard)
      +30  dishwasher
    """
    score = 0
    if analysis.laundry_type == "in-unit":
        score += 35
    if analysis.has_outdoor_space:
        score += 35
    if analysis.has_dishwasher:
        score += 30
    return float(min(score, 100))


# ---------------------------------------------------------------------------
# Composite scorer
# ---------------------------------------------------------------------------


def score_listing(listing: RawListing, analysis: ListingAnalysis) -> ScoredListing:
    """Compute and return a fully scored listing."""
    s_layout = _layout_score(analysis)
    s_price = _price_score(listing.price)
    s_location = _location_score(listing.zip_code)
    s_amenities = _amenities_score(analysis)

    # Move-in availability bonus for top sorting priority
    move_in_bonus = 0.0
    if analysis.move_in_window == "target_fall":
        move_in_bonus = 10.0  # September / October perfect matches
    elif analysis.move_in_window == "late_fall":
        move_in_bonus = 5.0

    total = (
        0.35 * s_layout
        + 0.30 * s_price
        + 0.20 * s_location
        + 0.15 * s_amenities
        + move_in_bonus
    )
    total = min(total, 100.0)

    scored = ScoredListing(
        listing=listing,
        analysis=analysis,
        total_score=round(total, 1),
        layout_score=round(s_layout, 1),
        price_score=round(s_price, 1),
        location_score=round(s_location, 1),
        amenities_score=round(s_amenities, 1),
    )

    logger.debug(
        "Scored '%s': total=%.1f (layout=%.0f, price=%.0f, loc=%.0f, amenities=%.0f)",
        listing.title[:50],
        scored.total_score,
        s_layout,
        s_price,
        s_location,
        s_amenities,
    )
    return scored


def score_and_rank(
    audited: list[tuple[RawListing, ListingAnalysis]],
) -> list[ScoredListing]:
    """
    Score all audited listings, filter by SCORE_THRESHOLD,
    and return sorted descending by total_score.
    If no listings pass threshold, returns top 5 scored listings as fallback.
    """
    all_scored = [score_listing(listing, analysis) for listing, analysis in audited]
    all_scored.sort(key=lambda s: s.total_score, reverse=True)

    qualified = [s for s in all_scored if s.total_score >= SCORE_THRESHOLD]

    if not qualified and all_scored:
        logger.info("No listings above threshold %.0f. Returning top %d scored listings as fallback.", SCORE_THRESHOLD, min(5, len(all_scored)))
        return all_scored[:5]

    logger.info(
        "Scoring complete: %d/%d listings qualified.",
        len(qualified),
        len(all_scored),
    )
    return qualified
