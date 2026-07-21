"""
main.py — Pipeline Orchestrator

Entry point for the SF Apartment Scanner. Executes the full pipeline:
  1. Ingest listings from Craigslist RSS
  2. Deduplicate against GitHub Gist state
  3. AI-audit new listings with Gemini
  4. Score and rank qualified listings
  5. Dispatch email digest via Resend
  6. Persist updated state back to Gist
"""

from __future__ import annotations

import logging
import sys

from ingestion import fetch_listings
from state import StateManager
from auditor import audit_listings
from scorer import score_and_rank
from notifier import send_digest

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def run() -> None:
    logger.info("=" * 60)
    logger.info("SF Apartment Scanner — Pipeline Starting")
    logger.info("=" * 60)

    # ── Step 1: Ingest ──────────────────────────────────────────────────────
    logger.info("[1/5] Ingesting listings from Craigslist RSS feeds...")
    raw_listings = fetch_listings()
    if not raw_listings:
        logger.info("No listings fetched. Exiting early.")
        return

    # ── Step 2: Deduplicate ─────────────────────────────────────────────────
    logger.info("[2/5] Deduplicating against Gist state...")
    state = StateManager()
    new_listings = state.filter_new(raw_listings)
    if not new_listings:
        logger.info("All listings already seen. Nothing to process. Exiting.")
        return

    # ── Step 3: AI Audit ────────────────────────────────────────────────────
    logger.info("[3/5] Running AI audit with Gemini 2.5 Flash (%d listings)...", len(new_listings))
    audited = audit_listings(new_listings)
    if not audited:
        logger.info("No listings passed AI audit. Persisting seen IDs and exiting.")
        state.mark_seen(new_listings)
        state.save()
        return

    # ── Step 4: Score & Rank ────────────────────────────────────────────────
    logger.info("[4/5] Scoring and ranking %d audited listings...", len(audited))
    qualified = score_and_rank(audited)

    # ── Step 5: Notify ──────────────────────────────────────────────────────
    logger.info("[5/5] Dispatching email digest (%d qualified listings)...", len(qualified))
    send_digest(qualified)

    # ── Persist state ───────────────────────────────────────────────────────
    # Always mark ALL new listings (including filtered ones) as seen
    # so we don't re-audit scams or under-threshold listings again
    state.mark_seen(new_listings)
    state.save()

    logger.info("=" * 60)
    logger.info(
        "Pipeline complete. Processed %d new, %d qualified, %d emailed.",
        len(new_listings),
        len([a for a in audited]),
        len(qualified),
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
