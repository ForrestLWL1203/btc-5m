"""Reusable FAK quote selection helpers.

These helpers choose executable price hints from cached Polymarket order-book
depth. They do not know about strategy signals, window lifecycle, or position
state, so different strategies can reuse the same entry and stop-loss mechanics.
"""

import math
from dataclasses import dataclass
from typing import Optional

from polybot.core import config
from polybot.core.client import get_tick_size
from polybot.market.stream import PriceStream


DEPTH_ENTRY_SKIP_LEVELS = 1
DEPTH_PREVIEW_LEVELS = 6


@dataclass(frozen=True)
class CapDepthQuote:
    """Cap-limited book depth quote for target-leg entry."""

    price: Optional[float]
    price_hint: Optional[float]
    cap_notional: float
    levels_used: int
    total_levels: int
    skipped_levels: int
    entry_ask_level: int
    best_ask_level_1: Optional[float]
    ask_age_sec: Optional[float]
    preview: list[tuple[float, float]]
    enough: bool


@dataclass(frozen=True)
class BidDepthQuote:
    """Bid-book depth quote for stop-loss SELL execution."""

    price: Optional[float]
    price_hint: Optional[float]
    shares_available: float
    levels_used: int
    total_levels: int
    skipped_levels: int
    sell_bid_level: int
    best_bid_level_1: Optional[float]
    bid_age_sec: Optional[float]
    preview: list[tuple[float, float]]
    enough: bool


def buffer_buy_price_hint(
    token_id: str,
    best_ask: Optional[float],
    buffer_ticks: Optional[float] = None,
    max_price: Optional[float] = None,
) -> Optional[float]:
    """Add a small upward tick buffer to the BUY hint."""
    if best_ask is None:
        return None
    tick = get_tick_size(token_id)
    if tick <= 0:
        tick = 0.001
    ticks = config.PRICE_HINT_BUFFER_TICKS if buffer_ticks is None else buffer_ticks
    buffered = best_ask + tick * ticks
    if max_price is not None:
        buffered = min(buffered, max_price)
    return max(0.0, min(1.0, math.ceil(buffered / tick) * tick))


def buffer_sell_price_hint(
    token_id: str,
    bid_price: Optional[float],
    *,
    buffer_ticks: Optional[float] = None,
    min_price: Optional[float] = None,
) -> Optional[float]:
    """Move a SELL hint below the selected bid to improve FAK fill odds."""
    if bid_price is None:
        return None
    tick = get_tick_size(token_id)
    if tick <= 0:
        tick = 0.001
    ticks = config.FAK_RETRY_PRICE_HINT_BUFFER_TICKS if buffer_ticks is None else buffer_ticks
    buffered = bid_price - tick * ticks
    if min_price is not None:
        buffered = max(buffered, min_price)
    return max(0.0, min(1.0, math.floor(buffered / tick) * tick))


def dynamic_entry_level(
    best_ask: Optional[float],
    base_entry_level: int,
    dynamic_entry_levels: Optional[list[tuple[float, int]]],
) -> int:
    """Return dynamic ask-book scan depth for the current target-leg best ask."""
    selected = max(1, int(base_entry_level))
    if best_ask is None or not dynamic_entry_levels:
        return selected
    for threshold, level in dynamic_entry_levels:
        if best_ask <= threshold:
            return max(1, int(level))
    return selected


def _best_ask_level_1(ws: PriceStream, token_id: str) -> Optional[float]:
    try:
        return ws.get_latest_best_ask(token_id, max_age_sec=None, level=1)
    except TypeError:
        return ws.get_latest_best_ask(token_id)


