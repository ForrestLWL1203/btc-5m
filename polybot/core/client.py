"""CLOB client singleton — wraps ClobClient for the trading bot."""

import asyncio
import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import PartialCreateOrderOptions

from . import config
from .auth import create_clob_client

log = logging.getLogger(__name__)

_client: Optional[ClobClient] = None

# Tick size cache: token_id -> tick_size (rarely changes, safe to cache for session)
_tick_size_cache: dict[str, float] = {}
# Pre-fetched order params: token_id -> (tick_size_str, neg_risk, fee_rate_bps)
_order_params_cache: dict[str, tuple[str, bool, int]] = {}


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
            val = val.get("mid", val.get("price"))
        if val is None:
            return None
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


def get_token_balance(token_id: str, safe: bool = True) -> Optional[float]:
    """Fetch the share balance for a token from the CLOB API.

    Args:
        safe: If True, truncate and subtract tick to avoid overselling.
              If False, return raw balance (for cleanup/diagnostic use).
    """
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
        resp = get_client().get_balance_allowance(params)
        if resp and "balance" in resp:
            raw = float(resp["balance"]) / 1_000_000
            if not safe:
                return raw
            # Truncate to 4 decimal places, then subtract safety margin
            # Use proportional margin: min(tick, 1% of raw) to avoid destroying small balances
            tick = get_tick_size(token_id)
            truncated = int(raw * 10_000) / 10_000  # floor to 4 decimals
            safe_balance = max(0.0, truncated - min(tick, raw * 0.01))
            return safe_balance
        return None
    except Exception as e:
        log.warning("get_token_balance failed for %s: %s", token_id, e)
        return None


def round_to_tick(price: float, token_id: str) -> float:
    """Round a price to the nearest valid tick for the given token."""
    tick = get_tick_size(token_id)
    if tick <= 0:
        tick = 0.001
    rounded = round(price / tick) * tick
    # Clamp to [0, 1]
    return max(0.0, min(1.0, rounded))


def prefetch_order_params(token_id: str) -> None:
    """Pre-fetch tick_size, neg_risk, fee_rate for a token.

    Populates SDK internal caches so create_market_order / create_order
    skip redundant API calls during order placement.
    """
    if token_id in _order_params_cache:
        return
    try:
        client = get_client()
        # Populate SDK caches
        tick_str = client.get_tick_size(token_id)
        tick_val = float(tick_str)
        _tick_size_cache[token_id] = tick_val

        neg_risk = client.get_neg_risk(token_id)

        # Fee rate: populate SDK's internal __fee_rates cache
        client.get_fee_rate_bps(token_id)

        _order_params_cache[token_id] = (tick_str, bool(neg_risk), 0)
        log.debug("Prefetched order params for %s: tick=%s neg_risk=%s",
                  token_id[:20], tick_str, neg_risk)
    except Exception as e:
        log.debug("Prefetch failed for %s: %s", token_id[:20], e)


def get_order_options(token_id: str) -> Optional[PartialCreateOrderOptions]:
    """Return cached PartialCreateOrderOptions to skip SDK internal lookups."""
    cached = _order_params_cache.get(token_id)
    if cached is None:
        return None
    tick_str, neg_risk, _ = cached
    return PartialCreateOrderOptions(tick_size=tick_str, neg_risk=neg_risk)
