"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
import datetime
import functools
import logging
import time
from typing import Optional

from polybot.core import config
from polybot.core.client import get_midpoint_async, get_token_balance
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
from polybot.strategies.crowd_m1 import CrowdM1Strategy
from polybot.strategies.paired_window import PairedWindowStrategy
from polybot.trade_config import TradeConfig
from .fak_execution import place_fak_buy, place_fak_stop_loss_sell
from .fak_quotes import (
    BidDepthQuote,
    CapDepthQuote,
    buffer_sell_price_hint,
    cap_limited_depth_quote as _cap_limited_depth_quote,
    stop_loss_bid_quote as _stop_loss_bid_quote,
)

log = logging.getLogger(__name__)

_PREOPEN_BUFFER = 10  # seconds before window start to wake up
_STARTED_SKIP_THRESHOLD = 60  # allow attaching to a window within its first minute
_SIGNAL_EVAL_LOG_INTERVAL_SEC = 5.0
_STOP_LOSS_CHECK_LOG_INTERVAL_SEC = 5.0
_STOP_LOSS_PREWARM_SEC = 5.0
_SETTLEMENT_MARK_FRESH_SEC = 5.0


async def _noop_price_callback(update: PriceUpdate) -> None:
    """Placeholder callback used before a PriceStream is fully wired."""
    return None


def _mark_fatal_error(state: MonitorState, message: str) -> None:
    if state.fatal_error is None:
        state.fatal_error = message


def _raise_if_fatal(state: MonitorState) -> None:
    if state.fatal_error is not None:
        raise RuntimeError(state.fatal_error)


def _record_min_max(
    current_min: Optional[float],
    current_max: Optional[float],
    value: Optional[float],
) -> tuple[Optional[float], Optional[float]]:
    if value is None:
        return current_min, current_max
    next_min = value if current_min is None else min(current_min, value)
    next_max = value if current_max is None else max(current_max, value)
    return next_min, next_max


def _record_entry_replay_quote(
    state: MonitorState,
    *,
    leading_ask: Optional[float],
    quote: CapDepthQuote,
) -> None:
    """Aggregate entry quote evidence for compact dry-run replay logs."""
    state.entry_replay_check_count += 1
    state.entry_replay_min_leading_ask, state.entry_replay_max_leading_ask = _record_min_max(
        state.entry_replay_min_leading_ask,
        state.entry_replay_max_leading_ask,
        leading_ask,
    )
    state.entry_replay_min_best_ask, state.entry_replay_max_best_ask = _record_min_max(
        state.entry_replay_min_best_ask,
        state.entry_replay_max_best_ask,
        quote.best_ask_level_1,
    )
    state.entry_replay_min_selected_ask, state.entry_replay_max_selected_ask = _record_min_max(
        state.entry_replay_min_selected_ask,
        state.entry_replay_max_selected_ask,
        quote.price,
    )
    state.entry_replay_max_depth_notional = max(
        state.entry_replay_max_depth_notional,
        quote.cap_notional,
    )
    ask_age_ms = round(quote.ask_age_sec * 1000) if quote.ask_age_sec is not None else None
    state.entry_replay_min_best_ask_age_ms, state.entry_replay_max_best_ask_age_ms = _record_min_max(
        state.entry_replay_min_best_ask_age_ms,
        state.entry_replay_max_best_ask_age_ms,
        ask_age_ms,
    )
    if quote.enough:
        state.entry_replay_signal_count += 1


def _record_stop_replay_quote(
    state: MonitorState,
    *,
    quote: BidDepthQuote,
) -> None:
    """Aggregate stop-loss quote evidence for compact dry-run replay logs."""
    state.stop_replay_check_count += 1
    state.stop_replay_min_best_bid, state.stop_replay_max_best_bid = _record_min_max(
        state.stop_replay_min_best_bid,
        state.stop_replay_max_best_bid,
        quote.best_bid_level_1,
    )
    state.stop_replay_min_selected_bid, state.stop_replay_max_selected_bid = _record_min_max(
        state.stop_replay_min_selected_bid,
        state.stop_replay_max_selected_bid,
        quote.price,
    )
    state.stop_replay_max_bid_shares_available = max(
        state.stop_replay_max_bid_shares_available,
        quote.shares_available,
    )
    bid_age_ms = round(quote.bid_age_sec * 1000) if quote.bid_age_sec is not None else None
    state.stop_replay_min_best_bid_age_ms, state.stop_replay_max_best_bid_age_ms = _record_min_max(
        state.stop_replay_min_best_bid_age_ms,
        state.stop_replay_max_best_bid_age_ms,
        bid_age_ms,
    )


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
            max_entry_level=entry_ask_level,
            low_price_threshold=trade_config.low_price_threshold,
            low_price_entry_level=trade_config.low_price_entry_ask_level,
            dynamic_entry_levels=trade_config.dynamic_entry_levels,
            max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
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