def cap_limited_depth_quote(
    ws: PriceStream,
    token_id: str,
    amount: float,
    max_entry_price: Optional[float],
    *,
    max_age_sec: Optional[float] = None,
    skip_levels: int = DEPTH_ENTRY_SKIP_LEVELS,
    min_entry_level: int = 1,
    low_price_threshold: Optional[float] = None,
    low_price_entry_level: Optional[int] = None,
    dynamic_entry_levels: Optional[list[tuple[float, int]]] = None,
    max_slippage_from_best_ask: Optional[float] = None,
    buffer_ticks: Optional[float] = None,
) -> CapDepthQuote:
    """Return the first ask level where cap-limited depth can cover amount.

    Level 1 is deliberately excluded from fillability calculations because it
    often disappears before the FAK reaches Polymarket. ``min_entry_level`` is
    the deepest ask level scanned for the first FAK hint, not a mandatory
    minimum level.
    """
    ask_age = ws.get_latest_best_ask_age(token_id, level=1)
    try:
        raw_levels = ws.get_latest_ask_levels_with_size(token_id, max_age_sec=max_age_sec)
    except AttributeError:
        raw_levels = None
    if not isinstance(raw_levels, list):
        fallback_ask = ws.get_latest_best_ask(token_id, max_age_sec=max_age_sec, level=1)
        fallback_size = (amount / fallback_ask * 1.01) if fallback_ask and fallback_ask > 0 else amount
        raw_levels = (
            [(fallback_ask, fallback_size), (fallback_ask, fallback_size)]
            if fallback_ask is not None
            else []
        )

    levels = [(float(price), float(size)) for price, size in raw_levels if float(size) > 0]
    best_ask_level_1 = levels[0][0] if levels else _best_ask_level_1(ws, token_id)
    preview = levels[:DEPTH_PREVIEW_LEVELS]
    max_entry_level = dynamic_entry_level(
        best_ask_level_1,
        min_entry_level,
        dynamic_entry_levels,
    )
    if (
        not dynamic_entry_levels
        and best_ask_level_1 is not None
        and low_price_threshold is not None
        and low_price_entry_level is not None
        and best_ask_level_1 < low_price_threshold
    ):
        max_entry_level = max(max_entry_level, int(low_price_entry_level))
    max_entry_index = max(max_entry_level - 1, int(skip_levels))
    if not levels or max_entry_price is None:
        return CapDepthQuote(
            price=None,
            price_hint=None,
            cap_notional=0.0,
            levels_used=0,
            total_levels=len(levels),
            skipped_levels=min(skip_levels, len(levels)),
            entry_ask_level=max_entry_level,
            best_ask_level_1=best_ask_level_1,
            ask_age_sec=ask_age,
            preview=preview,
            enough=False,
        )

    effective_max_entry_price = max_entry_price
    if (
        best_ask_level_1 is not None
        and max_slippage_from_best_ask is not None
        and max_slippage_from_best_ask >= 0
    ):
        slippage_cap = best_ask_level_1 + max_slippage_from_best_ask
        effective_max_entry_price = min(max_entry_price, slippage_cap)

    cap_notional = 0.0
    levels_used = 0
    selected_price = None
    for index, (ask_price, ask_size) in enumerate(levels):
        if index > max_entry_index:
            break
        if ask_price > effective_max_entry_price:
            break
        if index < skip_levels:
            continue
        levels_used += 1
        cap_notional += ask_price * ask_size
        if cap_notional >= amount:
            selected_price = ask_price
            break

    price_hint = (
        buffer_buy_price_hint(
            token_id,
            selected_price,
            buffer_ticks=buffer_ticks,
            max_price=effective_max_entry_price,
        )
        if selected_price is not None
        else None
    )
    return CapDepthQuote(
        price=selected_price,
        price_hint=price_hint,
        cap_notional=cap_notional,
        levels_used=levels_used,
        total_levels=len(levels),
        skipped_levels=min(skip_levels, len(levels)),
        entry_ask_level=max_entry_level,
        best_ask_level_1=best_ask_level_1,
        ask_age_sec=ask_age,
        preview=preview,
        enough=selected_price is not None and price_hint is not None,
    )


def stop_loss_bid_quote(
    ws: PriceStream,
    token_id: str,
    shares: float,
    *,
    max_age_sec: Optional[float],
    skip_levels: int = DEPTH_ENTRY_SKIP_LEVELS,
    min_sell_level: int = 9,
    min_sell_price: float = 0.20,
    buffer_ticks: Optional[float] = None,
) -> BidDepthQuote:
    """Return the bid level where enough stop-loss sell depth exists."""
    bid_age = None
    if hasattr(ws, "get_latest_best_bid_age"):
        bid_age = ws.get_latest_best_bid_age(token_id, level=1)
    try:
        raw_levels = ws.get_latest_bid_levels_with_size(token_id, max_age_sec=max_age_sec)
    except AttributeError:
        raw_levels = None
    if not isinstance(raw_levels, list):
        fallback_bid = (
            ws.get_latest_best_bid(token_id, max_age_sec=max_age_sec, level=1)
            if hasattr(ws, "get_latest_best_bid")
            else None
        )
        raw_levels = (
            [(fallback_bid, shares), (fallback_bid, shares)]
            if fallback_bid is not None
            else []
        )

    levels = [(float(price), float(size)) for price, size in raw_levels if float(size) > 0]
    best_bid_level_1 = levels[0][0] if levels else (
        ws.get_latest_best_bid(token_id, max_age_sec=max_age_sec, level=1)
        if hasattr(ws, "get_latest_best_bid")
        else None
    )
    preview = levels[:DEPTH_PREVIEW_LEVELS]
    max_sell_level = max(1, int(min_sell_level))
    max_sell_index = max(max_sell_level - 1, int(skip_levels))
    if not levels or shares <= 0:
        return BidDepthQuote(
            price=None,
            price_hint=None,
            shares_available=0.0,
            levels_used=0,
            total_levels=len(levels),
            skipped_levels=min(skip_levels, len(levels)),
            sell_bid_level=max_sell_level,
            best_bid_level_1=best_bid_level_1,
            bid_age_sec=bid_age,
            preview=preview,
            enough=False,
        )

    shares_available = 0.0
    levels_used = 0
    selected_price = None
    enough = False
    for index, (bid_price, bid_size) in enumerate(levels):
        if index > max_sell_index:
            break
        if bid_price < min_sell_price:
            break
        if index < skip_levels:
            continue
        levels_used += 1
        shares_available += bid_size
        selected_price = bid_price
        if shares_available >= shares:
            enough = True

    price_hint = (
        buffer_sell_price_hint(
            token_id,
            selected_price,
            buffer_ticks=buffer_ticks,
            min_price=min_sell_price,
        )
        if enough and selected_price is not None
        else None
    )
    return BidDepthQuote(
        price=selected_price,
        price_hint=price_hint,
        shares_available=shares_available,
        levels_used=levels_used,
        total_levels=len(levels),
        skipped_levels=min(skip_levels, len(levels)),
        sell_bid_level=max_sell_level,
        best_bid_level_1=best_bid_level_1,
        bid_age_sec=bid_age,
        preview=preview,
        enough=enough and selected_price is not None and price_hint is not None,
    )
