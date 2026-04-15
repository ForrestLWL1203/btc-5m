"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
import functools
import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import config
from .client import get_midpoint_async
from .log_formatter import (
    MARKET,
    SIGNAL,
    TRADE,
    WINDOW,
    log_event,
)
from .market import (
    MarketWindow,
    find_next_window,
    find_window_after,
)
from .stream import PriceStream, PriceUpdate
from .trading import buy_token, cancel_all_open_orders, sell_token

log = logging.getLogger(__name__)


def _side_token(window: MarketWindow) -> tuple[str, str]:
    """Return (buy_token, price_token) based on BUY_SIDE config."""
    if config.BUY_SIDE == "down":
        return window.down_token, window.down_token
    return window.up_token, window.up_token


@dataclass
class MonitorState:
    """Mutable state shared between callbacks and the main loop."""

    bought: bool = False
    holding_size: float = 0.0  # shares held
    entry_price: float = 0.0
    exit_triggered: bool = False
    tp_count: int = 0      # take-profit exits this window
    stop_loss_count: int = 0  # stop-loss exits this window
    latest_midpoint: Optional[float] = None
    buy_blocked_sl: bool = False  # permanently blocked for this window due to stop-loss count exceeded
    buy_blocked_tp: bool = False  # permanently blocked for this window due to take-profit count exceeded
    trade_lock: asyncio.Lock = None  # prevents concurrent buy/sell from WS callbacks
    started: bool = False  # set True when window officially starts — prevents pre-start trades

    def __post_init__(self):
        self.trade_lock = asyncio.Lock()


async def _monitor_single_window(
    window: MarketWindow,
    state: MonitorState,
    ws: Optional[PriceStream],
    dry_run: bool,
) -> Optional[MarketWindow]:
    """
    Monitor a single window until expiry or exit_triggered, then clean up.
    """
    buy_token_id, _ = _side_token(window)
    effective_end = window.end_epoch - config.WINDOW_END_BUFFER
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
                    for sell_attempt in range(3):
                        result = await sell_token(
                            buy_token_id, state.holding_size, "Window expired",
                            window_end_epoch=window.end_epoch,
                        )
                        if result.success:
                            break
                        log_event(log, logging.ERROR, TRADE, {
                            "action": "SELL_FAILED",
                            "reason": f"Window expired sell attempt {sell_attempt + 1}/3",
                            "message": result.message,
                            "window": window.short_label,
                        })
                        if sell_attempt < 2:
                            await asyncio.sleep(0.5)
            # Always pre-fetch next window on expiry to avoid stale fallback
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch)
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
                asyncio.to_thread(_find_next_window_after, window.end_epoch)
            )
            await cancel_all_open_orders()
            if not dry_run:
                for sell_attempt in range(3):
                    result = await sell_token(
                        buy_token_id, state.holding_size, f"Window ending in {remaining}s",
                        window_end_epoch=window.end_epoch,
                    )
                    if result.success:
                        break
                    log_event(log, logging.ERROR, TRADE, {
                        "action": "SELL_FAILED",
                        "reason": f"Window end sell attempt {sell_attempt + 1}/3",
                        "message": result.message,
                        "window": window.short_label,
                    })
                    if sell_attempt < 2:
                        await asyncio.sleep(0.5)
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
                asyncio.to_thread(_find_next_window_after, window.end_epoch)
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


def _find_next_window_after(after_epoch: int) -> Optional[MarketWindow]:
    """Find the next window after the given epoch (delegates to market.find_window_after)."""
    return find_window_after(after_epoch)


