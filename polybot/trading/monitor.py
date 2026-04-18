"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
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
from polybot.strategies.immediate import FixedSideStrategy
from polybot.trade_config import ExitReason, TradeConfig
from .trading import buy_token, cancel_all_open_orders, sell_token

log = logging.getLogger(__name__)

_PREOPEN_BUFFER = 10  # seconds before window start to wake up


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
        result = await sell_token(
            token_id, sell_size, reason,
            window_end_epoch=window_end_epoch,
        )
        if result.success:
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
    buy_token_id, _ = _side_token(window, side)
    buffer = series.window_end_buffer if series else config.WINDOW_END_BUFFER
    effective_end = window.end_epoch - buffer
    fetch_task = None

    while True:
        now = int(time.time())
        if now >= effective_end:
            log_event(log, logging.INFO, WINDOW, {
                "action": "EXPIRED",
                "window": window.short_label,
                "holding": state.bought,
            })
            if state.bought and not state.exit_triggered:
                log_event(log, logging.WARNING, TRADE, {
                    "action": "SELL",
                    "reason": "Window expired with open position",
                    "window": window.short_label,
                    "shares": state.holding_size,
                    "dry_run": dry_run,
                })
                await cancel_all_open_orders()
                if not dry_run:
                    await _sell_with_retry(
                        buy_token_id, state.holding_size, "Window expired",
                        window_end_epoch=window.end_epoch,
                    )
            # Always pre-fetch next window on expiry to avoid stale fallback
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
            )
            break

        remaining = effective_end - now
        # Window ending soon (<=10s) with position open and no exit triggered — sell now
        if remaining <= 10 and state.bought and not state.exit_triggered:
            log_event(log, logging.WARNING, TRADE, {
                "action": "SELL",
                "reason": f"Window ending in {remaining}s",
                "window": window.short_label,
                "shares": state.holding_size,
                "dry_run": dry_run,
            })
            # Pre-fetch next window while we sleep
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch, series)
            )
            async with state.trade_lock:
                await cancel_all_open_orders()
                if not dry_run:
                    await _sell_with_retry(
                        buy_token_id, state.holding_size, f"Window ending in {remaining}s",
                        window_end_epoch=window.end_epoch,
                    )
                state.bought = False
                state.exit_triggered = True
            await asyncio.sleep(remaining)
            try:
                next_win = fetch_task.result()
            except Exception as e:
                log.debug("Pre-fetch next window failed: %s", e)
                next_win = find_next_window()
            # Do NOT close ws — reuse across windows
            return next_win

        if state.exit_triggered:
            remaining = effective_end - now
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
                next_win = fetch_task.result()
            except Exception as e:
                log.debug("Pre-fetch next window failed: %s", e)
                next_win = find_next_window()
            # Do NOT close ws — reuse across windows
            return next_win

        await asyncio.sleep(1)

    if fetch_task is not None:
        try:
            next_win = fetch_task.result()
        except Exception as e:
            log.debug("Pre-fetch next window after expiry failed: %s", e)
            next_win = None
        return next_win
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

    return next_win