def _log_stop_loss_check(
    state: MonitorState,
    *,
    side: str,
    window: MarketWindow,
    remaining: float,
    entry_price: float,
    stop_price: float,
    quote: BidDepthQuote,
    shares: float,
    reason: str,
) -> None:
    if reason in state.stop_loss_check_logged_reasons:
        return
    state.stop_loss_check_logged_reasons.add(reason)
    log_event(log, logging.INFO, TRADE, {
        "action": "STOP_LOSS_CHECK",
        "reason": reason,
        "side": side.upper(),
        "window": window.short_label,
        "remaining_sec": round(remaining, 3),
        "entry_price": round(entry_price, 4),
        "stop_price": round(stop_price, 4),
        "best_bid_level_1": quote.best_bid_level_1,
        "target_sell_bid": quote.price,
        "price_hint": quote.price_hint,
        "bid_levels_used": quote.levels_used,
        "bid_shares_available": round(quote.shares_available, 4),
        "shares_to_sell": round(shares, 6),
        "sell_bid_level": quote.sell_bid_level,
        "bid_age_ms": round(quote.bid_age_sec * 1000) if quote.bid_age_sec is not None else None,
        "book_bid_preview": quote.preview,
        "quote_enough": quote.enough,
    })


def _log_stop_loss_book_freshness(
    state: MonitorState,
    *,
    side: str,
    window: MarketWindow,
    remaining: float,
    phase: str,
    best_bid_age_ms: Optional[int],
) -> None:
    key = ("book_freshness", phase)
    now = time.time()
    if (
        state.last_stop_loss_check_key == key
        and now - state.last_stop_loss_check_logged_at < _STOP_LOSS_CHECK_LOG_INTERVAL_SEC
    ):
        return
    state.last_stop_loss_check_key = key
    state.last_stop_loss_check_logged_at = now
    log_event(log, logging.INFO, TRADE, {
        "action": "STOP_LOSS_BOOK_FRESHNESS",
        "side": side.upper(),
        "window": window.short_label,
        "phase": phase,
        "remaining_sec": round(remaining, 3),
        "best_bid_age_ms": best_bid_age_ms,
    })


async def _sync_holding_balance_after_buy(
    state: MonitorState,
    token_id: str,
    window: MarketWindow,
    side: str,
    entry_count: int,
    *,
    delay_sec: float = 8.0,
) -> None:
    """Refresh held shares from CLOB shortly after a successful FAK BUY."""
    await asyncio.sleep(delay_sec)
    if not state.bought or state.exit_triggered or state.entry_count != entry_count:
        return
    live_balance = await asyncio.to_thread(get_token_balance, token_id, False)
    if live_balance is None:
        log_event(log, logging.WARNING, TRADE, {
            "action": "HOLDING_BALANCE_SYNC_FAILED",
            "side": side.upper(),
            "window": window.short_label,
            "token": token_id[:20],
            "state_shares": state.holding_size,
        })
        return
    if live_balance <= 1e-9:
        log_event(log, logging.WARNING, TRADE, {
            "action": "HOLDING_BALANCE_SYNC_EMPTY",
            "side": side.upper(),
            "window": window.short_label,
            "token": token_id[:20],
            "state_shares": state.holding_size,
            "balance_shares": live_balance,
        })
        return
    if abs(live_balance - state.holding_size) > 1e-6:
        old_shares = state.holding_size
        state.holding_size = live_balance
        log_event(log, logging.INFO, TRADE, {
            "action": "HOLDING_BALANCE_SYNCED",
            "side": side.upper(),
            "window": window.short_label,
            "token": token_id[:20],
            "old_shares": old_shares,
            "balance_shares": live_balance,
        })
        return
    log_event(log, logging.INFO, TRADE, {
        "action": "HOLDING_BALANCE_SYNCED",
        "side": side.upper(),
        "window": window.short_label,
        "token": token_id[:20],
        "balance_shares": live_balance,
        "unchanged": True,
    })


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
        "active_theta_pct": (
            round(state.target_active_theta_pct, 4)
            if state.target_active_theta_pct is not None
            else None
        ),
        "remaining_sec": (
            round(state.target_remaining_sec)
            if state.target_remaining_sec is not None
            else None
        ),
    })


