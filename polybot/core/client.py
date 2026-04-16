"""CLOB client singleton — wraps ClobClient for the trading bot."""

import asyncio
import logging
from typing import Optional

from py_clob_client.client import ClobClient

from . import config
from .auth import create_clob_client

log = logging.getLogger(__name__)

_client: Optional[ClobClient] = None

# Tick size cache: token_id -> tick_size (rarely changes, safe to cache for session)
_tick_size_cache: dict[str, float] = {}


def get_client() -> ClobClient:
    """Return the singleton CLOB client (lazy init)."""
    global _client
    if _client is None:
        _client = create_clob_client()
        log.info("CLOB client initialized")
    return _client


def get_midpoint(token_id: str) -> Optional[float]:
    """Fetch the current midpoint price for a token (blocking)."""
    try:
        val = get_client().get_midpoint(token_id)
        if val is None:
            return None
        # API returns {'mid': '0.505'} or a plain number
        if isinstance(val, dict):
            val = val.get("mid", val.get("price", 0))
        return float(val)
    except Exception as e:
        log.warning("get_midpoint failed for %s: %s", token_id, e)
        return None


async def get_midpoint_async(token_id: str) -> Optional[float]:
    """Non-blocking midpoint fetch via thread pool."""
    return await asyncio.to_thread(get_midpoint, token_id)


def get_tick_size(token_id: str) -> float:
    """Get the tick size for a token (cached per session)."""
    cached = _tick_size_cache.get(token_id)
    if cached is not None:
        return cached
    try:
        ts = get_client().get_tick_size(token_id)
        val = float(ts)
        _tick_size_cache[token_id] = val
        return val
    except Exception:
        return 0.001  # default fallback


def round_to_tick(price: float, token_id: str) -> float:
    """Round a price to the nearest valid tick for the given token."""
    tick = get_tick_size(token_id)
    if tick <= 0:
        tick = 0.001
    rounded = round(price / tick) * tick
    # Clamp to [0, 1]
    return max(0.0, min(1.0, rounded))
