"""
state.py — GitHub Gist State Management & Deduplication

Persists seen listing IDs in a GitHub Gist as a time-stamped JSON map.
Auto-prunes entries older than TTL_DAYS to prevent unbounded growth.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GIST_TOKEN: str = os.environ["GIST_TOKEN"]
GIST_ID: str = os.environ["GIST_ID"]
GIST_FILENAME: str = "sf_seen_listings.json"

# Listings older than this are pruned from state to prevent bloat
TTL_DAYS: int = 45

GIST_API_URL = f"https://api.github.com/gists/{GIST_ID}"
_HEADERS = {
    "Authorization": f"Bearer {GIST_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ---------------------------------------------------------------------------
# State schema: {"seen": {"listing_id": "ISO-8601 timestamp", ...}}
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _is_expired(timestamp_str: str) -> bool:
    """Return True if the timestamp is older than TTL_DAYS."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        return (datetime.now(tz=timezone.utc) - ts) > timedelta(days=TTL_DAYS)
    except (ValueError, TypeError):
        return True  # treat malformed timestamps as expired


# ---------------------------------------------------------------------------
# Gist I/O
# ---------------------------------------------------------------------------


def _read_gist() -> dict[str, Any]:
    """Fetch the raw JSON state from the Gist. Returns empty state on failure."""
    try:
        resp = requests.get(GIST_API_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        files = resp.json().get("files", {})
        if GIST_FILENAME not in files:
            logger.warning("Gist file '%s' not found — starting fresh.", GIST_FILENAME)
            return {"seen": {}}
        content = files[GIST_FILENAME].get("content", "{}")
        return json.loads(content)
    except (requests.RequestException, json.JSONDecodeError) as exc:
        logger.error("Failed to read Gist state: %s", exc)
        return {"seen": {}}


def _write_gist(state: dict[str, Any]) -> None:
    """Persist the state dict back to the Gist."""
    payload = {
        "files": {
            GIST_FILENAME: {
                "content": json.dumps(state, indent=2)
            }
        }
    }
    try:
        resp = requests.patch(GIST_API_URL, headers=_HEADERS, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Gist state persisted successfully (%d seen IDs).", len(state.get("seen", {})))
    except requests.RequestException as exc:
        logger.error("Failed to write Gist state: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class StateManager:
    """
    Manages the persistent set of seen listing IDs stored in a GitHub Gist.

    Usage:
        sm = StateManager()
        new_listings = sm.filter_new(all_raw_listings)
        sm.mark_seen(new_listings)
        sm.save()
    """

    def __init__(self) -> None:
        self._state = _read_gist()
        self._seen: dict[str, str] = self._state.get("seen", {})
        self._prune_expired()

    def _prune_expired(self) -> None:
        """Remove entries older than TTL_DAYS from in-memory state."""
        before = len(self._seen)
        self._seen = {
            k: v for k, v in self._seen.items()
            if not _is_expired(v)
        }
        pruned = before - len(self._seen)
        if pruned:
            logger.info("Pruned %d expired listing IDs (TTL=%d days).", pruned, TTL_DAYS)

    def filter_new(self, listings: list) -> list:
        """Return only listings whose ID has not been seen before."""
        new = [l for l in listings if l.id not in self._seen]
        logger.info(
            "Deduplication: %d incoming → %d new (skipped %d already-seen).",
            len(listings),
            len(new),
            len(listings) - len(new),
        )
        return new

    def mark_seen(self, listings: list) -> None:
        """Add listing IDs to the in-memory seen set with current timestamp."""
        now = _now_iso()
        for listing in listings:
            self._seen[listing.id] = now

    def save(self) -> None:
        """Persist the current in-memory state back to the GitHub Gist."""
        self._state["seen"] = self._seen
        _write_gist(self._state)
