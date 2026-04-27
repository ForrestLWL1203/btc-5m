"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
import datetime
import functools
import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

from polybot.core import config
from polybot.core.client import get_midpoint_async, get_tick_size
from polybot.core.log_formatter import (
    MARKET,
    SIGNAL,
    TRADE,
    WINDOW,
    log_event,
)
from polybot.market.market import (
    MarketWindow,
    find_next_window,
    find_window_after,
)
from polybot.market.series import MarketSeries
from polybot.core.state import MonitorState
from polybot.market.stream import PriceStream, PriceUpdate
from polybot.strategies.base import Strategy
from polybot.trade_config import TradeConfig
from .trading import buy_token, sell_token

log = logging.getLogger(__name__)

_PREOPEN_BUFFER = 10  # seconds before window start to wake up
_STARTED_SKIP_THRESHOLD = 60  # allow attaching to a window within its first minute
_SIGNAL_EVAL_LOG_INTERVAL_SEC = 5.0
_DEPTH_ENTRY_SKIP_LEVELS = 1
_DEPTH_PREVIEW_LEVELS = 6


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


async def _noop_price_callback(update: PriceUpdate) -> None:
    """Placeholder callback used before a PriceStream is fully wired."""
    return None


def _entry_price_cap(
    strategy: Optional[Strategy],
    state: Optional[MonitorState] = None,
) -> Optional[float]:
    """Return the active max entry cap when exposed by the strategy."""
    if strategy is None:
        return None
    max_price = None
    if state is not None:
        max_price = state.target_max_entry_price
    if max_price is None:
        max_price = getattr(strategy, "max_entry_price", getattr(strategy, "_max_entry_price", None))
    return max_price