async def _simulated_dry_sell_fill_price(
    ws: PriceStream,
    token_id: str,
    *,
    fallback_price: Optional[float],
    min_sell_price: Optional[float],
) -> Optional[float]:
    """Return a pessimistic dry SELL fill after simulated FAK latency."""
    await asyncio.sleep(config.DRY_RUN_SIMULATED_FAK_LATENCY_SEC)
    try:
        latest_bid = ws.get_latest_best_bid(
            token_id,
            max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
            level=1,
        )
    except TypeError:
        latest_bid = ws.get_latest_best_bid(token_id)
    except AttributeError:
        latest_bid = None
    if latest_bid is None:
        return fallback_price
    return buffer_sell_price_hint(
        token_id,
        float(latest_bid),
        buffer_ticks=config.DRY_RUN_SIMULATED_PRICE_BUFFER_TICKS,
        min_price=min_sell_price,
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
    if entry_price < trade_config.stop_loss_disable_below_entry_price:
        return
    stop_price = max(
        trade_config.stop_loss_min_sell_price,
        trade_config.stop_loss_trigger_price,
    )
    state.stop_loss_price = stop_price

    shares_to_sell = state.holding_size
    balance_source = "state"
    quote = _stop_loss_bid_quote(
        ws,
        token_id,
        shares_to_sell,
        max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
        min_sell_level=trade_config.stop_loss_bid_level(),
        min_sell_price=trade_config.stop_loss_min_sell_price,
    )
    if quote.best_bid_level_1 is None:
        if dry_run:
            _record_stop_replay_quote(state, quote=quote)
            state.stop_replay_missing_or_stale_bid_count += 1
        _log_stop_loss_check(
            state,
            side=side,
            window=window,
            remaining=remaining,
            entry_price=entry_price,
            stop_price=stop_price,
            quote=quote,
            shares=shares_to_sell,
            reason="missing_or_stale_bid",
        )
        return
    if dry_run:
        _record_stop_replay_quote(state, quote=quote)
    if quote.best_bid_level_1 > stop_price:
        return
    if not quote.enough or quote.price is None:
        if dry_run:
            state.stop_replay_insufficient_depth_count += 1
        _log_stop_loss_check(
            state,
            side=side,
            window=window,
            remaining=remaining,
            entry_price=entry_price,
            stop_price=stop_price,
            quote=quote,
            shares=shares_to_sell,
            reason="insufficient_bid_depth",
        )
        return

    if not dry_run:
        live_balance = await asyncio.to_thread(get_token_balance, token_id, False)
        if live_balance is not None:
            balance_source = "clob_balance"
            shares_to_sell = max(0.0, live_balance)
            if shares_to_sell <= 1e-9:
                log_event(log, logging.WARNING, TRADE, {
                    "action": "STOP_LOSS_NO_POSITION",
                    "side": side.upper(),
                    "window": window.short_label,
                    "state_shares": state.holding_size,
                    "balance_shares": live_balance,
                })
                state.holding_size = 0.0
                state.bought = False
                state.exit_triggered = True
                return
            if abs(shares_to_sell - state.holding_size) > 1e-6:
                log_event(log, logging.INFO, TRADE, {
                    "action": "STOP_LOSS_BALANCE_SYNC",
                    "side": side.upper(),
                    "window": window.short_label,
                    "state_shares": state.holding_size,
                    "balance_shares": shares_to_sell,
                })
                state.holding_size = shares_to_sell
                quote = _stop_loss_bid_quote(
                    ws,
                    token_id,
                    shares_to_sell,
                    max_age_sec=config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
                    min_sell_level=trade_config.stop_loss_bid_level(),
                    min_sell_price=trade_config.stop_loss_min_sell_price,
                )
                if quote.best_bid_level_1 is None or quote.best_bid_level_1 > stop_price or not quote.enough or quote.price is None:
                    log_event(log, logging.INFO, TRADE, {
                        "action": "STOP_LOSS_BALANCE_RECHECK_ABORT",
                        "side": side.upper(),
                        "window": window.short_label,
                        "shares": shares_to_sell,
                        "stop_price": round(stop_price, 4),
                        "sell_price": quote.price,
                        "best_bid_level_1": quote.best_bid_level_1,
                        "price_hint": quote.price_hint,
                        "bid_shares_available": round(quote.shares_available, 4),
                    })
                    return

    state.stop_loss_attempted = True
    if dry_run:
        state.stop_replay_triggered_count += 1
    log_event(log, logging.WARNING, TRADE, {
        "action": "STOP_LOSS_TRIGGERED",
        "side": side.upper(),
        "window": window.short_label,
        "entry_price": entry_price,
        "stop_price": round(stop_price, 4),
        "sell_price": quote.price,
        "price_hint": quote.price_hint,
        "shares": shares_to_sell,
        "shares_source": balance_source,
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
        sell_price = await _simulated_dry_sell_fill_price(
            ws,
            token_id,
            fallback_price=quote.price_hint or quote.price,
            min_sell_price=trade_config.stop_loss_min_sell_price,
        )
        if sell_price is None:
            sell_price = quote.price_hint or quote.price or 0.0
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
            "simulated_latency_ms": round(config.DRY_RUN_SIMULATED_FAK_LATENCY_SEC * 1000),
            "simulated_price_buffer_ticks": config.DRY_RUN_SIMULATED_PRICE_BUFFER_TICKS,
            "dry_run": True,
        })
        state.holding_size = 0.0
        state.bought = False
        state.exit_triggered = True
        state.stop_loss_triggered = True
        return

    result = await place_fak_stop_loss_sell(
        token_id,
        shares_to_sell,
        price_hint=quote.price_hint,
        price_hint_refresher=_stop_loss_price_hint_refresher(ws, token_id, trade_config, state),
        retry_count=trade_config.stop_loss_retry_count,
    )
    if not result.success:
        if "INSUFFICIENT_FUNDS" in result.message:
            _mark_fatal_error(state, result.message)
        log_event(log, logging.WARNING, TRADE, {
            "action": "STOP_LOSS_FAILED",
            "side": side.upper(),
            "window": window.short_label,
            "shares": shares_to_sell,
            "shares_source": balance_source,
            "price_hint": quote.price_hint,
            "message": result.message,
        })
        _raise_if_fatal(state)
        return

    sold_size = min(result.filled_size or shares_to_sell, shares_to_sell)
    sell_price = result.avg_price or quote.price_hint or quote.price or 0.0
    cost_basis = state.entry_amount * (sold_size / shares_to_sell) if shares_to_sell > 0 else 0.0
    realized_pnl = sold_size * sell_price - cost_basis
    _process_trade_result(state, realized_pnl >= 0, realized_pnl, trade_config)
    log_event(log, logging.WARNING, TRADE, {
        "action": "STOP_LOSS_FILLED",
        "side": side.upper(),
        "window": window.short_label,
        "avg_price": sell_price,
        "shares": sold_size,
        "requested_shares": shares_to_sell,
        "shares_source": balance_source,
        "realized_pnl": round(realized_pnl, 4),
        "daily_realized_pnl": round(state.daily_realized_pnl, 4),
        "order_id": result.order_id,
    })
    state.holding_size = max(0.0, shares_to_sell - sold_size)
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

    Active strategy windows should be attachable until the end of the configured
    entry band. Missing strategy uses the base 60s fallback.
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
    if dry_run and state.entry_replay_check_count > 0:
        data["entry_replay"] = {
            "checks": state.entry_replay_check_count,
            "signals": state.entry_replay_signal_count,
            "buy_signals": state.entry_replay_buy_signal_count,
            "leading_ask_min": state.entry_replay_min_leading_ask,
            "leading_ask_max": state.entry_replay_max_leading_ask,
            "best_ask_min": state.entry_replay_min_best_ask,
            "best_ask_max": state.entry_replay_max_best_ask,
            "selected_ask_min": state.entry_replay_min_selected_ask,
            "selected_ask_max": state.entry_replay_max_selected_ask,
            "depth_notional_max": round(state.entry_replay_max_depth_notional, 4),
            "best_ask_age_ms_min": state.entry_replay_min_best_ask_age_ms,
            "best_ask_age_ms_max": state.entry_replay_max_best_ask_age_ms,
        }
    if dry_run and state.stop_replay_check_count > 0:
        data["stop_replay"] = {
            "checks": state.stop_replay_check_count,
            "triggered": state.stop_replay_triggered_count,
            "missing_or_stale_bid": state.stop_replay_missing_or_stale_bid_count,
            "insufficient_depth": state.stop_replay_insufficient_depth_count,
            "best_bid_min": state.stop_replay_min_best_bid,
            "best_bid_max": state.stop_replay_max_best_bid,
            "selected_bid_min": state.stop_replay_min_selected_bid,
            "selected_bid_max": state.stop_replay_max_selected_bid,
            "bid_shares_available_max": round(state.stop_replay_max_bid_shares_available, 4),
            "best_bid_age_ms_min": state.stop_replay_min_best_bid_age_ms,
            "best_bid_age_ms_max": state.stop_replay_max_best_bid_age_ms,
        }
    log_event(log, logging.INFO, WINDOW, data)


def _side_token(window: MarketWindow, side: str) -> tuple[str, str]:
    """Return (buy_token, price_token) based on trade side."""
    if side == "down":
        return window.down_token, window.down_token
    return window.up_token, window.up_token


def _is_crowd_m1_strategy(strategy: Optional[Strategy]) -> bool:
    """Return true for the crowd-following strategy."""
    return isinstance(strategy, CrowdM1Strategy)


def _is_paired_window_strategy(strategy: Optional[Strategy]) -> bool:
    """Return true for the paired BTC-window strategy."""
    return isinstance(strategy, PairedWindowStrategy)


def _entry_update_allowed(
    strategy: Optional[Strategy],
    window: MarketWindow,
    update: PriceUpdate,
) -> bool:
    """Return whether a WS update should trigger entry evaluation."""
    if _is_crowd_m1_strategy(strategy):
        return update.token_id in (window.up_token, window.down_token)
    if _is_paired_window_strategy(strategy):
        return update.token_id == window.up_token
    return update.token_id == window.up_token


def _stop_loss_remaining_state(
    window: MarketWindow,
    trade_config: TradeConfig,
    now: Optional[float] = None,
) -> tuple[str, float]:
    """Return stop-loss timing state and remaining seconds."""
    remaining = window.end_epoch - (time.time() if now is None else now)
    if remaining > trade_config.stop_loss_start_remaining_sec + _STOP_LOSS_PREWARM_SEC:
        return "before", remaining
    if remaining > trade_config.stop_loss_start_remaining_sec:
        return "prewarm", remaining
    if remaining < trade_config.stop_loss_end_remaining_sec:
        return "after", remaining
    return "active", remaining


def _market_snapshot_from_ws(
    window: MarketWindow,
    ws: PriceStream,
    update: Optional[PriceUpdate] = None,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Read UP/DOWN midpoints and best asks, using current update if cache lags."""
    up_mid = ws.get_latest_price(window.up_token)
    down_mid = ws.get_latest_price(window.down_token)
    up_best_ask = ws.get_latest_best_ask(window.up_token)
    down_best_ask = ws.get_latest_best_ask(window.down_token)
    up_best_ask_age_sec = _best_ask_age_sec(ws, window.up_token)
    down_best_ask_age_sec = _best_ask_age_sec(ws, window.down_token)
    if update is not None:
        if update.token_id == window.up_token:
            if update.midpoint is not None:
                up_mid = update.midpoint
            if update.best_ask is not None:
                up_best_ask = update.best_ask
                up_best_ask_age_sec = 0.0
        elif update.token_id == window.down_token:
            if update.midpoint is not None:
                down_mid = update.midpoint
            if update.best_ask is not None:
                down_best_ask = update.best_ask
                down_best_ask_age_sec = 0.0
    return up_mid, down_mid, up_best_ask, down_best_ask, up_best_ask_age_sec, down_best_ask_age_sec


def _best_ask_age_sec(ws: PriceStream, token_id: str) -> Optional[float]:
    if not hasattr(ws, "get_latest_best_ask_age"):
        return None
    age = ws.get_latest_best_ask_age(token_id)
    if not isinstance(age, (int, float)):
        return None
    return float(age)


def _best_ask_age_ms(ws: PriceStream, token_id: str) -> Optional[int]:
    age = _best_ask_age_sec(ws, token_id)
    return round(age * 1000) if age is not None else None


def _best_bid_age_ms(ws: PriceStream, token_id: str) -> Optional[int]:
    if not hasattr(ws, "get_latest_best_bid_age"):
        return None
    age = ws.get_latest_best_bid_age(token_id)
    return round(age * 1000) if age is not None else None


def _snapshot_entry_band_active(strategy: Strategy, window: MarketWindow, now: int) -> bool:
    """Return true when a snapshot-driven strategy should be actively evaluated."""
    if not _is_crowd_m1_strategy(strategy):
        return False
    start_remaining = getattr(strategy, "entry_start_remaining_sec", None)
    end_remaining = getattr(strategy, "entry_end_remaining_sec", None)
    if start_remaining is None or end_remaining is None:
        return False
    remaining = window.end_epoch - now
    return float(end_remaining) <= remaining <= float(start_remaining)


def _effective_signal_price(
    strategy: Optional[Strategy],
    state: MonitorState,
    fallback_price: float,
) -> float:
    """Return the signal price that should be shown in entry logs."""
    if _is_crowd_m1_strategy(strategy) and state.signal_reference_price is not None:
        return state.signal_reference_price
    return fallback_price


async def _attempt_strategy_entry(
    window: MarketWindow,
    state: MonitorState,
    ws: PriceStream,
    dry_run: bool,
    trade_config: TradeConfig,
    strategy: Strategy,
    side: str,
    signal_price: float,
) -> None:
    """Run the shared strategy entry pipeline once using the given signal price."""
    effective_side = state.target_side if state.target_side is not None else side
    buy_token_id, _ = _side_token(window, effective_side)

    if trade_config.max_entries_per_window is not None and state.entry_count >= trade_config.max_entries_per_window:
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "BLOCKED_WINDOW_CAP",
            "window": window.short_label,
            "entry_count": state.entry_count,
            "max_entries": trade_config.max_entries_per_window,
        })
        state.buy_blocked_window_cap = True
        return

    if not strategy.should_buy(signal_price, state):
        return

    entry_signal_price = _effective_signal_price(strategy, state, signal_price)
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
        max_entry_level=entry_ask_level,
        low_price_threshold=trade_config.low_price_threshold,
        low_price_entry_level=trade_config.low_price_entry_ask_level,
        dynamic_entry_levels=trade_config.dynamic_entry_levels,
        max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
    )
    if dry_run:
        _record_entry_replay_quote(state, leading_ask=entry_signal_price, quote=quote)
    if not quote.enough:
        state.target_entry_price = None
        _log_depth_skip(
            state,
            effective_side,
            entry_signal_price,
            quote,
            max_entry_price,
            trade_amount,
            "cap-limited book depth insufficient",
        )
        return
    _log_signal_eval(
        state,
        effective_side,
        entry_signal_price,
        quote.best_ask_level_1,
        quote.price,
        max_entry_price,
        depth_notional=quote.cap_notional,
        depth_levels_used=quote.levels_used,
    )
    if not _entry_ask_changed(state, effective_side, quote.price_hint):
        state.target_entry_price = None
        return
    if dry_run:
        state.entry_replay_buy_signal_count += 1
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
        "signal_price": entry_signal_price,
        "side": effective_side.upper(),
        "window": window.short_label,
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
        "active_theta_pct": (
            round(state.target_active_theta_pct, 4)
            if state.target_active_theta_pct is not None
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
        window,
        state,
        buy_token_id,
        entry_signal_price,
        dry_run,
        trade_config,
        strategy,
        effective_side,
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
        _raise_if_fatal(state)
        now = int(time.time())
        if now >= window.end_epoch:
            if state.bought and not state.exit_triggered:
                # Post-window-end phase: record trade result and await auto-redeem
                # Polymarket has auto-redeem enabled, so position will be automatically
                # redeemed and funds returned to account. No manual sell needed.
                mark_price = state.latest_midpoint
                mark_price_age_sec = (
                    time.time() - state.latest_midpoint_received_at
                    if state.latest_midpoint_received_at is not None
                    else None
                )
                mark_price_fresh = (
                    mark_price is not None
                    and mark_price_age_sec is not None
                    and mark_price_age_sec <= _SETTLEMENT_MARK_FRESH_SEC
                )
                direction_correct = mark_price_fresh and mark_price > 0.5
                settlement_price = 1.0 if direction_correct else 0.0 if mark_price_fresh else None

                entry_amount = state.entry_amount or state.entry_price * state.holding_size
                realized_pnl = (
                    state.holding_size * settlement_price - entry_amount
                    if settlement_price is not None
                    else (
                        state.holding_size * mark_price - entry_amount
                        if mark_price is not None
                        else 0.0
                    )
                )
                trade_result_win = direction_correct if mark_price_fresh else realized_pnl > 0
                result_label = (
                    "WIN" if direction_correct else
                    "LOSS" if mark_price_fresh else
                    "MARK_STALE"
                )

                # Process trade result for risk management
                _process_trade_result(
                    state,
                    trade_result_win,
                    realized_pnl,
                    trade_config,
                )

                # Record trade resolution
                log_event(log, logging.INFO, TRADE, {
                    "action": "TRADE_RESOLVED",
                    "window": window.short_label,
                    "result": result_label,
                    "shares": state.holding_size,
                    "price": settlement_price if settlement_price is not None else mark_price,
                    "mark_price": mark_price,
                    "mark_price_age_sec": round(mark_price_age_sec, 3) if mark_price_age_sec is not None else None,
                    "mark_price_fresh": mark_price_fresh,
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
            _raise_if_fatal(state)
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

        if (
            ws is not None
            and state.bought
            and not state.exit_triggered
            and not state.trade_lock.locked()
        ):
            async with state.trade_lock:
                if state.bought and not state.exit_triggered:
                    effective_side = state.target_side if state.target_side is not None else side
                    buy_token_id, _ = _side_token(window, effective_side)
                    await _maybe_handle_stop_loss(
                        window,
                        state,
                        ws,
                        buy_token_id,
                        dry_run,
                        trade_config,
                        effective_side,
                    )

        if (
            ws is not None
            and strategy is not None
            and not state.bought
            and not state.buy_blocked_window_cap
            and _snapshot_entry_band_active(strategy, window, now)
            and not state.trade_lock.locked()
        ):
            async with state.trade_lock:
                if not state.bought and not state.buy_blocked_window_cap:
                    (
                        up_mid,
                        down_mid,
                        up_best_ask,
                        down_best_ask,
                        up_best_ask_age_sec,
                        down_best_ask_age_sec,
                    ) = _market_snapshot_from_ws(window, ws)
                    if not state.snapshot_entry_check_logged:
                        log_event(log, logging.INFO, SIGNAL, {
                            "action": "SNAPSHOT_ENTRY_CHECK",
                            "strategy": strategy.__class__.__name__,
                            "window": window.short_label,
                            "remaining_sec": round(window.end_epoch - time.time()),
                            "up_mid": up_mid,
                            "down_mid": down_mid,
                            "up_best_ask": up_best_ask,
                            "down_best_ask": down_best_ask,
                            "up_best_ask_age_ms": round(up_best_ask_age_sec * 1000) if up_best_ask_age_sec is not None else None,
                            "down_best_ask_age_ms": round(down_best_ask_age_sec * 1000) if down_best_ask_age_sec is not None else None,
                        })
                        state.snapshot_entry_check_logged = True
                    strategy.set_market_snapshot(
                        up_mid=up_mid,
                        down_mid=down_mid,
                        up_best_ask=up_best_ask,
                        down_best_ask=down_best_ask,
                        up_best_ask_age_sec=up_best_ask_age_sec,
                        down_best_ask_age_sec=down_best_ask_age_sec,
                    )
                    await _attempt_strategy_entry(
                        window,
                        state,
                        ws,
                        dry_run,
                        trade_config,
                        strategy,
                        side,
                        up_mid if up_mid is not None else 0.0,
                    )

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
        preopened: If True, skip the generic stale check. Snapshot-entry
            strategies still skip windows whose entry band has already elapsed.
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
        if getattr(strategy, "dynamic_side", False):
            side = "up"
        else:
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
    _raise_if_fatal(state)
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
    state.latest_midpoint_received_at = None
    state.target_side = None
    state.target_entry_price = None
    state.target_max_entry_price = None
    state.target_signal_strength = None
    state.target_past_signal_strength = None
    state.target_active_theta_pct = None
    state.target_remaining_sec = None
    state.signal_reference_price = None
    state.entry_avg_price = 0.0
    state.stop_loss_triggered = False
    state.stop_loss_attempted = False
    state.stop_loss_price = None
    state.last_stop_loss_check_key = None
    state.last_stop_loss_check_logged_at = 0.0
    state.stop_loss_check_logged_reasons.clear()
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
    state.entry_replay_check_count = 0
    state.entry_replay_signal_count = 0
    state.entry_replay_buy_signal_count = 0
    state.entry_replay_min_leading_ask = None
    state.entry_replay_max_leading_ask = None
    state.entry_replay_min_best_ask = None
    state.entry_replay_max_best_ask = None
    state.entry_replay_min_selected_ask = None
    state.entry_replay_max_selected_ask = None
    state.entry_replay_max_depth_notional = 0.0
    state.entry_replay_min_best_ask_age_ms = None
    state.entry_replay_max_best_ask_age_ms = None
    state.stop_replay_check_count = 0
    state.stop_replay_triggered_count = 0
    state.stop_replay_missing_or_stale_bid_count = 0
    state.stop_replay_insufficient_depth_count = 0
    state.stop_replay_min_best_bid = None
    state.stop_replay_max_best_bid = None
    state.stop_replay_min_selected_bid = None
    state.stop_replay_max_selected_bid = None
    state.stop_replay_max_bid_shares_available = 0.0
    state.stop_replay_min_best_bid_age_ms = None
    state.stop_replay_max_best_bid_age_ms = None
    state.entry_amount = 0.0
    state.last_entry_check_side = None
    state.last_entry_check_best_ask = None
    state.snapshot_entry_check_logged = False
    state.started = False

    # Check daily reset and risk management before monitoring this window
    _check_and_reset_daily_state(state)
    if _should_skip_window(state):
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, existing_ws, False

    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch

    # Skip stale windows before subscribing. A preopened crowd snapshot window
    # can become stale if next-window discovery or WS switching stalls.
    skip_threshold, skip_reason = _strategy_attach_skip_threshold(strategy, window)
    if elapsed_since_start > skip_threshold and (not preopened or _is_crowd_m1_strategy(strategy)):
        log_event(log, logging.INFO, WINDOW, {
            "action": "SKIP",
            "window": window.short_label,
            "elapsed": elapsed_since_start,
            "reason": skip_reason,
            "preopened": preopened,
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
    _raise_if_fatal(state)

    # Pre-fetch order params during wait time to reduce order placement delay.
    from polybot.core.client import prefetch_order_params
    for tid in token_ids:
        await asyncio.to_thread(prefetch_order_params, tid)

    # Wait for window start if not yet started
    if elapsed_since_start < 0:
        wait_sec = window.start_epoch - now_epoch
        log.debug("Waiting %ds for window to start... (WS pre-connected)", wait_sec)
        await asyncio.sleep(wait_sec)
        _raise_if_fatal(state)

    # Re-check after WS subscribe/prefetch/wait. Network stalls can consume the
    # narrow snapshot entry band even when the window was fresh on initial attach.
    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch
    if elapsed_since_start > skip_threshold:
        log_event(log, logging.INFO, WINDOW, {
            "action": "SKIP",
            "window": window.short_label,
            "elapsed": elapsed_since_start,
            "reason": skip_reason,
            "preopened": preopened,
            "phase": "post_connect",
        })
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, ws, False

    # Window is now live — enable trading
    state.started = True

    # Notify strategy of window start so it can initialize window state.
    strategy.set_window_start(window.start_epoch)

    # Seed BTC open price via REST if WS feed has no mid-window coverage.
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
                    max_entry_level=entry_ask_level,
                    low_price_threshold=trade_config.low_price_threshold,
                    low_price_entry_level=trade_config.low_price_entry_ask_level,
                    dynamic_entry_levels=trade_config.dynamic_entry_levels,
                    max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
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
                            ws=ws,
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

    if not state.bought and not _entry_update_allowed(strategy, window, update):
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
            (
                up_mid,
                down_mid,
                up_best_ask,
                down_best_ask,
                up_best_ask_age_sec,
                down_best_ask_age_sec,
            ) = _market_snapshot_from_ws(window, ws, update)
            strategy.set_market_snapshot(
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                up_best_ask_age_sec=up_best_ask_age_sec,
                down_best_ask_age_sec=down_best_ask_age_sec,
            )
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
                entry_signal_price = _effective_signal_price(strategy, state, price)
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
                    max_entry_level=entry_ask_level,
                    low_price_threshold=trade_config.low_price_threshold,
                    low_price_entry_level=trade_config.low_price_entry_ask_level,
                    dynamic_entry_levels=trade_config.dynamic_entry_levels,
                    max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
                )
                if not quote.enough:
                    state.target_entry_price = None
                    _log_depth_skip(
                        state,
                        effective_side,
                        entry_signal_price,
                        quote,
                        max_entry_price,
                        trade_amount,
                        "cap-limited book depth insufficient",
                    )
                    return
                _log_signal_eval(
                    state,
                    effective_side,
                    entry_signal_price,
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
                    "signal_price": entry_signal_price,
                    "side": effective_side.upper(),
                    "window": window.short_label,
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
                    "active_theta_pct": (
                        round(state.target_active_theta_pct, 4)
                        if state.target_active_theta_pct is not None
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
                    window, state, buy_token_id, entry_signal_price, dry_run, trade_config, strategy, effective_side,
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
                    ws=ws,
                )
            return

        is_held_token_update = update.token_id == buy_token_id
        if is_held_token_update:
            state.latest_midpoint = price
            state.latest_midpoint_received_at = time.time()
            stop_timing, stop_remaining = _stop_loss_remaining_state(window, trade_config)
            if stop_timing == "before":
                return
            if stop_timing == "prewarm":
                _log_stop_loss_book_freshness(
                    state,
                    side=effective_side,
                    window=window,
                    remaining=stop_remaining,
                    phase="prewarm",
                    best_bid_age_ms=_best_bid_age_ms(ws, buy_token_id),
                )
                return
            if stop_timing == "after":
                return
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
    ws: Optional[PriceStream] = None,
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
            "active_theta_pct": (
                round(state.target_active_theta_pct, 4)
                if state.target_active_theta_pct is not None
                else None
            ),
            "remaining_sec": (
                round(state.target_remaining_sec)
                if state.target_remaining_sec is not None
                else None
            ),
            "best_ask_age_ms": round(best_ask_age_sec * 1000) if best_ask_age_sec is not None else None,
        })
        result = await place_fak_buy(
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
            asyncio.create_task(_sync_holding_balance_after_buy(
                state,
                buy_token_id,
                window,
                side,
                state.entry_count,
            ))
            if strategy is not None:
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
                _mark_fatal_error(state, result.message)
                log_event(log, logging.CRITICAL, TRADE, {
                    "action": "STOP_INSUFFICIENT_FUNDS",
                    "window": window.short_label,
                    "message": result.message,
                })
                raise RuntimeError(result.message)
    else:
        dry_buy_price = target_entry_ask if target_entry_ask is not None else buy_price
        max_entry_price = _entry_price_cap(strategy, state)
        if max_entry_price is not None and dry_buy_price > max_entry_price:
            state.exit_triggered = True
            state.buy_blocked_window_cap = True
            log_event(log, logging.WARNING, TRADE, {
                "action": "BUY_FAILED",
                "side": side.upper(),
                "price": buy_price,
                "amount": trade_amount,
                "message": "dry-run depth quote above cap",
                "window": window.short_label,
                "dry_run": True,
                "dry_run_pricing": "depth_quote",
                "target_entry_ask": target_entry_ask,
                "max_entry_price": max_entry_price,
            })
            state.target_entry_price = None
            state.target_side = None
            return
        buy_price = dry_buy_price
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
            "dry_run_pricing": "depth_quote",
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
            "active_theta_pct": (
                round(state.target_active_theta_pct, 4)
                if state.target_active_theta_pct is not None
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
            "avg_price": buy_price,
            "dry_run_pricing": "depth_quote",
            "dry_run": True,
        })
        if strategy is not None:
            strategy.on_buy_confirmed(time.time())
    state.target_entry_price = None
