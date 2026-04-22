"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
import datetime
import functools
import logging
import time
from typing import Optional

from polybot.core import config
from polybot.core.client import get_midpoint_async
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
from .trading import buy_token

log = logging.getLogger(__name__)

_PREOPEN_BUFFER = 10  # seconds before window start to wake up
_STARTED_SKIP_THRESHOLD = 60  # allow attaching to a window within its first minute


def _get_utc8_date() -> str:
    """Get current date in UTC+8 as YYYY-MM-DD."""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _check_and_reset_daily_state(state: MonitorState) -> None:
    """Reset daily risk management stats at UTC+8 midnight."""
    current_date = _get_utc8_date()
    if state.last_reset_date != current_date:
        log_event(log, logging.INFO, TRADE, {
            "action": "DAILY_RESET",
            "date": current_date,
            "previous_date": state.last_reset_date,
        })
        state.last_reset_date = current_date
        state.daily_wins = 0
        state.daily_losses = 0
        state.consecutive_losses = 0
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


def _process_trade_result(state: MonitorState, direction_correct: bool) -> None:
    """Update daily statistics and check risk management triggers."""
    if direction_correct:
        state.daily_wins += 1
        state.consecutive_losses = 0
    else:
        state.daily_losses += 1
        state.consecutive_losses += 1

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


def _calc_dry_run_pnl(state: MonitorState, exit_price: float) -> tuple[float, float]:
    """Estimate dry-run PnL from entry and exit prices, excluding fees."""
    if state.entry_price <= 0 or state.holding_size <= 0:
        return 0.0, 0.0
    pnl = (exit_price - state.entry_price) * state.holding_size
    pnl_pct = (exit_price / state.entry_price - 1.0) * 100 if state.entry_price > 0 else 0.0
    return pnl, pnl_pct


def _log_dry_run_sell(state: MonitorState, window: MarketWindow, reason: str, price: float) -> None:
    """Log a dry-run exit with estimated per-trade and cumulative PnL."""
    pnl, pnl_pct = _calc_dry_run_pnl(state, price)
    state.realized_pnl += pnl
    log_event(log, logging.INFO, TRADE, {
        "action": "SELL",
        "reason": reason,
        "price": price,
        "shares": state.holding_size,
        "window": window.short_label,
        "dry_run": True,
        "pnl_usd": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 2),
        "cum_pnl_usd": round(state.realized_pnl, 4),
    })


def _log_window_summary(state: MonitorState, window: MarketWindow, dry_run: bool) -> None:
    """Emit a compact end-of-window summary."""
    payload = {
        "action": "SUMMARY",
        "window": window.short_label,
        "entries": state.entry_count,
        "blocked_window_cap": state.buy_blocked_window_cap,
    }
    if dry_run:
        payload["dry_run_realized_pnl_usd"] = round(state.realized_pnl, 4)
    log_event(log, logging.INFO, WINDOW, payload)


async def _get_exact_holding(token_id: str, estimated: float) -> float:
    """Query exact token balance from API; fall back to estimated size."""
    from polybot.core.client import get_token_balance
    exact = await asyncio.to_thread(get_token_balance, token_id)
    if exact is not None and exact > 0:
        log_event(log, logging.INFO, TRADE, {
            "action": "BALANCE_QUERY",
            "token": token_id[:20],
            "exact": exact,
            "estimated": estimated,
        })
        return exact
    # Fallback: truncate estimated balance to be safe
    truncated = int(estimated * 10_000) / 10_000
    if truncated > 0:
        log_event(log, logging.WARNING, TRADE, {
            "action": "BALANCE_FALLBACK",
            "token": token_id[:20],
            "estimated": estimated,
            "truncated": truncated,
        })
        return truncated
    return estimated


