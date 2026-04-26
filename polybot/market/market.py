"""Market discovery — finds active trading windows.

The slug number in {prefix}-{N} IS the Unix epoch of the window's start time.
Since we can compute the exact slug, we query the Gamma API by slug directly —
no need for batch fetching 1000 markets.
"""

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from polybot.core import config
from polybot.core.log_formatter import MARKET, log_event
from .series import MarketSeries

log = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com/markets"
UTC = datetime.timezone.utc


@dataclass
class MarketWindow:
    """Represents a single trading window."""

    question: str
    up_token: str  # token ID for "Up" outcome
    down_token: str  # token ID for "Down" outcome
    start_time: datetime.datetime  # UTC-aware
    end_time: datetime.datetime  # UTC-aware
    slug: str

    @property
    def short_label(self) -> str:
        """Human-readable window label."""
        for prefix in ("Bitcoin Up or Down - ", "Ethereum Up or Down - "):
            if self.question.startswith(prefix):
                return self.question[len(prefix):]
        return self.question

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
            for market in data:
                if market.get("slug") == slug:
                    return market
        return None
    except Exception as e:
        log.warning("Failed to fetch market %s: %s", slug, e)
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


def _build_window(m: dict, series: Optional[MarketSeries] = None) -> Optional[MarketWindow]:
    """Build a MarketWindow from a raw market dict, or return None if invalid."""
    tokens = _parse_tokens(m.get("clobTokenIds", []))
    if not tokens or len(tokens) < 2:
        return None

    end_dt = _parse_dt(m.get("endDate", ""))
    if end_dt is None:
        return None

    start_dt = _parse_dt(m.get("eventStartTime", m.get("endDate", "")))
    if start_dt is None:
        fallback_duration = (
            datetime.timedelta(seconds=series.slug_step) if series else datetime.timedelta(minutes=5)
        )
        start_dt = end_dt - fallback_duration

    return MarketWindow(
        question=m.get("question", ""),
        up_token=tokens[0],
        down_token=tokens[1],
        start_time=start_dt,
        end_time=end_dt,
        slug=m.get("slug", ""),
    )


def _epoch_to_slug(n: int, series: Optional[MarketSeries] = None) -> str:
    """Convert a Unix epoch to the corresponding slug."""
    if series is not None:
        return series.epoch_to_slug(n)
    return f"{config.SERIES_SLUG_PREFIX}-{n}"


def _scan_forward(
    from_epoch: int,
    series: Optional[MarketSeries] = None,
    max_windows: int = 12,
    include_future: bool = False,
) -> Optional[MarketWindow]:
    """Scan forward from a given epoch, querying one slug at a time.

    Stops at the first matching window. By default this means an active,
    not-yet-expired window. When include_future=True, future windows are also
    allowed as long as they are not closed and their start is at or after the
    requested epoch.
    """
    now = datetime.datetime.now(UTC)
    slug_step = series.slug_step if series else config.SLUG_STEP
    base_epoch = (from_epoch // slug_step) * slug_step

    for offset in range(max_windows):
        candidate_epoch = base_epoch + offset * slug_step
        slug = _epoch_to_slug(candidate_epoch, series)

        m = _fetch_market_by_slug(slug)
        if m is None:
            continue
        if m.get("closed"):
            continue

        window = _build_window(m, series)
        if window is None:
            continue
        if window.end_time <= now:
            continue

        if include_future:
            if window.start_epoch < from_epoch:
                continue
            return window

        if not m.get("active"):
            continue

        return window

    return None


def find_next_window(series: Optional[MarketSeries] = None) -> Optional[MarketWindow]:
    """
    Find the next active trading window.

    Computes the current window boundary epoch, then queries Gamma API
    by exact slug — one lightweight request per candidate window.
    """
    now = datetime.datetime.now(UTC)
    now_epoch = int(now.timestamp())
    slug_step = series.slug_step if series else config.SLUG_STEP
    current_start_epoch = (now_epoch // slug_step) * slug_step

    window = _scan_forward(current_start_epoch, series)
    if window is None:
        series_info = series.series_key if series else "BTC 5-min"
        log_event(log, logging.WARNING, MARKET, {
            "action": "NOT_FOUND",
            "message": f"No active {series_info} window found in scan range",
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


def find_window_after(after_epoch: int, series: Optional[MarketSeries] = None) -> Optional[MarketWindow]:
    """Find the first window that starts at or after the given epoch.

    Uses ceiling division so that if after_epoch is exactly on a boundary
    (e.g. window end == next window start), that boundary is included rather
    than skipped.
    """
    slug_step = series.slug_step if series else config.SLUG_STEP
    # Ceiling division: round up to next boundary, but include current boundary
    next_boundary = -(-after_epoch // slug_step) * slug_step
    window = _scan_forward(next_boundary, series, include_future=True)
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