async def monitor_window(
    window: MarketWindow,
    dry_run: bool = False,
    preopened: bool = False,
    existing_ws: Optional[PriceStream] = None,
    trade_config: Optional[TradeConfig] = None,
    strategy: Optional[Strategy] = None,
    series: Optional[MarketSeries] = None,
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

    Returns (next_window, ws, monitored) — monitored is False if window was skipped.
    Pass ws to the next call's existing_ws param.
    """
    if trade_config is None:
        trade_config = TradeConfig()
    if strategy is None:
        strategy = FixedSideStrategy()

    # Resolve direction — strategy.get_side() runs once per window
    candles = None
    if series is not None:
        from polybot.predict.kline import BinanceKlineFetcher
        fetcher = BinanceKlineFetcher(series)
        candles = await asyncio.to_thread(fetcher.fetch)
    side = strategy.get_side(candles)
    if side is None:
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "DIRECTION_SKIP",
            "window": window.short_label,
            "candles": len(candles) if candles else 0,
            "reason": "strategy returned no side",
        })
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, existing_ws, False
    log_event(log, logging.INFO, SIGNAL, {
        "action": "SIDE_RESOLVED",
        "side": side.upper(),
        "window": window.short_label,
    })

    state = MonitorState()
    ws: Optional[PriceStream] = existing_ws

    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch

    # Skip windows that started too long ago — pre-open next immediately
    step = series.slug_step if series else config.SLUG_STEP
    skip_threshold = max(5, step // 60)
    if not preopened and elapsed_since_start > skip_threshold:
        log_event(log, logging.INFO, WINDOW, {
            "action": "SKIP",
            "window": window.short_label,
            "elapsed": elapsed_since_start,
            "reason": f"started >{skip_threshold}s ago",
        })
        next_win = _find_and_preopen_next_window(window, series)
        return next_win, ws, False

    buy_token_id, price_token_id = _side_token(window, side)
    token_ids = [window.up_token, window.down_token]
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

    # Pre-fetch order params during wait time to reduce buy latency
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

    log_event(log, logging.INFO, WINDOW, {
        "action": "STARTED",
        "window": window.short_label,
        "side": side.upper(),
        "buy_token": buy_token_id[:20],
        "price_token": price_token_id[:20],
    })

    # Price should already be cached from WS pre-connection
    opening_price = ws.get_latest_price(price_token_id)
    if opening_price is None:
        opening_price = await get_midpoint_async(price_token_id)

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
            await _handle_opening_price(
                window, state, buy_token_id, opening_price, dry_run, trade_config, strategy, side,
            )
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
    next_win = find_next_window()
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


async def _check_sl_tp(
    update: PriceUpdate,
    state: MonitorState,
    window: MarketWindow,
    buy_token_id: str,
    dry_run: bool,
    trade_config: TradeConfig,
    side: str = "up",
) -> bool:
    """
    Check SL/TP thresholds using optimistic/pessimistic price signals.
    Returns True if an SL/TP exit was triggered.
    Must be called while trade_lock is held.
    """
    if state.exit_triggered or not state.bought:
        return False

    fresh_trade = state.get_fresh_trade_price()
    price = update.midpoint

    # TP: use most optimistic signal — any single price above threshold triggers
    tp_price = max(
        price if price is not None else float('-inf'),
        fresh_trade if fresh_trade is not None else float('-inf'),
        update.best_ask if update.best_ask is not None else float('-inf'),
    )
    # SL: use most pessimistic signal — any single price below threshold triggers
    sl_price = min(
        price if price is not None else float('inf'),
        fresh_trade if fresh_trade is not None else float('inf'),
        update.best_bid if update.best_bid is not None else float('inf'),
    )

    signal = trade_config.check_exit(tp_price, sl_price, state)
    if signal is None:
        return False

    if signal.reason == ExitReason.TAKE_PROFIT:
        state.tp_count += 1
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "TAKE_PROFIT",
            "side": side.upper(),
            "price": tp_price,
            "threshold": signal.threshold,
            "source": update.source,
            "fresh_trade": fresh_trade,
            "reentry": signal.can_reenter,
            "count": f"{state.tp_count}/{trade_config.max_tp_reentry + 1}",
            "window": window.short_label,
        })
        state.exit_triggered = not signal.can_reenter
        state.bought = False
        await cancel_all_open_orders()
        if not dry_run:
            success = await _sell_with_retry(
                buy_token_id, state.holding_size, f"Take-profit @ {tp_price}",
                window_end_epoch=window.end_epoch,
            )
            if not success:
                state.bought = True
        else:
            log_event(log, logging.INFO, TRADE, {
                "action": "SELL",
                "reason": "Take-profit",
                "price": tp_price,
                "shares": state.holding_size,
                "window": window.short_label,
                "dry_run": True,
            })
        return True

    if signal.reason == ExitReason.STOP_LOSS:
        # SL time gate: wait until enough of the window has elapsed
        # 5m: after halfway (2m30s) | 15m: last 5m | 4h: last 1h
        now = time.time()
        remaining = window.end_epoch - now
        total = window.end_epoch - window.start_epoch
        if total <= 300:       # ≤5m window
            sl_allowed_after = total / 2
        elif total <= 900:     # ≤15m window
            sl_allowed_after = total - 300
        else:                  # >15m (4h etc.)
            sl_allowed_after = total - 3600
        if remaining > sl_allowed_after:
            return False
        state.stop_loss_count += 1
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "STOP_LOSS",
            "side": side.upper(),
            "price": sl_price,
            "threshold": signal.threshold,
            "effective_sl_pct": round(signal.effective_sl_pct, 2),
            "source": update.source,
            "fresh_trade": fresh_trade,
            "reentry": signal.can_reenter,
            "count": f"{state.stop_loss_count}/{trade_config.max_sl_reentry + 1}",
            "window": window.short_label,
        })
        state.exit_triggered = not signal.can_reenter
        state.bought = False
        await cancel_all_open_orders()
        if not dry_run:
            success = await _sell_with_retry(
                buy_token_id, state.holding_size, f"Stop-loss @ {sl_price}",
                window_end_epoch=window.end_epoch,
            )
            if not success:
                state.bought = True
        else:
            log_event(log, logging.INFO, TRADE, {
                "action": "SELL",
                "reason": "Stop-loss",
                "price": sl_price,
                "shares": state.holding_size,
                "window": window.short_label,
                "dry_run": True,
            })
        return True

    return False


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
    Triggers buy / stop-loss / take-profit immediately on signal.
    """
    if strategy is None:
        strategy = FixedSideStrategy()

    buy_token_id, price_token_id = _side_token(window, side)

    if update.token_id != price_token_id:
        return

    price = update.midpoint
    if price is None:
        return

    if not state.started:
        return

    state.latest_midpoint = price

    # Track last trade price for more responsive SL/TP
    if update.is_trade:
        state.update_trade_price(price)

    log.debug(
        "WS price update | %s: %s (source=%s, bid=%s, ask=%s)",
        side.upper(), price, update.source,
        update.best_bid, update.best_ask,
    )

    if state.buy_blocked_sl or state.buy_blocked_tp:
        return

    if state.trade_lock.locked():
        # Defer SL/TP signal instead of dropping it
        if state.bought and not state.exit_triggered:
            state._pending_signal = update
        return

    async with state.trade_lock:
        if state.buy_blocked_sl or state.buy_blocked_tp:
            return

        # Not holding: check re-entry limits and buy decision
        if not state.bought:
            if state.tp_count > trade_config.max_tp_reentry:
                log_event(log, logging.WARNING, SIGNAL, {
                    "action": "BLOCKED_TP",
                    "window": window.short_label,
                    "tp_count": state.tp_count,
                    "max_tp": trade_config.max_tp_reentry,
                })
                state.buy_blocked_tp = True
                return
            if state.stop_loss_count > trade_config.max_sl_reentry:
                log_event(log, logging.WARNING, SIGNAL, {
                    "action": "BLOCKED_SL",
                    "window": window.short_label,
                    "sl_count": state.stop_loss_count,
                    "max_sl": trade_config.max_sl_reentry,
                })
                state.buy_blocked_sl = True
                return
            # Re-entry price gates: only re-buy if price meets criteria
            if state.stop_loss_count > 0:
                # SL re-entry: require price recovery near original entry (within 5%)
                if price < state.original_entry_price * 0.95:
                    return
            if state.tp_count > 0 and trade_config.tp_pct:
                # TP re-entry: require pullback (price below half the gain)
                if price > state.original_entry_price * (1 + trade_config.tp_pct * 0.5):
                    return
            if strategy.should_buy(price, state):
                log_event(log, logging.INFO, SIGNAL, {
                    "action": "BUY_SIGNAL",
                    "price": price,
                    "side": side.upper(),
                    "window": window.short_label,
                })
                await _handle_opening_price(
                    window, state, buy_token_id, price, dry_run, trade_config, strategy, side,
                )
            return

        # Already holding — check stop-loss / take-profit
        await _check_sl_tp(update, state, window, buy_token_id, dry_run, trade_config, side)


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
    if strategy is None:
        strategy = FixedSideStrategy()

    if state.bought:
        return

    if state.exit_triggered:
        state.exit_triggered = False

    if strategy.should_buy(price, state):
        if not dry_run:
            state.bought = True
            result = await buy_token(
                buy_token_id, trade_config.amount, window.short_label,
                window_end_epoch=window.end_epoch,
            )
            if result.success:
                # Query exact balance from API instead of estimating
                from polybot.core.client import get_token_balance
                exact_balance = await asyncio.to_thread(get_token_balance, buy_token_id)
                if exact_balance is not None and exact_balance > 0:
                    state.holding_size = exact_balance
                    log_event(log, logging.DEBUG, TRADE, {
                        "action": "BALANCE_CONFIRMED",
                        "token": buy_token_id[:20],
                        "shares": exact_balance,
                    })
                elif result.filled_size > 0 and result.avg_price > 0:
                    state.holding_size = result.filled_size
                elif result.avg_price > 0:
                    state.holding_size = trade_config.amount / result.avg_price
                else:
                    state.holding_size = trade_config.amount / price if price > 0 else trade_config.amount
                state.entry_price = price
                if state.original_entry_price == 0.0:
                    state.original_entry_price = price
                log_event(log, logging.INFO, TRADE, {
                    "action": "BUY_FILLED",
                    "side": side.upper(),
                    "price": price,
                    "amount": trade_config.amount,
                    "shares": state.holding_size,
                    "window": window.short_label,
                })
                # Discard deferred signal — it's from before this buy,
                # price context is stale for the new position
                if state._pending_signal is not None:
                    log_event(log, logging.DEBUG, SIGNAL, {
                        "action": "DEFERRED_SIGNAL_DISCARDED",
                        "window": window.short_label,
                        "deferred_source": state._pending_signal.source,
                        "deferred_midpoint": state._pending_signal.midpoint,
                    })
                    state._pending_signal = None
            else:
                state.bought = False
                state.exit_triggered = True
                log_event(log, logging.WARNING, TRADE, {
                    "action": "BUY_FAILED",
                    "side": side.upper(),
                    "price": price,
                    "message": result.message,
                    "window": window.short_label,
                })
        else:
            state.bought = True
            state.holding_size = trade_config.amount / price if price > 0 else trade_config.amount
            state.entry_price = price
            if state.original_entry_price == 0.0:
                state.original_entry_price = price
            log_event(log, logging.INFO, TRADE, {
                "action": "BUY",
                "side": side.upper(),
                "price": price,
                "amount": trade_config.amount,
                "shares": state.holding_size,
                "window": window.short_label,
                "dry_run": True,
            })
            # Discard deferred signal — stale price context for new position
            if state._pending_signal is not None:
                log_event(log, logging.DEBUG, SIGNAL, {
                    "action": "DEFERRED_SIGNAL_DISCARDED",
                    "window": window.short_label,
                    "deferred_source": state._pending_signal.source,
                    "deferred_midpoint": state._pending_signal.midpoint,
                })
                state._pending_signal = None
