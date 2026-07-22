"""
main.py — Pipeline Orchestrator

Entry point for the SF Apartment Scanner. Executes the full pipeline:
  1. Ingest listings from Craigslist RSS
  2. Deduplicate against GitHub Gist state
  3. AI-audit new listings with Gemini
  4. Score and rank qualified listings
  5. Dispatch email digest via Gmail SMTP
  6. Persist updated state back to Gist
"""

from __future__ import annotations

import logging
import sys

from ingestion import fetch_listings
from state import StateManager
from auditor import audit_listings
from scorer import score_and_rank
from notifier import send_digest, send_status_update

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
    logger.info("[1/5] Ingesting listings from Craigslist search feeds...")
    raw_listings = fetch_listings()
    if not raw_listings:
        logger.info("No listings fetched. Sending status email...")
        send_status_update("No listings were found matching initial price criteria ($2,400 - $4,400).")
        return

    # ── Step 2: Deduplicate ─────────────────────────────────────────────────
    logger.info("[2/5] Deduplicating against Gist state...")
    state = StateManager()
    new_listings = state.filter_new(raw_listings)
    if not new_listings:
        logger.info("All fetched listings have already been processed in prior runs.")
        send_status_update(f"Scanned {len(raw_listings)} listings — all have already been processed in previous runs.")
        return

    # ── Step 3: AI Audit ────────────────────────────────────────────────────
    logger.info("[3/5] Running AI audit with Gemini (%d listings)...", len(new_listings))
    audited = audit_listings(new_listings)
    if not audited:
        logger.info("No listings passed AI audit. Sending status update...")
        send_status_update(f"Scanned {len(new_listings)} new listings — none passed AI scam/bedroom validation.")
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
    state.mark_seen(new_listings)
    state.save()

    logger.info("=" * 60)
    logger.info(
        "Pipeline complete. Processed %d new, %d qualified, %d emailed.",
        len(new_listings),
        len(audited),
        len(qualified),
    )
    logger.info("=" * 60)


if __name__ == "__main__":
    run()