def _buffer_price_hint(
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


def _buffer_sell_price_hint(
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


def _initial_price_hint(
    token_id: str,
    best_ask: Optional[float],
    strategy: Optional[Strategy],
    state: Optional[MonitorState],
) -> Optional[float]:
    """Return first-attempt BUY hint using the dynamic strength cap directly."""
    if best_ask is None:
        return None
    max_entry_price = _entry_price_cap(strategy, state)
    if max_entry_price is not None and best_ask <= max_entry_price:
        return max(0.0, min(1.0, max_entry_price))
    return _buffer_price_hint(
        token_id,
        best_ask,
        max_price=max_entry_price,
    )


def _cap_limited_depth_quote(
    ws: PriceStream,
    token_id: str,
    amount: float,
    max_entry_price: Optional[float],
    *,
    max_age_sec: Optional[float] = None,
    skip_levels: int = _DEPTH_ENTRY_SKIP_LEVELS,
    min_entry_level: int = 1,
    low_price_threshold: Optional[float] = None,
    low_price_entry_level: Optional[int] = None,
    buffer_ticks: Optional[float] = None,
) -> CapDepthQuote:
    """Return the first ask level where cap-limited depth can cover amount.

    Level 1 is deliberately excluded from fillability calculations because it
    often disappears before the FAK reaches Polymarket.
    """
    ask_age = ws.get_latest_best_ask_age(token_id, level=1)
    try:
        raw_levels = ws.get_latest_ask_levels_with_size(token_id, max_age_sec=max_age_sec)
    except AttributeError:
        raw_levels = None
    if not isinstance(raw_levels, list):
        fallback_ask = ws.get_latest_best_ask(token_id, max_age_sec=max_age_sec, level=1)
        # Test doubles and legacy stream shims may not expose L2 sizes. Real
        # PriceStream returns [] when the book is unavailable, which still
        # blocks live entry.
        fallback_size = (amount / fallback_ask * 1.01) if fallback_ask and fallback_ask > 0 else amount
        raw_levels = (
            [(fallback_ask, fallback_size), (fallback_ask, fallback_size)]
            if fallback_ask is not None
            else []
        )

    levels = [(float(price), float(size)) for price, size in raw_levels if float(size) > 0]
    best_ask_level_1 = levels[0][0] if levels else _best_ask_level_1(ws, token_id)
    preview = levels[:_DEPTH_PREVIEW_LEVELS]
    min_entry_level = max(1, int(min_entry_level))
    if (
        best_ask_level_1 is not None
        and low_price_threshold is not None
        and low_price_entry_level is not None
        and best_ask_level_1 < low_price_threshold
    ):
        min_entry_level = max(min_entry_level, int(low_price_entry_level))
    min_entry_index = min_entry_level - 1
    if not levels or max_entry_price is None:
        return CapDepthQuote(
            price=None,
            price_hint=None,
            cap_notional=0.0,
            levels_used=0,
            total_levels=len(levels),
            skipped_levels=min(skip_levels, len(levels)),
            entry_ask_level=min_entry_level,
            best_ask_level_1=best_ask_level_1,
            ask_age_sec=ask_age,
            preview=preview,
            enough=False,
        )

    cap_notional = 0.0
    levels_used = 0
    selected_price = None
    for index, (ask_price, ask_size) in enumerate(levels):
        if ask_price > max_entry_price:
            break
        if index < skip_levels:
            continue
        levels_used += 1
        cap_notional += ask_price * ask_size
        if cap_notional >= amount and index >= min_entry_index:
            selected_price = ask_price
            break

    price_hint = _buffer_price_hint(
        token_id,
        selected_price,
        buffer_ticks=buffer_ticks,
        max_price=max_entry_price,
    ) if selected_price is not None else None
    return CapDepthQuote(
        price=selected_price,
        price_hint=price_hint,
        cap_notional=cap_notional,
        levels_used=levels_used,
        total_levels=len(levels),
        skipped_levels=min(skip_levels, len(levels)),
        entry_ask_level=min_entry_level,
        best_ask_level_1=best_ask_level_1,
        ask_age_sec=ask_age,
        preview=preview,
        enough=selected_price is not None and price_hint is not None,
    )


def _stop_loss_bid_quote(
    ws: PriceStream,
    token_id: str,
    shares: float,
    *,
    max_age_sec: Optional[float],
    skip_levels: int = _DEPTH_ENTRY_SKIP_LEVELS,
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
    preview = levels[:_DEPTH_PREVIEW_LEVELS]
    min_sell_level = max(1, int(min_sell_level))
    min_sell_index = min_sell_level - 1
    if not levels or shares <= 0:
        return BidDepthQuote(
            price=None,
            price_hint=None,
            shares_available=0.0,
            levels_used=0,
            total_levels=len(levels),
            skipped_levels=min(skip_levels, len(levels)),
            sell_bid_level=min_sell_level,
            best_bid_level_1=best_bid_level_1,
            bid_age_sec=bid_age,
            preview=preview,
            enough=False,
        )

    shares_available = 0.0
    levels_used = 0
    selected_price = None
    for index, (bid_price, bid_size) in enumerate(levels):
        if bid_price < min_sell_price:
            break
        if index < skip_levels:
            continue
        levels_used += 1
        shares_available += bid_size
        if shares_available >= shares and index >= min_sell_index:
            selected_price = bid_price
            break

    price_hint = _buffer_sell_price_hint(
        token_id,
        selected_price,
        buffer_ticks=buffer_ticks,
        min_price=min_sell_price,
    ) if selected_price is not None else None
    return BidDepthQuote(
        price=selected_price,
        price_hint=price_hint,
        shares_available=shares_available,
        levels_used=levels_used,
        total_levels=len(levels),
        skipped_levels=min(skip_levels, len(levels)),
        sell_bid_level=min_sell_level,
        best_bid_level_1=best_bid_level_1,
        bid_age_sec=bid_age,
        preview=preview,
        enough=selected_price is not None and price_hint is not None,
    )


def _log_depth_skip(
    state: MonitorState,
    side: str,
    signal_price: float,
    quote: CapDepthQuote,
    max_entry_price: Optional[float],
    amount: float,
    reason: str,
) -> None:
    state.depth_skip_count += 1
    state.depth_skip_last_reason = reason
    state.depth_skip_max_notional = max(state.depth_skip_max_notional, quote.cap_notional)
    if quote.best_ask_level_1 is not None:
        state.depth_skip_min_best_ask = (
            quote.best_ask_level_1
            if state.depth_skip_min_best_ask is None
            else min(state.depth_skip_min_best_ask, quote.best_ask_level_1)
        )
        state.depth_skip_max_best_ask = (
            quote.best_ask_level_1
            if state.depth_skip_max_best_ask is None
            else max(state.depth_skip_max_best_ask, quote.best_ask_level_1)
        )
    if quote.price is not None:
        state.depth_skip_min_entry_ask = (
            quote.price
            if state.depth_skip_min_entry_ask is None
            else min(state.depth_skip_min_entry_ask, quote.price)
        )
        state.depth_skip_max_entry_ask = (
            quote.price
            if state.depth_skip_max_entry_ask is None
            else max(state.depth_skip_max_entry_ask, quote.price)
        )
    if state.depth_skip_first_logged:
        return
    state.depth_skip_first_logged = True
    _log_signal_eval(
        state,
        side,
        signal_price,
        quote.best_ask_level_1,
        quote.price,
        max_entry_price,
        depth_notional=quote.cap_notional,
        depth_levels_used=quote.levels_used,
    )
    log_event(log, logging.INFO, SIGNAL, {
        "action": "ENTRY_DEPTH_SKIP",
        "side": side.upper(),
        "price": quote.price,
        "price_hint": quote.price_hint,
        "best_ask_level_1": quote.best_ask_level_1,
        "depth_levels_used": quote.levels_used,
        "depth_notional": round(quote.cap_notional, 4),
        "depth_total_levels": quote.total_levels,
        "depth_skipped_levels": quote.skipped_levels,
        "entry_ask_level": quote.entry_ask_level,
        "book_ask_preview": quote.preview,
        "amount": amount,
        "max_entry_price": max_entry_price,
        "best_ask_age_ms": round(quote.ask_age_sec * 1000) if quote.ask_age_sec is not None else None,
        "reason": reason,
    })


def _price_hint_refresher(
    ws: PriceStream,
    token_id: str,
    strategy: Optional[Strategy],
    trade_config: TradeConfig,
    state: Optional[MonitorState] = None,
):
    """Return a callback that refreshes retry BUY hints from the latest WS ask."""

    def refresh() -> Optional[float]:
        max_entry_price = _entry_price_cap(strategy, state)
        trade_amount = trade_config.amount_for_signal_strength(
            state.target_signal_strength if state is not None else None
        )
        entry_ask_level = trade_config.base_entry_ask_level()
        quote = _cap_limited_depth_quote(
            ws,
            token_id,
            trade_amount,
            max_entry_price,
            max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
            min_entry_level=entry_ask_level,
            low_price_threshold=trade_config.low_price_threshold,
            low_price_entry_level=trade_config.low_price_entry_ask_level,
            buffer_ticks=config.FAK_RETRY_PRICE_HINT_BUFFER_TICKS,
        )
        if not quote.enough:
            log_event(log, logging.INFO, SIGNAL, {
                "action": "BUY_RETRY_ABORT",
                "price": quote.price,
                "price_hint": quote.price_hint,
                "best_ask_level_1": quote.best_ask_level_1,
                "depth_levels_used": quote.levels_used,
                "depth_notional": round(quote.cap_notional, 4),
                "depth_total_levels": quote.total_levels,
                "depth_skipped_levels": quote.skipped_levels,
                "entry_ask_level": quote.entry_ask_level,
                "book_ask_preview": quote.preview,
                "amount": trade_amount,
                "max_entry_price": max_entry_price,
                "best_ask_age_ms": round(quote.ask_age_sec * 1000) if quote.ask_age_sec is not None else None,
                "reason": "cap-limited book depth insufficient or stale",
            })
            return None
        return quote.price_hint

    return refresh


def _stop_loss_price_hint_refresher(
    ws: PriceStream,
    token_id: str,
    trade_config: TradeConfig,
    state: MonitorState,
):
    """Return a callback that refreshes stop-loss SELL hints from fresh WS bids."""

    def refresh() -> Optional[float]:
        quote = _stop_loss_bid_quote(
            ws,
            token_id,
            state.holding_size,
            max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
            min_sell_level=trade_config.stop_loss_bid_level(),
            min_sell_price=trade_config.stop_loss_min_sell_price,
            buffer_ticks=config.FAK_RETRY_PRICE_HINT_BUFFER_TICKS,
        )
        if not quote.enough:
            log_event(log, logging.INFO, SIGNAL, {
                "action": "STOP_LOSS_RETRY_ABORT",
                "price": quote.price,
                "price_hint": quote.price_hint,
                "best_bid_level_1": quote.best_bid_level_1,
                "bid_levels_used": quote.levels_used,
                "bid_shares_available": round(quote.shares_available, 4),
                "bid_total_levels": quote.total_levels,
                "bid_skipped_levels": quote.skipped_levels,
                "sell_bid_level": quote.sell_bid_level,
                "book_bid_preview": quote.preview,
                "shares": state.holding_size,
                "best_bid_age_ms": round(quote.bid_age_sec * 1000) if quote.bid_age_sec is not None else None,
                "reason": "stop-loss bid depth insufficient or stale",
            })
            return None
        return quote.price_hint

    return refresh


def _log_signal_eval(
    state: MonitorState,
    side: str,
    signal_price: float,
    best_ask_level_1: Optional[float],
    target_entry_ask: Optional[float],
    max_entry_price: Optional[float],
    depth_notional: Optional[float] = None,
    depth_levels_used: Optional[int] = None,
) -> None:
    now = time.monotonic()
    key = (
        side,
        round(best_ask_level_1, 3) if best_ask_level_1 is not None else None,
        round(target_entry_ask, 3) if target_entry_ask is not None else None,
        round(depth_notional, 4) if depth_notional is not None else None,
        depth_levels_used,
        round(max_entry_price, 6) if max_entry_price is not None else None,
    )
    if (
        state.last_signal_eval_key == key
        and now - state.last_signal_eval_logged_at < _SIGNAL_EVAL_LOG_INTERVAL_SEC
    ):
        return
    state.last_signal_eval_key = key
    state.last_signal_eval_logged_at = now
    log_event(log, logging.INFO, SIGNAL, {
        "action": "SIGNAL_EVAL",
        "side": side.upper(),
        "signal_price": signal_price,
        "signal_ref_price": state.signal_reference_price,
        "best_ask_level_1": best_ask_level_1,
        "target_entry_ask": target_entry_ask,
        "depth_notional": round(depth_notional, 4) if depth_notional is not None else None,
        "depth_levels_used": depth_levels_used,
        "max_entry_price": max_entry_price,
        "confidence": state.target_signal_confidence,
        "signal_strength": (
            round(state.target_signal_strength, 3)
            if state.target_signal_strength is not None
            else None
        ),
        "past_signal_strength": (
            round(state.target_past_signal_strength, 3)
            if state.target_past_signal_strength is not None
            else None
        ),
        "remaining_sec": (
            round(state.target_remaining_sec)
            if state.target_remaining_sec is not None
            else None
        ),
    })


def _best_ask_level_1(ws: PriceStream, token_id: str) -> Optional[float]:
    return ws.get_latest_best_ask(
        token_id,
        max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
        level=1,
    )


def _entry_ask_changed(state: MonitorState, side: str, best_ask: Optional[float]) -> bool:
    """Return True once for each changed target ask checked for entry."""
    if best_ask is None:
        return True
    if state.last_entry_check_side == side and state.last_entry_check_best_ask == best_ask:
        return False
    state.last_entry_check_side = side
    state.last_entry_check_best_ask = best_ask
    return True


def _get_utc8_date() -> str:
    """Get current date in UTC+8 as YYYY-MM-DD."""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _check_and_reset_daily_state(state: MonitorState) -> None:
    """Reset daily risk management stats at UTC+8 midnight."""
    current_date = _get_utc8_date()
    if state.last_reset_date != current_date:
        log.debug("DAILY_RESET: %s -> %s", state.last_reset_date, current_date)
        state.last_reset_date = current_date
        state.daily_wins = 0
        state.daily_losses = 0
        state.daily_realized_pnl = 0.0
        state.consecutive_losses = 0
        state.consecutive_loss_amount = 0.0
        state.windows_to_skip = 0


def _should_skip_window(state: MonitorState) -> bool:
    """Check if this window should be skipped due to risk management."""
    if state.windows_to_skip > 0:
        log_event(log, logging.WARNING, WINDOW, {
            "action": "SKIP_WINDOW",
            "reason": "risk_management_pause",
            "windows_remaining": state.windows_to_skip,
        })
        state.windows_to_skip -= 1
        return True
    return False


def _process_trade_result(
    state: MonitorState,
    direction_correct: bool,
    realized_pnl: float,
    trade_config: Optional[TradeConfig] = None,
) -> None:
    """Update daily statistics and check risk management triggers."""
    state.daily_realized_pnl += realized_pnl
    if direction_correct:
        state.daily_wins += 1
        state.consecutive_losses = 0
        state.consecutive_loss_amount = 0.0
    else:
        state.daily_losses += 1
        state.consecutive_losses += 1
        state.consecutive_loss_amount += abs(realized_pnl)

        # Trigger 1: 5 consecutive losses
        if state.consecutive_losses >= 5:
            state.windows_to_skip = 2
            log_event(log, logging.WARNING, TRADE, {
                "action": "RISK_ALERT_5_LOSSES",
                "consecutive_losses": state.consecutive_losses,
                "window_pause": 2,
                "reason": "System anomaly detected",
            })
            state.consecutive_losses = 0  # Reset after triggering pause

        if (
            trade_config is not None
            and trade_config.consecutive_loss_amount_limit is not None
            and state.consecutive_loss_amount >= trade_config.consecutive_loss_amount_limit
        ):
            state.windows_to_skip = max(
                state.windows_to_skip,
                trade_config.consecutive_loss_pause_windows,
            )
            log_event(log, logging.WARNING, TRADE, {
                "action": "RISK_ALERT_CONSECUTIVE_LOSS_AMOUNT",
                "consecutive_loss_amount": round(state.consecutive_loss_amount, 4),
                "limit": trade_config.consecutive_loss_amount_limit,
                "window_pause": trade_config.consecutive_loss_pause_windows,
            })
            state.consecutive_loss_amount = 0.0

    # Trigger 2: Check win rate after minimum trades
    total_trades = state.daily_wins + state.daily_losses
    if total_trades >= state.min_trades_for_eval:
        current_wr = state.daily_wins / total_trades
        if current_wr < 0.50:
            # Only trigger once to avoid repeated alerts
            if state.windows_to_skip == 0:  # Not already paused
                state.windows_to_skip = 5
                log_event(log, logging.CRITICAL, TRADE, {
                    "action": "RISK_ALERT_WIN_RATE",
                    "win_rate": round(current_wr, 3),
                    "trades": total_trades,
                    "wins": state.daily_wins,
                    "losses": state.daily_losses,
                    "window_pause": 5,
                    "reason": "Strategy failure - win rate < 50%",
                })

    if (
        trade_config is not None
        and trade_config.daily_loss_amount_limit is not None
        and state.daily_realized_pnl <= -trade_config.daily_loss_amount_limit
    ):
        state.windows_to_skip = max(
            state.windows_to_skip,
            trade_config.daily_loss_pause_windows,
        )
        log_event(log, logging.CRITICAL, TRADE, {
            "action": "RISK_ALERT_DAILY_LOSS_AMOUNT",
            "daily_realized_pnl": round(state.daily_realized_pnl, 4),
            "limit": trade_config.daily_loss_amount_limit,
            "window_pause": trade_config.daily_loss_pause_windows,
        })


async def _maybe_handle_stop_loss(
    window: MarketWindow,
    state: MonitorState,
    ws: PriceStream,
    token_id: str,
    dry_run: bool,
    trade_config: TradeConfig,
    side: str,
) -> None:
    """Evaluate and execute optional stop-loss while holding a position."""
    if not trade_config.stop_loss_enabled:
        return
    if not state.bought or state.exit_triggered or state.stop_loss_attempted:
        return
    if state.holding_size <= 0:
        return

    remaining = window.end_epoch - time.time()
    if remaining > trade_config.stop_loss_start_remaining_sec:
        return
    if remaining < trade_config.stop_loss_end_remaining_sec:
        return

    entry_price = state.entry_avg_price or state.entry_price
    if entry_price <= 0:
        return
    stop_price = max(
        trade_config.stop_loss_min_sell_price,
        (1.0 - entry_price) * trade_config.stop_loss_multiplier,
    )
    state.stop_loss_price = stop_price

    quote = _stop_loss_bid_quote(
        ws,
        token_id,
        state.holding_size,
        max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
        min_sell_level=trade_config.stop_loss_bid_level(),
        min_sell_price=trade_config.stop_loss_min_sell_price,
    )
    if not quote.enough or quote.price is None or quote.price > stop_price:
        return

    state.stop_loss_attempted = True
    log_event(log, logging.WARNING, TRADE, {
        "action": "STOP_LOSS_TRIGGERED",
        "side": side.upper(),
        "window": window.short_label,
        "entry_price": entry_price,
        "stop_price": round(stop_price, 4),
        "sell_price": quote.price,
        "price_hint": quote.price_hint,
        "shares": state.holding_size,
        "remaining_sec": round(remaining),
        "best_bid_level_1": quote.best_bid_level_1,
        "bid_levels_used": quote.levels_used,
        "bid_shares_available": round(quote.shares_available, 4),
        "bid_total_levels": quote.total_levels,
        "bid_skipped_levels": quote.skipped_levels,
        "sell_bid_level": quote.sell_bid_level,
        "book_bid_preview": quote.preview,
        "best_bid_age_ms": round(quote.bid_age_sec * 1000) if quote.bid_age_sec is not None else None,
        "dry_run": dry_run,
    })

    if dry_run:
        sell_price = quote.price_hint or quote.price
        realized_pnl = state.holding_size * sell_price - state.entry_amount
        _process_trade_result(state, realized_pnl >= 0, realized_pnl, trade_config)
        log_event(log, logging.WARNING, TRADE, {
            "action": "STOP_LOSS_FILLED",
            "side": side.upper(),
            "window": window.short_label,
            "avg_price": sell_price,
            "shares": state.holding_size,
            "realized_pnl": round(realized_pnl, 4),
            "daily_realized_pnl": round(state.daily_realized_pnl, 4),
            "dry_run": True,
        })
        state.holding_size = 0.0
        state.bought = False
        state.exit_triggered = True
        state.stop_loss_triggered = True
        return

    result = await sell_token(
        token_id,
        state.holding_size,
        price_hint=quote.price_hint,
        price_hint_refresher=_stop_loss_price_hint_refresher(ws, token_id, trade_config, state),
        retry_count=trade_config.stop_loss_retry_count,
    )
    if not result.success:
        log_event(log, logging.WARNING, TRADE, {
            "action": "STOP_LOSS_FAILED",
            "side": side.upper(),
            "window": window.short_label,
            "shares": state.holding_size,
            "price_hint": quote.price_hint,
            "message": result.message,
        })
        return

    sold_size = min(result.filled_size or state.holding_size, state.holding_size)
    sell_price = result.avg_price or quote.price_hint or quote.price or 0.0
    cost_basis = state.entry_amount * (sold_size / state.holding_size) if state.holding_size > 0 else 0.0
    realized_pnl = sold_size * sell_price - cost_basis
    _process_trade_result(state, realized_pnl >= 0, realized_pnl, trade_config)
    log_event(log, logging.WARNING, TRADE, {
        "action": "STOP_LOSS_FILLED",
        "side": side.upper(),
        "window": window.short_label,
        "avg_price": sell_price,
        "shares": sold_size,
        "requested_shares": state.holding_size,
        "realized_pnl": round(realized_pnl, 4),
        "daily_realized_pnl": round(state.daily_realized_pnl, 4),
        "order_id": result.order_id,
    })
    state.holding_size = max(0.0, state.holding_size - sold_size)
    state.entry_amount = max(0.0, state.entry_amount - cost_basis)
    state.stop_loss_triggered = True
    if state.holding_size <= 1e-9:
        state.holding_size = 0.0
        state.bought = False
        state.exit_triggered = True


def _strategy_attach_skip_threshold(
    strategy: Optional[Strategy],
    window: MarketWindow,
) -> tuple[float, str]:
    """Return the latest elapsed-start threshold for a fresh attach.

    Strategies with an explicit entry window should be attachable until the
    end of that entry band. Generic strategies keep the legacy 60s fallback.
    """
    if strategy is None:
        return float(_STARTED_SKIP_THRESHOLD), f"started >{_STARTED_SKIP_THRESHOLD}s ago"

    entry_end_remaining = getattr(strategy, "entry_end_remaining_sec", None)
    if isinstance(entry_end_remaining, (int, float)):
        window_seconds = window.end_epoch - window.start_epoch
        threshold = max(0.0, float(window_seconds) - float(entry_end_remaining))
        return threshold, (
            "outside strategy entry window "
            f"(started >{threshold:.0f}s ago, remaining <{float(entry_end_remaining):.0f}s)"
        )

    return float(_STARTED_SKIP_THRESHOLD), f"started >{_STARTED_SKIP_THRESHOLD}s ago"


def _sanitize_next_window(current_window: MarketWindow, next_window: Optional[MarketWindow]) -> Optional[MarketWindow]:
    """Reject stale or repeated windows when chaining to the next round."""
    if next_window is None:
        return None
    if next_window.start_epoch <= current_window.start_epoch:
        log_event(log, logging.WARNING, WINDOW, {
            "action": "INVALID_NEXT_WINDOW",
            "current": current_window.short_label,
            "candidate": next_window.short_label,
            "reason": "candidate did not advance beyond current window",
        })
        return None
    return next_window


def _log_window_summary(state: MonitorState, window: MarketWindow, dry_run: bool) -> None:
    """Emit a compact end-of-window summary."""
    data = {
        "action": "SUMMARY",
        "window": window.short_label,
        "entries": state.entry_count,
        "blocked_window_cap": state.buy_blocked_window_cap,
    }
    if state.depth_skip_count > 0:
        data.update({
            "depth_skip_count": state.depth_skip_count,
            "depth_skip_last_reason": state.depth_skip_last_reason,
            "depth_skip_min_best_ask": state.depth_skip_min_best_ask,
            "depth_skip_max_best_ask": state.depth_skip_max_best_ask,
            "depth_skip_min_entry_ask": state.depth_skip_min_entry_ask,
            "depth_skip_max_entry_ask": state.depth_skip_max_entry_ask,
            "depth_skip_max_notional": round(state.depth_skip_max_notional, 4),
        })
    log_event(log, logging.INFO, WINDOW, data)


def _side_token(window: MarketWindow, side: str) -> tuple[str, str]:
    """Return (buy_token, price_token) based on trade side."""
    if side == "down":
        return window.down_token, window.down_token
    return window.up_token, window.up_token


async def _monitor_single_window(
    window: MarketWindow,
    state: MonitorState,
    ws: Optional[PriceStream],
    dry_run: bool,
    trade_config: TradeConfig,
    strategy: Optional[Strategy] = None,
    series: Optional[MarketSeries] = None,
    side: str = "up",
    prefetch_next_window: bool = True,
) -> Optional[MarketWindow]:
    """
    Monitor a single window until expiry or exit_triggered, then clean up.
    """
    # Check daily reset at start of each window
    _check_and_reset_daily_state(state)

    fetch_task = None

    while True:
        now = int(time.time())
        if now >= window.end_epoch:
            if state.bought and not state.exit_triggered:
                # Post-window-end phase: record trade result and await auto-redeem
                # Polymarket has auto-redeem enabled, so position will be automatically
                # redeemed and funds returned to account. No manual sell needed.
                token_price = state.latest_midpoint
                direction_correct = token_price is not None and token_price > 0.5

                entry_amount = state.entry_amount or state.entry_price * state.holding_size
                realized_pnl = (
                    state.holding_size * token_price - entry_amount
                    if token_price is not None
                    else 0.0
                )

                # Process trade result for risk management
                _process_trade_result(
                    state,
                    direction_correct,
                    realized_pnl,
                    trade_config,
                )

                # Record trade resolution
                log_event(log, logging.INFO, TRADE, {
                    "action": "TRADE_RESOLVED",
                    "window": window.short_label,
                    "result": "WIN" if direction_correct else "LOSS",
                    "shares": state.holding_size,
                    "price": token_price,
                    "amount": entry_amount,
                    "realized_pnl": round(realized_pnl, 4),
                    "daily_realized_pnl": round(state.daily_realized_pnl, 4),
                    "note": "Position held to window end, auto-redeem in progress",
                })

                state.exit_triggered = True

            # All positions resolved, pre-fetch next window unless this is the
            # caller's final planned round.
            if prefetch_next_window and fetch_task is None:
                fetch_task = asyncio.create_task(
                    asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
                )
            break

        if state.exit_triggered:
            remaining = window.end_epoch - now
            # Pre-fetch next window while we sleep unless this is the caller's
            # final planned round.
            if prefetch_next_window and fetch_task is None:
                fetch_task = asyncio.create_task(
                    asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
                )
            await asyncio.sleep(remaining)
            if fetch_task is None:
                _log_window_summary(state, window, dry_run)
                return None
            try:
                next_win = _sanitize_next_window(window, await fetch_task)
            except Exception as e:
                log.debug("Pre-fetch next window failed: %s", e)
                next_win = _sanitize_next_window(window, find_next_window())
            # Do NOT close ws — reuse across windows
            _log_window_summary(state, window, dry_run)
            return next_win

        await asyncio.sleep(1)

    if fetch_task is not None:
        try:
            next_win = _sanitize_next_window(window, await fetch_task)
        except Exception as e:
            log.debug("Pre-fetch next window after expiry failed: %s", e)
            next_win = None
        _log_window_summary(state, window, dry_run)
        return next_win
    _log_window_summary(state, window, dry_run)
    return None


def _find_next_window_after(after_epoch: int, series: Optional[MarketSeries] = None) -> Optional[MarketWindow]:
    """Find the next window after the given epoch (delegates to market.find_window_after)."""
    return find_window_after(after_epoch, series)


def _find_and_preopen_next_window(
    current_window: MarketWindow,
    series: Optional[MarketSeries] = None,
) -> Optional[MarketWindow]:
    """
    Find the window that starts after current_window.end_epoch and return it.
    """
    next_win = _find_next_window_after(current_window.end_epoch, series)
    if next_win is None:
        log_event(log, logging.WARNING, WINDOW, {
            "action": "NOT_FOUND",
            "message": f"No next window after {current_window.short_label}",
        })
        return None

    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - _PREOPEN_BUFFER

    if now_epoch < wake_epoch:
        remaining = wake_epoch - now_epoch
        log.debug(
            "Pre-open: sleeping %ds until %s starts at %s",
            remaining, next_win.short_label, next_win.start_time,
        )
        return next_win

    return _sanitize_next_window(current_window, next_win)


async def monitor_window(
    window: MarketWindow,
    dry_run: bool = False,
    preopened: bool = False,
    existing_ws: Optional[PriceStream] = None,
    trade_config: Optional[TradeConfig] = None,
    strategy: Optional[Strategy] = None,
    series: Optional[MarketSeries] = None,
    state: Optional[MonitorState] = None,
    prefetch_next_window: bool = True,
) -> tuple[Optional[MarketWindow], Optional[PriceStream], bool]:
    """
    Monitor a trading window using WebSocket real-time price updates.

    Args:
        window: The window to monitor.
        dry_run: If True, log actions but don't place orders.
        preopened: If True, skip the stale check.
        existing_ws: Reuse this WS connection instead of creating a new one.
        trade_config: Common execution parameters (amount, per-window cap, rounds).
        strategy: Strategy handling direction + buy decision.
        series: Market series definition (uses config defaults if None).
        state: Shared MonitorState for risk management tracking across windows.
               If None, creates a new one (risk management won't persist).

    Returns (next_window, ws, monitored) — monitored is False if window was skipped.
    Pass ws to the next call's existing_ws param and state for next window.
    """
    if trade_config is None:
        trade_config = TradeConfig()
    assert strategy is not None, "strategy is required"

    # Resolve direction — strategy.get_side() runs once per window
    side = strategy.get_side()
    if side is None:
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "DIRECTION_SKIP",
            "window": window.short_label,
            "reason": "strategy returned no side",
        })
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, existing_ws, False
    # Use shared state if provided, otherwise create new (which won't persist)
    if state is None:
        state = MonitorState()
    ws: Optional[PriceStream] = existing_ws

    # Reset per-window state for new window
    # (risk management state like daily_wins persists across windows)
    state.bought = False
    state.holding_size = 0.0
    state.entry_price = 0.0
    state.exit_triggered = False
    state.buy_blocked_window_cap = False
    state.entry_count = 0
    state.entry_timestamps = []
    state.latest_midpoint = None
    state.target_side = None
    state.target_entry_price = None
    state.target_max_entry_price = None
    state.target_signal_confidence = None
    state.target_signal_strength = None
    state.target_past_signal_strength = None
    state.target_remaining_sec = None
    state.signal_reference_price = None
    state.entry_avg_price = 0.0
    state.stop_loss_triggered = False
    state.stop_loss_attempted = False
    state.stop_loss_price = None
    state.last_signal_eval_key = None
    state.last_signal_eval_logged_at = 0.0
    state.last_depth_skip_key = None
    state.last_depth_skip_logged_at = 0.0
    state.depth_skip_count = 0
    state.depth_skip_first_logged = False
    state.depth_skip_last_reason = None
    state.depth_skip_min_best_ask = None
    state.depth_skip_max_best_ask = None
    state.depth_skip_min_entry_ask = None
    state.depth_skip_max_entry_ask = None
    state.depth_skip_max_notional = 0.0
    state.entry_amount = 0.0
    state.last_entry_check_side = None
    state.last_entry_check_best_ask = None
    state.started = False

    # Check daily reset and risk management before monitoring this window
    _check_and_reset_daily_state(state)
    if _should_skip_window(state):
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, existing_ws, False

    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch

    # Skip stale windows on fresh attach. For strategies with a delayed entry
    # band, keep monitoring until that band has actually elapsed.
    skip_threshold, skip_reason = _strategy_attach_skip_threshold(strategy, window)
    if not preopened and elapsed_since_start > skip_threshold:
        log_event(log, logging.INFO, WINDOW, {
            "action": "SKIP",
            "window": window.short_label,
            "elapsed": elapsed_since_start,
            "reason": skip_reason,
        })
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, ws, False

    # Subscribe to both tokens; strategy resolves the effective side.
    token_ids = [window.up_token, window.down_token]
    # Initial side for logging; actual side may be overridden by state.target_side
    buy_token_id, price_token_id = _side_token(window, side)
    if ws is None:
        ws = PriceStream(on_price=_noop_price_callback)
    new_callback = functools.partial(
        _on_price_update, window=window, state=state, ws=ws, dry_run=dry_run,
        trade_config=trade_config, strategy=strategy, side=side,
    )
    ws.set_on_price(new_callback)

    if existing_ws is not None:
        # Reuse existing WS — switch subscription to new window's tokens
        await ws.switch_tokens(token_ids)
    else:
        # First window — create new WS connection
        await ws.connect(token_ids)

    # Pre-fetch order params during wait time to reduce order placement delay.
    from polybot.core.client import prefetch_order_params
    for tid in token_ids:
        await asyncio.to_thread(prefetch_order_params, tid)

    # Wait for window start if not yet started
    if elapsed_since_start < 0:
        wait_sec = window.start_epoch - now_epoch
        log.debug("Waiting %ds for window to start... (WS pre-connected)", wait_sec)
        await asyncio.sleep(wait_sec)

    # Window is now live — enable trading
    state.started = True

    # Notify strategy of window start so it can initialize window state.
    if hasattr(strategy, 'set_window_start'):
        strategy.set_window_start(window.start_epoch)

    # Seed BTC open price via REST if WS feed has no mid-window coverage.
    if hasattr(strategy, 'preload_open_btc'):
        await strategy.preload_open_btc(window.start_epoch)

    # Price should already be cached from WS pre-connection
    # Use UP token price as the reference entry signal input.
    opening_token = window.up_token
    opening_price = ws.get_latest_price(opening_token)
    if opening_price is None:
        opening_price = await get_midpoint_async(opening_token)

    if opening_price is not None:
        if not state.bought:
            if strategy.should_buy(opening_price, state):
                if state.target_side is not None:
                    buy_token_id, price_token_id = _side_token(window, state.target_side)
                trade_amount = trade_config.amount_for_signal_strength(state.target_signal_strength)
                entry_ask_level = trade_config.base_entry_ask_level()
                max_entry_price = _entry_price_cap(strategy, state)
                quote = _cap_limited_depth_quote(
                    ws,
                    buy_token_id,
                    trade_amount,
                    max_entry_price,
                    max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
                    min_entry_level=entry_ask_level,
                    low_price_threshold=trade_config.low_price_threshold,
                    low_price_entry_level=trade_config.low_price_entry_ask_level,
                )
                if not quote.enough:
                    state.target_entry_price = None
                    _log_depth_skip(
                        state,
                        state.target_side or side,
                        opening_price,
                        quote,
                        max_entry_price,
                        trade_amount,
                        "cap-limited book depth insufficient",
                    )
                else:
                    _log_signal_eval(
                        state,
                        state.target_side or side,
                        opening_price,
                        quote.best_ask_level_1,
                        quote.price,
                        max_entry_price,
                        depth_notional=quote.cap_notional,
                        depth_levels_used=quote.levels_used,
                    )
                    if not _entry_ask_changed(state, state.target_side or side, quote.price_hint):
                        state.target_entry_price = None
                    else:
                        state.target_entry_price = quote.price
                        await _handle_opening_price(
                            window, state, buy_token_id, opening_price, dry_run, trade_config, strategy, state.target_side or side,
                            best_ask=quote.price_hint,
                            target_entry_ask=quote.price,
                            best_ask_level_1=quote.best_ask_level_1,
                            best_ask_age_sec=quote.ask_age_sec,
                            depth_levels_used=quote.levels_used,
                            depth_notional=quote.cap_notional,
                            depth_skipped_levels=quote.skipped_levels,
                            entry_ask_level=quote.entry_ask_level,
                            book_ask_preview=quote.preview,
                            price_hint_refresher=_price_hint_refresher(ws, buy_token_id, strategy, trade_config, state),
                        )
                        # Re-resolve token if strategy set target_side during opening buy
                        if state.target_side is not None:
                            buy_token_id, price_token_id = _side_token(window, state.target_side)
    # Monitor until window expires or exit triggered (ws is NOT closed inside)
    next_win = await _monitor_single_window(
        window, state, ws, dry_run, trade_config, strategy, series, side,
        prefetch_next_window=prefetch_next_window,
    )

    if next_win is not None:
        return next_win, ws, True

    if not prefetch_next_window:
        return None, ws, True

    # Fallback: window ended without pre-fetch
    next_win = _sanitize_next_window(window, find_next_window())
    if next_win is None:
        log_event(log, logging.WARNING, MARKET, {
            "action": "NOT_FOUND",
            "message": "No next window found",
        })
        return None, ws, True

    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - _PREOPEN_BUFFER

    if now_epoch < wake_epoch:
        log.debug(
            "Pre-open: sleeping %ds until %s starts at %s",
            wake_epoch - now_epoch, next_win.short_label, next_win.start_time,
        )
        await asyncio.sleep(wake_epoch - now_epoch)

    return next_win, ws, True


async def _on_price_update(
    update: PriceUpdate,
    window: MarketWindow,
    state: MonitorState,
    ws: PriceStream,
    dry_run: bool,
    trade_config: TradeConfig,
    strategy: Optional[Strategy] = None,
    side: str = "up",
) -> None:
    """
    Called by PriceStream whenever a price update arrives.
    Triggers the strategy entry when the window and price state allow it.
    """
    if not state.started:
        return

    # Resolve effective side: the strategy may override side via target_side.
    effective_side = state.target_side if state.target_side is not None else side
    buy_token_id, _ = _side_token(window, effective_side)

    price = update.midpoint
    if price is None:
        return

    # Entry signals are computed from the UP token reference price.
    if not state.bought and update.token_id != window.up_token:
        return

    log.debug(
        "WS price update | %s: %s (source=%s, bid=%s, ask=%s)",
        side.upper(), price, update.source,
        update.best_bid, update.best_ask,
    )

    if state.trade_lock.locked():
        return

    async with state.trade_lock:
        if state.buy_blocked_window_cap:
            return

        if not state.bought:
            if (
                trade_config.max_entries_per_window is not None
                and state.entry_count >= trade_config.max_entries_per_window
            ):
                log_event(log, logging.WARNING, SIGNAL, {
                    "action": "BLOCKED_WINDOW_CAP",
                    "window": window.short_label,
                    "entry_count": state.entry_count,
                    "max_entries": trade_config.max_entries_per_window,
                })
                state.buy_blocked_window_cap = True
                return
            if strategy.should_buy(price, state):
                if state.target_side is not None:
                    effective_side = state.target_side
                    buy_token_id, _ = _side_token(window, effective_side)
                trade_amount = trade_config.amount_for_signal_strength(state.target_signal_strength)
                entry_ask_level = trade_config.base_entry_ask_level()
                max_entry_price = _entry_price_cap(strategy, state)
                quote = _cap_limited_depth_quote(
                    ws,
                    buy_token_id,
                    trade_amount,
                    max_entry_price,
                    max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
                    min_entry_level=entry_ask_level,
                    low_price_threshold=trade_config.low_price_threshold,
                    low_price_entry_level=trade_config.low_price_entry_ask_level,
                )
                if not quote.enough:
                    state.target_entry_price = None
                    _log_depth_skip(
                        state,
                        effective_side,
                        price,
                        quote,
                        max_entry_price,
                        trade_amount,
                        "cap-limited book depth insufficient",
                    )
                    return
                _log_signal_eval(
                    state,
                    effective_side,
                    price,
                    quote.best_ask_level_1,
                    quote.price,
                    max_entry_price,
                    depth_notional=quote.cap_notional,
                    depth_levels_used=quote.levels_used,
                )
                if not _entry_ask_changed(state, effective_side, quote.price_hint):
                    state.target_entry_price = None
                    return
                state.target_entry_price = quote.price
                log_event(log, logging.INFO, SIGNAL, {
                    "action": "BUY_SIGNAL",
                    "price": quote.price,
                    "target_entry_ask": quote.price,
                    "best_ask_level_1": quote.best_ask_level_1,
                    "price_hint": quote.price_hint,
                    "depth_levels_used": quote.levels_used,
                    "depth_notional": round(quote.cap_notional, 4),
                    "depth_total_levels": quote.total_levels,
                    "depth_skipped_levels": quote.skipped_levels,
                    "entry_ask_level": quote.entry_ask_level,
                    "book_ask_preview": quote.preview,
                    "signal_price": price,
                    "side": effective_side.upper(),
                    "window": window.short_label,
                    "confidence": state.target_signal_confidence,
                    "max_entry_price": max_entry_price,
                    "signal_strength": (
                        round(state.target_signal_strength, 3)
                        if state.target_signal_strength is not None
                        else None
                    ),
                    "past_signal_strength": (
                        round(state.target_past_signal_strength, 3)
                        if state.target_past_signal_strength is not None
                        else None
                    ),
                    "remaining_sec": (
                        round(state.target_remaining_sec)
                        if state.target_remaining_sec is not None
                        else None
                    ),
                    "amount": trade_amount,
                    "best_ask_age_ms": round(quote.ask_age_sec * 1000) if quote.ask_age_sec is not None else None,
                })
                await _handle_opening_price(
                    window, state, buy_token_id, price, dry_run, trade_config, strategy, effective_side,
                    best_ask=quote.price_hint,
                    target_entry_ask=quote.price,
                    best_ask_level_1=quote.best_ask_level_1,
                    best_ask_age_sec=quote.ask_age_sec,
                    depth_levels_used=quote.levels_used,
                    depth_notional=quote.cap_notional,
                    depth_skipped_levels=quote.skipped_levels,
                    entry_ask_level=quote.entry_ask_level,
                    book_ask_preview=quote.preview,
                    price_hint_refresher=_price_hint_refresher(ws, buy_token_id, strategy, trade_config, state),
                )
            return

        is_held_token_update = update.token_id == buy_token_id
        if is_held_token_update:
            state.latest_midpoint = price
            await _maybe_handle_stop_loss(
                window,
                state,
                ws,
                buy_token_id,
                dry_run,
                trade_config,
                effective_side,
            )
        return


async def _handle_opening_price(
    window: MarketWindow,
    state: MonitorState,
    buy_token_id: str,
    price: float,
    dry_run: bool,
    trade_config: TradeConfig,
    strategy: Optional[Strategy] = None,
    side: str = "up",
    best_ask: Optional[float] = None,
    target_entry_ask: Optional[float] = None,
    best_ask_level_1: Optional[float] = None,
    best_ask_age_sec: Optional[float] = None,
    depth_levels_used: Optional[int] = None,
    depth_notional: Optional[float] = None,
    depth_skipped_levels: Optional[int] = None,
    entry_ask_level: Optional[int] = None,
    book_ask_preview: Optional[list[tuple[float, float]]] = None,
    price_hint_refresher=None,
) -> None:
    """Handle the opening price check and buy decision."""
    if state.bought:
        return

    # Defense in depth: if buy was already attempted in this window, don't retry.
    # _on_price_update also gates on buy_blocked_window_cap before calling us.
    if state.exit_triggered:
        return

    # Re-resolve token if strategy overrode direction
    if state.target_side is not None:
        buy_token_id, _ = _side_token(window, state.target_side)
    buy_price = state.target_entry_price if state.target_entry_price is not None else price
    trade_amount = trade_config.amount_for_signal_strength(state.target_signal_strength)
    state.entry_amount = trade_amount
    if not dry_run:
        state.bought = True
        t_signal = time.time()
        log_event(log, logging.INFO, TRADE, {
            "action": "BUY_PREP",
            "side": side.upper(),
            "window": window.short_label,
            "token": buy_token_id[:20],
            "signal_price": price,
            "target_price": buy_price,
            "target_entry_ask": target_entry_ask,
            "best_ask_level_1": best_ask_level_1,
            "price_hint": best_ask,
            "depth_levels_used": depth_levels_used,
            "depth_notional": round(depth_notional, 4) if depth_notional is not None else None,
            "depth_skipped_levels": depth_skipped_levels,
            "entry_ask_level": entry_ask_level,
            "book_ask_preview": book_ask_preview,
            "amount": trade_amount,
            "signal_strength": (
                round(state.target_signal_strength, 3)
                if state.target_signal_strength is not None
                else None
            ),
            "past_signal_strength": (
                round(state.target_past_signal_strength, 3)
                if state.target_past_signal_strength is not None
                else None
            ),
            "remaining_sec": (
                round(state.target_remaining_sec)
                if state.target_remaining_sec is not None
                else None
            ),
            "best_ask_age_ms": round(best_ask_age_sec * 1000) if best_ask_age_sec is not None else None,
        })
        result = await buy_token(
            buy_token_id, trade_amount,
            price_hint=best_ask,
            price_hint_refresher=price_hint_refresher,
        )
        if result.success:
            entry_latency_ms = round((time.time() - t_signal) * 1000)
            state.entry_count += 1
            state.entry_timestamps.append(time.time())
            if result.filled_size > 0 and result.avg_price > 0:
                state.holding_size = result.filled_size
            elif result.avg_price > 0:
                state.holding_size = trade_amount / result.avg_price
            else:
                state.holding_size = trade_amount / buy_price if buy_price > 0 else trade_amount
            state.entry_avg_price = result.avg_price if result.avg_price > 0 else buy_price
            state.entry_price = state.entry_avg_price
            log_event(log, logging.INFO, TRADE, {
                "action": "BUY_FILLED",
                "side": side.upper(),
                "price": buy_price,
                "amount": trade_amount,
                "shares": state.holding_size,
                "window": window.short_label,
                "entry_latency_ms": entry_latency_ms,
            })
            if strategy is not None and hasattr(strategy, "on_buy_confirmed"):
                strategy.on_buy_confirmed(time.time())
        else:
            state.bought = False
            state.exit_triggered = True
            state.buy_blocked_window_cap = True
            log_event(log, logging.WARNING, TRADE, {
                "action": "BUY_FAILED",
                "side": side.upper(),
                "price": buy_price,
                "amount": trade_amount,
                "message": result.message,
                "window": window.short_label,
                "note": "window locked to prevent duplicate entries",
            })
            if "INSUFFICIENT_FUNDS" in result.message:
                log_event(log, logging.CRITICAL, TRADE, {
                    "action": "STOP_INSUFFICIENT_FUNDS",
                    "window": window.short_label,
                    "message": result.message,
                })
                raise RuntimeError(result.message)
    else:
        state.bought = True
        state.entry_count += 1
        state.entry_timestamps.append(time.time())
        state.holding_size = trade_amount / buy_price if buy_price > 0 else trade_amount
        state.entry_price = buy_price
        state.entry_avg_price = buy_price
        log_event(log, logging.INFO, TRADE, {
            "action": "BUY_PREP",
            "side": side.upper(),
            "window": window.short_label,
            "token": buy_token_id[:20],
            "signal_price": price,
            "target_price": buy_price,
            "target_entry_ask": target_entry_ask,
            "best_ask_level_1": best_ask_level_1,
            "price_hint": best_ask,
            "depth_levels_used": depth_levels_used,
            "depth_notional": round(depth_notional, 4) if depth_notional is not None else None,
            "depth_skipped_levels": depth_skipped_levels,
            "entry_ask_level": entry_ask_level,
            "book_ask_preview": book_ask_preview,
            "amount": trade_amount,
            "signal_strength": (
                round(state.target_signal_strength, 3)
                if state.target_signal_strength is not None
                else None
            ),
            "past_signal_strength": (
                round(state.target_past_signal_strength, 3)
                if state.target_past_signal_strength is not None
                else None
            ),
            "remaining_sec": (
                round(state.target_remaining_sec)
                if state.target_remaining_sec is not None
                else None
            ),
            "best_ask_age_ms": round(best_ask_age_sec * 1000) if best_ask_age_sec is not None else None,
            "dry_run": True,
        })
        log_event(log, logging.INFO, TRADE, {
            "action": "BUY_FILLED",
            "side": side.upper(),
            "price": buy_price,
            "amount": trade_amount,
            "shares": state.holding_size,
            "window": window.short_label,
            "dry_run": True,
        })
        if strategy is not None and hasattr(strategy, "on_buy_confirmed"):
            strategy.on_buy_confirmed(time.time())
    state.target_entry_price = None