async def _sell_with_retry(
    token_id: str, estimated_size: float, reason: str,
    window_end_epoch: Optional[int] = None, max_attempts: int = 3,
) -> bool:
    """Sell with balance-refreshing retry. Re-queries balance on each attempt.

    After successful sell, cleans up residual balance if > dust threshold.
    Returns True if sell succeeded, False if all attempts failed.
    """
    for attempt in range(max_attempts):
        sell_size = await _get_exact_holding(token_id, estimated_size)
        if sell_size <= 0:
            log_event(log, logging.DEBUG, TRADE, {
                "action": "SELL_SKIP",
                "reason": reason,
                "message": "balance is 0",
            })
            return True
        t_sell = time.time()
        result = await sell_token(
            token_id, sell_size, reason,
            window_end_epoch=window_end_epoch,
        )
        if result.success:
            sell_latency_ms = round((time.time() - t_sell) * 1000)
            log_event(log, logging.INFO, TRADE, {
                "action": "SELL_LATENCY",
                "reason": reason,
                "sell_latency_ms": sell_latency_ms,
            })
            await _cleanup_residual(
                token_id, reason, window_end_epoch,
                original_size=sell_size,
            )
            return True
        log_event(log, logging.ERROR, TRADE, {
            "action": "SELL_RETRY",
            "attempt": f"{attempt + 1}/{max_attempts}",
            "reason": reason,
            "sell_size": sell_size,
            "message": result.message,
        })
        if attempt < max_attempts - 1:
            await asyncio.sleep(0.3)
    log_event(log, logging.CRITICAL, TRADE, {
        "action": "SELL_FAILED_ALL",
        "reason": reason,
        "estimated_size": estimated_size,
    })
    return False


_DUST_THRESHOLD = 0.005  # shares — skip cleanup below this (~$0.002)


