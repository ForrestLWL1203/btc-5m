"""Market discovery — finds active BTC 5-min windows.

The slug number in btc-updown-5m-{N} IS the Unix epoch of the window's start time.
Since we can compute the exact slug, we query the Gamma API by slug directly —
no need for batch fetching 1000 markets.
"""

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from . import config
from .log_formatter import MARKET, log_event

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/markets"
UTC = datetime.timezone.utc


@dataclass
class MarketWindow:
    """Represents a single 5-minute trading window."""

    question: str
    up_token: str  # token ID for "Up" outcome
    down_token: str  # token ID for "Down" outcome
    start_time: datetime.datetime  # UTC-aware
    end_time: datetime.datetime  # UTC-aware
    slug: str

    @property
    def short_label(self) -> str:
        """Human-readable window label."""
        return self.question.replace("Bitcoin Up or Down - ", "")

    @property
    def start_epoch(self) -> int:
        return int(self.start_time.timestamp())

    @property
    def end_epoch(self) -> int:
        return int(self.end_time.timestamp())


def _fetch_market_by_slug(slug: str) -> Optional[dict]:
    """Fetch a single market by its exact slug from Gamma API.

    Returns the raw market dict, or None if not found / on error.
    """
    try:
        resp = requests.get(GAMMA_API, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception as e:
        log.debug("Failed to fetch market %s: %s", slug, e)
        return None


def _parse_tokens(raw_tokens) -> list:
    """Parse clobTokenIds which can be a JSON string or a Python list."""
    if isinstance(raw_tokens, str):
        return json.loads(raw_tokens)
    return list(raw_tokens)


def _parse_dt(s: str) -> Optional[datetime.datetime]:
    """Parse an ISO datetime string, return a UTC-aware datetime."""
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        else:
            dt = dt.astimezone(UTC)
        return dt
    except Exception:
        return None


def _build_window(m: dict) -> Optional[MarketWindow]:
    """Build a MarketWindow from a raw market dict, or return None if invalid."""
    tokens = _parse_tokens(m.get("clobTokenIds", []))
    if not tokens or len(tokens) < 2:
        return None

    end_dt = _parse_dt(m.get("endDate", ""))
    if end_dt is None:
        return None

    start_dt = _parse_dt(m.get("eventStartTime", m.get("endDate", "")))
    if start_dt is None:
        start_dt = end_dt - datetime.timedelta(minutes=5)

    return MarketWindow(
        question=m.get("question", ""),
        up_token=tokens[0],
        down_token=tokens[1],
        start_time=start_dt,
        end_time=end_dt,
        slug=m.get("slug", ""),
    )


def _epoch_to_slug(n: int) -> str:
    """Convert a Unix epoch to the corresponding slug."""
    return f"{config.SERIES_SLUG_PREFIX}-{n}"


def _scan_forward(from_epoch: int, max_windows: int = 12) -> Optional[MarketWindow]:
    """Scan forward from a given epoch, querying one slug at a time.

    Stops at the first active, not-yet-expired window.  Each iteration is a
    single lightweight Gamma API call (1 result, not 1000).
    """
    now = datetime.datetime.now(UTC)
    base_epoch = (from_epoch // config.SLUG_STEP) * config.SLUG_STEP

    for offset in range(max_windows):
        candidate_epoch = base_epoch + offset * config.SLUG_STEP
        slug = _epoch_to_slug(candidate_epoch)

        m = _fetch_market_by_slug(slug)
        if m is None:
            continue
        if not m.get("active") or m.get("closed"):
            continue

        end_dt = _parse_dt(m.get("endDate", ""))
        if end_dt is None or end_dt <= now:
            continue

        window = _build_window(m)
        if window is None:
            continue

        return window

    return None


def find_next_window() -> Optional[MarketWindow]:
    """
    Find the next active BTC 5-min window.

    Computes the current 5-minute boundary epoch, then queries Gamma API
    by exact slug — one lightweight request per candidate window.
    """
    now = datetime.datetime.now(UTC)
    now_epoch = int(now.timestamp())
    current_start_epoch = (now_epoch // config.SLUG_STEP) * config.SLUG_STEP

    log.info(
        "Current time: %s UTC (epoch %d), window start: %d",
        now.strftime("%H:%M:%S"),
        now_epoch,
        current_start_epoch,
    )

    window = _scan_forward(current_start_epoch)
    if window is None:
        log_event(log, logging.WARNING, MARKET, {
            "action": "NOT_FOUND",
            "message": "No active BTC 5-min window found in scan range",
        })
        return None

    end_dt = window.end_time
    log_event(log, logging.INFO, MARKET, {
        "action": "FOUND",
        "window": window.short_label,
        "ends": end_dt.strftime("%H:%M"),
        "away": str(end_dt - now),
    })
    return window


def find_window_after(after_epoch: int) -> Optional[MarketWindow]:
    """Find the first window that starts at or after the given epoch.

    Uses ceiling division so that if after_epoch is exactly on a 5-minute
    boundary (e.g. window end == next window start), that boundary is
    included rather than skipped.
    """
    # Ceiling division: round up to next boundary, but include current boundary
    next_boundary = -(-after_epoch // config.SLUG_STEP) * config.SLUG_STEP
    window = _scan_forward(next_boundary)
    if window is None:
        log_event(log, logging.WARNING, MARKET, {
            "action": "NOT_FOUND",
            "message": f"No window found after epoch {after_epoch}",
        })
    return window


def get_window_by_slug(slug: str) -> Optional[MarketWindow]:
    """Direct lookup by slug string (e.g. 'btc-updown-5m-1776235500')."""
    m = _fetch_market_by_slug(slug)
    if m is None:
        return None

    if not m.get("active") or m.get("closed"):
        return None

    now = datetime.datetime.now(UTC)
    end_dt = _parse_dt(m.get("endDate", ""))
    if end_dt is None or end_dt <= now:
        return None

    return _build_window(m)