def _find_and_preopen_next_window(
    current_window: MarketWindow,
) -> Optional[MarketWindow]:
    """
    Find the window that starts after current_window.end_epoch and return it.
    """
    next_win = _find_next_window_after(current_window.end_epoch)
    if next_win is None:
        log_event(log, logging.WARNING, WINDOW, {
            "action": "NOT_FOUND",
            "message": f"No next window after {current_window.short_label}",
        })
        return None

    PREOPEN_BUFFER = 10  # seconds before window start to wake up
    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - PREOPEN_BUFFER

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
) -> tuple[Optional[MarketWindow], Optional[PriceStream]]:
    """
    Monitor a 5-minute window using WebSocket real-time price updates.

    Args:
        window: The window to monitor.
        dry_run: If True, log actions but don't place orders.
        preopened: If True, skip the stale check.
        existing_ws: Reuse this WS connection instead of creating a new one.

    Returns (next_window, ws) — pass ws to the next call's existing_ws param.
    """
    state = MonitorState()
    ws: Optional[PriceStream] = existing_ws

    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch

    # Skip windows that started more than 5 seconds ago — pre-open next immediately
    if not preopened and elapsed_since_start > 5:
        log_event(log, logging.INFO, WINDOW, {
            "action": "SKIP",
            "window": window.short_label,
            "elapsed": elapsed_since_start,
            "reason": "started >5s ago",
        })
        next_win = _find_and_preopen_next_window(window)
        return next_win, ws

    buy_token_id, price_token_id = _side_token(window)
    token_ids = [window.up_token, window.down_token]
    new_callback = functools.partial(
        _on_price_update, window=window, state=state, dry_run=dry_run,
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
        "side": config.BUY_SIDE.upper(),
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
            await _handle_opening_price(window, state, buy_token_id, opening_price, dry_run)
    else:
        log_event(log, logging.WARNING, SIGNAL, {
            "action": "OPENING_PRICE_MISSING",
            "window": window.short_label,
        })

    # Monitor until window expires or exit triggered (ws is NOT closed inside)
    next_win = await _monitor_single_window(window, state, ws, dry_run)

    if next_win is not None:
        return next_win, ws

    # Fallback: window ended without pre-fetch
    next_win = find_next_window()
    if next_win is None:
        log_event(log, logging.WARNING, MARKET, {
            "action": "NOT_FOUND",
            "message": "No next window found",
        })
        return None, ws

    PREOPEN_BUFFER = 10
    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - PREOPEN_BUFFER

    if now_epoch < wake_epoch:
        log.debug(
            "Pre-open: sleeping %ds until %s starts at %s",
            wake_epoch - now_epoch, next_win.short_label, next_win.start_time,
        )
        await asyncio.sleep(wake_epoch - now_epoch)

    return next_win, ws


async def _on_price_update(
    update: PriceUpdate,
    window: MarketWindow,
    state: MonitorState,
    dry_run: bool,
) -> None:
    """
    Called by PriceStream whenever a price update arrives.
    Triggers buy / stop-loss / take-profit immediately on signal.
    """
    buy_token_id, price_token_id = _side_token(window)

    if update.token_id != price_token_id:
        return

    price = update.midpoint
    if price is None:
        return

    if not state.started:
        return

    state.latest_midpoint = price

    sl_tp_price = price if update.is_trade else None
    log.debug(
        "WS price update | %s: %s (source=%s, sl_tp_price=%s)",
        config.BUY_SIDE.upper(), price, update.source, sl_tp_price,
    )

    if state.buy_blocked_sl or state.buy_blocked_tp:
        return

    if state.trade_lock.locked():
        return

    async with state.trade_lock:
        if state.buy_blocked_sl or state.buy_blocked_tp:
            return

        # Price in buy range: allow buy or re-buy if within re-entry limits
        if not state.bought:
            if state.tp_count > config.MAX_TP_REENTRY:
                log_event(log, logging.WARNING, SIGNAL, {
                    "action": "BLOCKED_TP",
                    "window": window.short_label,
                    "tp_count": state.tp_count,
                    "max_tp": config.MAX_TP_REENTRY,
                })
                state.buy_blocked_tp = True
                return
            max_reentry = config.MAX_STOP_LOSS_REENTRY
            if state.stop_loss_count > max_reentry:
                log_event(log, logging.WARNING, SIGNAL, {
                    "action": "BLOCKED_SL",
                    "window": window.short_label,
                    "sl_count": state.stop_loss_count,
                    "max_sl": max_reentry,
                })
                state.buy_blocked_sl = True
                return
            if config.BUY_THRESHOLD_LOW < price < config.BUY_THRESHOLD_HIGH:
                log_event(log, logging.INFO, SIGNAL, {
                    "action": "BUY_RANGE",
                    "price": price,
                    "side": config.BUY_SIDE.upper(),
                    "window": window.short_label,
                })
                await _handle_opening_price(window, state, buy_token_id, price, dry_run)
            return

        if state.exit_triggered:
            return

        # Already holding — check stop-loss / take-profit
        check_price = sl_tp_price if sl_tp_price is not None else price

        if check_price > config.TAKE_PROFIT:
            state.tp_count += 1
            can_reenter = state.tp_count <= config.MAX_TP_REENTRY
            log_event(log, logging.WARNING, SIGNAL, {
                "action": "TAKE_PROFIT",
                "side": config.BUY_SIDE.upper(),
                "price": check_price,
                "threshold": config.TAKE_PROFIT,
                "source": update.source,
                "reentry": can_reenter,
                "count": f"{state.tp_count}/{config.MAX_TP_REENTRY + 1}",
                "window": window.short_label,
            })
            state.exit_triggered = not can_reenter
            state.bought = False
            await cancel_all_open_orders()
            if not dry_run:
                result = await sell_token(
                    buy_token_id, state.holding_size, f"Take-profit @ {check_price}",
                    window_end_epoch=window.end_epoch,
                )
                if not result.success:
                    log_event(log, logging.ERROR, TRADE, {
                        "action": "SELL_FAILED",
                        "reason": "Take-profit sell failed",
                        "message": result.message,
                        "window": window.short_label,
                    })
                    state.bought = True
            else:
                log_event(log, logging.INFO, TRADE, {
                    "action": "SELL",
                    "reason": "Take-profit",
                    "price": check_price,
                    "shares": state.holding_size,
                    "window": window.short_label,
                    "dry_run": True,
                })
            return

        if check_price < config.STOP_LOSS:
            state.stop_loss_count += 1
            can_reenter = state.stop_loss_count <= config.MAX_STOP_LOSS_REENTRY
            log_event(log, logging.WARNING, SIGNAL, {
                "action": "STOP_LOSS",
                "side": config.BUY_SIDE.upper(),
                "price": check_price,
                "threshold": config.STOP_LOSS,
                "source": update.source,
                "reentry": can_reenter,
                "count": f"{state.stop_loss_count}/{config.MAX_STOP_LOSS_REENTRY + 1}",
                "window": window.short_label,
            })
            state.exit_triggered = not can_reenter
            state.bought = False
            await cancel_all_open_orders()
            if not dry_run:
                result = await sell_token(
                    buy_token_id, state.holding_size, f"Stop-loss @ {check_price}",
                    window_end_epoch=window.end_epoch,
                )
                if not result.success:
                    log_event(log, logging.ERROR, TRADE, {
                        "action": "SELL_FAILED",
                        "reason": "Stop-loss sell failed",
                        "message": result.message,
                        "window": window.short_label,
                    })
                    state.bought = True
            else:
                log_event(log, logging.INFO, TRADE, {
                    "action": "SELL",
                    "reason": "Stop-loss",
                    "price": check_price,
                    "shares": state.holding_size,
                    "window": window.short_label,
                    "dry_run": True,
                })
            return


async def _handle_opening_price(
    window: MarketWindow,
    state: MonitorState,
    buy_token_id: str,
    price: float,
    dry_run: bool,
) -> None:
    """Handle the opening price check and buy decision."""
    if state.bought:
        return

    if state.exit_triggered:
        state.exit_triggered = False

    in_range = config.BUY_THRESHOLD_LOW < price < config.BUY_THRESHOLD_HIGH

    if in_range:
        if not dry_run:
            state.bought = True
            result = await buy_token(
                buy_token_id, config.BUY_AMOUNT, window.short_label,
                window_end_epoch=window.end_epoch,
            )
            if result.success:
                if result.filled_size > 0:
                    state.holding_size = result.filled_size
                else:
                    state.holding_size = config.BUY_AMOUNT / price if price > 0 else config.BUY_AMOUNT
                state.entry_price = price
                log_event(log, logging.INFO, TRADE, {
                    "action": "BUY_FILLED",
                    "side": config.BUY_SIDE.upper(),
                    "price": price,
                    "amount": config.BUY_AMOUNT,
                    "shares": state.holding_size,
                    "window": window.short_label,
                })
            else:
                state.bought = False
                log_event(log, logging.WARNING, TRADE, {
                    "action": "BUY_FAILED",
                    "side": config.BUY_SIDE.upper(),
                    "price": price,
                    "message": result.message,
                    "window": window.short_label,
                })
        else:
            state.bought = True
            state.holding_size = config.BUY_AMOUNT / price if price > 0 else config.BUY_AMOUNT
            state.entry_price = price
            log_event(log, logging.INFO, TRADE, {
                "action": "BUY",
                "side": config.BUY_SIDE.upper(),
                "price": price,
                "amount": config.BUY_AMOUNT,
                "shares": state.holding_size,
                "window": window.short_label,
                "dry_run": True,
            })
    else:
        log_event(log, logging.INFO, SIGNAL, {
            "action": "SKIP",
            "price": price,
            "side": config.BUY_SIDE.upper(),
            "range_low": config.BUY_THRESHOLD_LOW,
            "range_high": config.BUY_THRESHOLD_HIGH,
            "window": window.short_label,
        })