async def _cleanup_residual(
    token_id: str, reason: str,
    window_end_epoch: Optional[int] = None,
    original_size: float = 0.0,
) -> None:
    """Query balance after sell and clean up any residual dust.

    Uses raw (non-truncated) balance and waits for chain settlement.
    Skips cleanup if balance hasn't settled yet (still ≈ original_size).
    """
    from polybot.core.client import get_token_balance

    # Wait longer for chain settlement (1s is often not enough)
    for delay in (3.0, 5.0):
        await asyncio.sleep(delay)
        residual = await asyncio.to_thread(get_token_balance, token_id, False)
        if residual is None or residual < _DUST_THRESHOLD:
            if residual is not None and residual > 0:
                log_event(log, logging.INFO, TRADE, {
                    "action": "CLEANUP_DUST",
                    "token": token_id[:20],
                    "residual": residual,
                    "note": "below dust threshold, skipping",
                })
            return
        # Balance ≈ original holding → first sell hasn't settled yet, skip
        if original_size > 0 and residual >= original_size * 0.8:
            log_event(log, logging.INFO, TRADE, {
                "action": "CLEANUP_SKIP",
                "token": token_id[:20],
                "residual": residual,
                "original_size": original_size,
                "note": "balance not settled yet, likely stale",
            })
            continue
        # Genuine residual detected
        break
    else:
        # All attempts showed balance ≈ original → nothing to clean
        return

    log_event(log, logging.INFO, TRADE, {
        "action": "CLEANUP_RESIDUAL",
        "token": token_id[:20],
        "residual": residual,
    })
    result = await sell_token(
        token_id, residual, f"Cleanup residual ({reason})",
        window_end_epoch=window_end_epoch,
    )
    if not result.success:
        log_event(log, logging.WARNING, TRADE, {
            "action": "CLEANUP_FAILED",
            "residual": residual,
            "message": result.message,
        })


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
) -> Optional[MarketWindow]:
    """
    Monitor a single window until expiry or exit_triggered, then clean up.
    """
    # Check daily reset at start of each window
    _check_and_reset_daily_state(state)

    buy_token_id, _ = _side_token(window, side)
    fetch_task = None
    window_end_epoch = None

    while True:
        now = int(time.time())
        if now >= window.end_epoch:
            if window_end_epoch is None:
                window_end_epoch = now
            log_event(log, logging.INFO, WINDOW, {
                "action": "EXPIRED",
                "window": window.short_label,
                "holding": state.bought,
            })
            if state.bought and not state.exit_triggered:
                # Post-window-end phase: record trade result and await auto-redeem
                # Polymarket has auto-redeem enabled, so position will be automatically
                # redeemed and funds returned to account. No manual sell needed.
                token_price = state.latest_midpoint
                direction_correct = token_price is not None and token_price > 0.5

                # Log position at window end
                log_event(log, logging.INFO, TRADE, {
                    "action": "WINDOW_END_POSITION",
                    "window": window.short_label,
                    "token_price": token_price,
                    "direction_correct": direction_correct,
                    "shares": state.holding_size,
                    "seconds_since_window_end": now - window.end_epoch,
                    "daily_record": f"{state.daily_wins}W {state.daily_losses}L",
                    "dry_run": dry_run,
                    "note": "awaiting auto-redeem (no manual sell)",
                })

                # Process trade result for risk management
                _process_trade_result(state, direction_correct)

                # Record trade resolution
                log_event(log, logging.INFO, TRADE, {
                    "action": "TRADE_RESOLVED",
                    "window": window.short_label,
                    "result": "WIN" if direction_correct else "LOSS",
                    "shares": state.holding_size,
                    "price": token_price,
                    "note": "Position held to window end, auto-redeem in progress",
                })

                state.exit_triggered = True

            # All positions resolved, pre-fetch next window
            if fetch_task is None:
                fetch_task = asyncio.create_task(
                    asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
                )
            break

        if state.exit_triggered:
            remaining = window.end_epoch - now
            log_event(log, logging.INFO, WINDOW, {
                "action": "EXIT_WAIT",
                "window": window.short_label,
                "sleep_seconds": remaining,
            })
            # Pre-fetch next window while we sleep
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
            )
            await asyncio.sleep(remaining)
            try:
                next_win = _sanitize_next_window(window, fetch_task.result())
            except Exception as e:
                log.debug("Pre-fetch next window failed: %s", e)
                next_win = _sanitize_next_window(window, find_next_window())
            # Do NOT close ws — reuse across windows
            _log_window_summary(state, window, dry_run)
            return next_win

        await asyncio.sleep(1)

    if fetch_task is not None:
        try:
            next_win = _sanitize_next_window(window, fetch_task.result())
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
) -> tuple[Optional[MarketWindow], Optional[PriceStream], bool]:
    """
    Monitor a trading window using WebSocket real-time price updates.

    Args:
        window: The window to monitor.
        dry_run: If True, log actions but don't place orders.
        preopened: If True, skip the stale check.
        existing_ws: Reuse this WS connection instead of creating a new one.
        trade_config: Common trading parameters (TP/SL, amount, etc).
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
    log_event(log, logging.INFO, SIGNAL, {
        "action": "SIDE_RESOLVED",
        "side": side.upper(),
        "window": window.short_label,
    })

    # Use shared state if provided, otherwise create new (which won't persist)
    if state is None:
        state = MonitorState()
    ws: Optional[PriceStream] = existing_ws

    # Reset per-window state for new window
    # (risk management state like daily_wins persists across windows)
    state.bought = False
    state.exit_triggered = False
    state.buy_blocked_window_cap = False
    state.entry_count = 0
    state.entry_timestamps = []

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
    new_callback = functools.partial(
        _on_price_update, window=window, state=state, dry_run=dry_run,
        trade_config=trade_config, strategy=strategy, side=side,
    )

    if ws is not None:
        # Reuse existing WS — switch subscription to new window's tokens
        ws.set_on_price(new_callback)
        await ws.switch_tokens(token_ids)
        log_event(log, logging.INFO, WINDOW, {
            "action": "WS_SWITCHED",
            "window": window.short_label,
        })
    else:
        # First window — create new WS connection
        ws = PriceStream(on_price=new_callback)
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

    log_event(log, logging.INFO, WINDOW, {
        "action": "STARTED",
        "window": window.short_label,
        "side": side.upper(),
        "buy_token": buy_token_id[:20],
        "price_token": price_token_id[:20],
    })

    # Price should already be cached from WS pre-connection
    # Use UP token price as the reference entry signal input.
    opening_token = window.up_token
    opening_price = ws.get_latest_price(opening_token)
    if opening_price is None:
        opening_price = await get_midpoint_async(opening_token)

    if opening_price is not None:
        if state.bought:
            log_event(log, logging.INFO, SIGNAL, {
                "action": "OPENING_PRICE",
                "price": opening_price,
                "window": window.short_label,
                "note": "already bought via WS",
            })
        else:
            log_event(log, logging.INFO, SIGNAL, {
                "action": "OPENING_PRICE",
                "price": opening_price,
                "window": window.short_label,
            })
            if strategy.should_buy(opening_price, state):
                await _handle_opening_price(
                    window, state, buy_token_id, opening_price, dry_run, trade_config, strategy, side,
                )
                # Re-resolve token if strategy set target_side during opening buy
                if state.target_side is not None:
                    buy_token_id, price_token_id = _side_token(window, state.target_side)
    else:
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "OPENING_PRICE_MISSING",
            "window": window.short_label,
        })

    # Monitor until window expires or exit triggered (ws is NOT closed inside)
    next_win = await _monitor_single_window(
        window, state, ws, dry_run, trade_config, strategy, series, side,
    )

    if next_win is not None:
        return next_win, ws, True

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
                log_event(log, logging.INFO, SIGNAL, {
                    "action": "BUY_SIGNAL",
                    "price": price,
                    "side": effective_side.upper(),
                    "window": window.short_label,
                })
                await _handle_opening_price(
                    window, state, buy_token_id, price, dry_run, trade_config, strategy, effective_side,
                )
            return

        is_held_token_update = update.token_id == buy_token_id
        if is_held_token_update:
            state.latest_midpoint = price
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
) -> None:
    """Handle the opening price check and buy decision."""
    if state.bought:
        return

    # Do NOT reset exit_triggered here — it prevents re-entry after buy failure
    # exit_triggered should only be reset at the start of a new window
    if state.exit_triggered:
        return  # Previous buy attempt failed, skip further attempts this window

    # Re-resolve token if strategy overrode direction
    if state.target_side is not None:
        buy_token_id, _ = _side_token(window, state.target_side)
    buy_price = state.target_entry_price if state.target_entry_price is not None else price
    if not dry_run:
        state.bought = True
        t_signal = time.time()
        result = await buy_token(
            buy_token_id, trade_config.amount, window.short_label,
            window_end_epoch=window.end_epoch,
        )
        if result.success:
            entry_latency_ms = round((time.time() - t_signal) * 1000)
            state.entry_count += 1
            state.entry_timestamps.append(time.time())
            if result.filled_size > 0 and result.avg_price > 0:
                state.holding_size = result.filled_size
            elif result.avg_price > 0:
                state.holding_size = trade_config.amount / result.avg_price
            else:
                state.holding_size = trade_config.amount / buy_price if buy_price > 0 else trade_config.amount
            state.entry_price = buy_price
            log_event(log, logging.INFO, TRADE, {
                "action": "BUY_FILLED",
                "side": side.upper(),
                "price": buy_price,
                "amount": trade_config.amount,
                "shares": state.holding_size,
                "window": window.short_label,
                "entry_latency_ms": entry_latency_ms,
            })
            if strategy is not None and hasattr(strategy, "on_buy_confirmed"):
                strategy.on_buy_confirmed(time.time())
        else:
            state.bought = False
            state.exit_triggered = True
            log_event(log, logging.WARNING, TRADE, {
                "action": "BUY_FAILED",
                "side": side.upper(),
                "price": buy_price,
                "message": result.message,
                "window": window.short_label,
            })
    else:
        state.bought = True
        state.entry_count += 1
        state.entry_timestamps.append(time.time())
        state.holding_size = trade_config.amount / buy_price if buy_price > 0 else trade_config.amount
        state.entry_price = buy_price
        log_event(log, logging.INFO, TRADE, {
            "action": "BUY",
            "side": side.upper(),
            "price": buy_price,
            "amount": trade_config.amount,
            "shares": state.holding_size,
            "window": window.short_label,
            "dry_run": True,
        })
        if strategy is not None and hasattr(strategy, "on_buy_confirmed"):
            strategy.on_buy_confirmed(time.time())
    state.target_entry_price = None
