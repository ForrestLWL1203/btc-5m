"""Monitoring loop — real-time monitoring via WebSocket, with fallback to REST polling."""

import asyncio
import datetime
import functools
import logging
import time
from dataclasses import dataclass
from typing import Optional

from . import config
from .client import get_midpoint_async
from .market import (
    MarketWindow,
    find_next_window,
    find_window_after,
)
from .stream import PriceStream, PriceUpdate
from .trading import buy_token, cancel_all_open_orders, sell_token

log = logging.getLogger(__name__)


def _side_token(window: MarketWindow) -> tuple[str, str]:
    """Return (buy_token, price_token) based on BUY_SIDE config.

    price_token is the token whose price we monitor for all trading signals.
    buy_token is the token we actually purchase.
    For UP side: both are window.up_token.
    For DOWN side: both are window.down_token.
    """
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


async def _monitor_single_window(
    window: MarketWindow,
    state: MonitorState,
    ws: Optional[PriceStream],
    dry_run: bool,
) -> Optional[MarketWindow]:
    """
    Monitor a single window until expiry or exit_triggered, then clean up.

    Returns the pre-fetched next window if one was retrieved during sleep,
    so the caller can start monitoring it immediately without waiting.
    """
    buy_token_id, _ = _side_token(window)

    while True:
        now = int(time.time())
        if now >= window.end_epoch:
            log.info("Window %s expired.", window.short_label)
            if state.bought and not state.exit_triggered:
                log.warning("Position still open at window expiry — cancelling orders and selling...")
                cancel_all_open_orders()
                if not dry_run:
                    result = await sell_token(
                buy_token_id, state.holding_size, "Window expired",
                window_end_epoch=window.end_epoch,
            )
                    if not result.success:
                        log.error("Sell at expiry FAILED: %s", result.message)
                else:
                    log.info("[DRY-RUN] Would SELL at expiry")
            break

        remaining = window.end_epoch - now
        # Window ending soon (<=10s) with position open and no exit triggered — sell now
        if remaining <= 10 and state.bought and not state.exit_triggered:
            log.info(
                "Window ending in %ds with position open — selling all shares.",
                remaining,
            )
            # Pre-fetch next window while we sleep
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch)
            )
            cancel_all_open_orders()
            if not dry_run:
                result = await sell_token(
                    buy_token_id, state.holding_size, f"Window ending in {remaining}s",
                    window_end_epoch=window.end_epoch,
                )
                if not result.success:
                    log.error("Sell at window end FAILED: %s", result.message)
            else:
                log.info("[DRY-RUN] Would SELL (window ending soon)")
            await asyncio.sleep(remaining)
            next_win = fetch_task.result()
            if ws:
                await ws.close()
            return next_win

        if state.exit_triggered:
            remaining = window.end_epoch - now
            log.info(
                "Exit done for %s, sleeping %ds until window end (%s) — pre-fetching next window...",
                window.short_label, remaining, window.end_time,
            )
            # Pre-fetch next window while we sleep
            fetch_task = asyncio.create_task(
                asyncio.to_thread(_find_next_window_after, window.end_epoch)
            )
            await asyncio.sleep(remaining)
            next_win = fetch_task.result()
            if ws:
                await ws.close()
            return next_win

        await asyncio.sleep(1)

    if ws:
        await ws.close()
    return None


def _find_next_window_after(after_epoch: int) -> Optional[MarketWindow]:
    """Find the next window after the given epoch (delegates to market.find_window_after)."""
    return find_window_after(after_epoch)


def _find_and_preopen_next_window(
    current_window: MarketWindow,
) -> Optional[MarketWindow]:
    """
    Find the window that starts after current_window.end_epoch and return it.
    Does not block — returns immediately or None.
    """
    next_win = _find_next_window_after(current_window.end_epoch)
    if next_win is None:
        log.warning("No next window found after %s", current_window.short_label)
        return None

    PREOPEN_BUFFER = 10  # seconds before window start to wake up
    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - PREOPEN_BUFFER

    if now_epoch < wake_epoch:
        # Next window is still far out — sleep until pre-open time
        remaining = wake_epoch - now_epoch
        log.debug(
            "Pre-open: sleeping %ds until %s starts at %s",
            remaining, next_win.short_label, next_win.start_time,
        )
        # We don't sleep here — the caller (monitor_window) will sleep
        return next_win

    # Already at or past wake_epoch — return immediately for immediate monitoring
    return next_win


async def monitor_window(
    window: MarketWindow,
    dry_run: bool = False,
    preopened: bool = False,
) -> Optional[MarketWindow]:
    """
    Monitor a 5-minute window using WebSocket real-time price updates.

    Args:
        window: The window to monitor.
        dry_run: If True, log actions but don't place orders.
        preopened: If True, this window was pre-opened after skipping the previous one;
                   skip the stale check and monitor immediately even if it just started.

    Returns the next window if it was pre-opened and is ready to monitor immediately
    (caller should call monitor_window again without extra sleep).
    Returns None when no pre-open is pending (normal completion or no next window).
    """
    state = MonitorState()
    ws: Optional[PriceStream] = None

    now_epoch = int(time.time())
    elapsed_since_start = now_epoch - window.start_epoch

    # Skip windows that started more than 5 seconds ago — pre-open next immediately
    if not preopened and elapsed_since_start > 5:
        log.info(
            "Window %s started %ds ago (>5s), skipping — pre-opening next window.",
            window.short_label, elapsed_since_start,
        )
        next_win = _find_and_preopen_next_window(window)
        return next_win  # caller will monitor next_win immediately

    # Wait for window start if not yet started
    if elapsed_since_start < 0:
        wait_sec = window.start_epoch - now_epoch
        log.debug("Waiting %ds for window to start...", wait_sec)
        await asyncio.sleep(wait_sec)

    log.info("=== Window STARTED: %s ===", window.short_label)

    buy_token_id, price_token_id = _side_token(window)
    log.info(
        "Trading side: %s | buy_token: %s | price_token: %s",
        config.BUY_SIDE.upper(), buy_token_id[:20], price_token_id[:20],
    )

    token_ids = [window.up_token, window.down_token]
    ws = PriceStream(on_price=functools.partial(
        _on_price_update, window=window, state=state, dry_run=dry_run,
    ))
    await ws.connect(token_ids)

    opening_price = ws.get_latest_price(price_token_id)
    if opening_price is None:
        opening_price = await get_midpoint_async(price_token_id)

    if opening_price is not None:
        log.info("Opening price: %s", opening_price)
        await _handle_opening_price(window, state, buy_token_id, opening_price, dry_run)
    else:
        log.warning("Could not get opening price.")

    # Monitor until window expires or exit triggered
    # next_win may have been pre-fetched during the wait
    next_win = await _monitor_single_window(window, state, ws, dry_run)

    if next_win is not None:
        # Pre-fetched during sleep — no need to search again
        return next_win

    # Fallback: window ended without pre-fetch (e.g. window expired without position)
    next_win = find_next_window()
    if next_win is None:
        log.warning("No next window found")
        return None

    PREOPEN_BUFFER = 10
    now_epoch = int(time.time())
    wake_epoch = next_win.start_epoch - PREOPEN_BUFFER

    if now_epoch < wake_epoch:
        log.debug(
            "Pre-open: sleeping %ds until %s starts at %s",
            wake_epoch - now_epoch, next_win.short_label, next_win.start_time,
        )
        await asyncio.sleep(wake_epoch - now_epoch)

    return next_win


async def _on_price_update(
    update: PriceUpdate,
    window: MarketWindow,
    state: MonitorState,
    dry_run: bool,
) -> None:
    """
    Called by PriceStream whenever a price update arrives.
    Triggers buy / stop-loss / take-profit immediately on signal.
    Uses config.BUY_SIDE to determine which token to monitor and trade.
    """
    buy_token_id, price_token_id = _side_token(window)

    if update.token_id != price_token_id:
        return

    price = update.midpoint
    if price is None:
        return

    state.latest_midpoint = price

    # For SL/TP decisions, prefer last_trade_price (actual execution) over
    # midpoint (derived from bid/ask, can lag during rapid moves).
    # We still use midpoint for buy decisions since it's more stable.
    sl_tp_price = price if update.is_trade else None
    log.debug(
        "WS price update | %s: %s (source=%s, sl_tp_price=%s)",
        config.BUY_SIDE.upper(), price, update.source, sl_tp_price,
    )

    # Permanently blocked for this window — stop checking entirely
    if state.buy_blocked_sl or state.buy_blocked_tp:
        return

    # Price in buy range: allow buy or re-buy if within re-entry limits
    if not state.bought:
        # Take-profit re-entry: controlled by MAX_TP_REENTRY
        if state.tp_count > config.MAX_TP_REENTRY:
            log.warning(
                "Buy permanently blocked by TP: tp_count=%d > MAX_TP=%d for window %s",
                state.tp_count, config.MAX_TP_REENTRY, window.short_label,
            )
            state.buy_blocked_tp = True
            return
        max_reentry = config.MAX_STOP_LOSS_REENTRY
        if state.stop_loss_count > max_reentry:
            log.warning(
                "Buy permanently blocked by SL: stop_loss_count=%d > MAX=%d for window %s",
                state.stop_loss_count, max_reentry, window.short_label,
            )
            state.buy_blocked_sl = True
            return
        if config.BUY_THRESHOLD_LOW < price < config.BUY_THRESHOLD_HIGH:
            log.info("Price %s moved into buy range — buying now!", price)
            await _handle_opening_price(window, state, buy_token_id, price, dry_run)
        return

    if state.exit_triggered:
        return  # Wait until window end; re-buy check on next cycle

    # Already holding — check stop-loss / take-profit
    # Use the more timely price for SL/TP: prefer last_trade_price over midpoint.
    check_price = sl_tp_price if sl_tp_price is not None else price

    if check_price > config.TAKE_PROFIT:
        log.warning(
            "TAKE-PROFIT triggered at %s=%s (>%.0f¢) [source=%s]",
            config.BUY_SIDE.upper(), check_price, config.TAKE_PROFIT * 100, update.source,
        )
        state.exit_triggered = True
        state.tp_count += 1
        state.bought = False
        cancel_all_open_orders()
        if not dry_run:
            result = await sell_token(
                buy_token_id, state.holding_size, f"Take-profit @ {check_price}",
                window_end_epoch=window.end_epoch,
            )
            if not result.success:
                log.error("Take-profit sell FAILED: %s", result.message)
        else:
            log.info("[DRY-RUN] Would SELL (take-profit)")
        return

    if check_price < config.STOP_LOSS:
        log.warning(
            "STOP-LOSS triggered at %s=%s (<%.0f¢) [%d/%d] [source=%s]",
            config.BUY_SIDE.upper(), check_price, config.STOP_LOSS * 100,
            state.stop_loss_count + 1, config.MAX_STOP_LOSS_REENTRY + 1, update.source,
        )
        state.exit_triggered = True
        state.stop_loss_count += 1
        state.bought = False
        cancel_all_open_orders()
        if not dry_run:
            result = await sell_token(
                buy_token_id, state.holding_size, f"Stop-loss @ {check_price}",
                window_end_epoch=window.end_epoch,
            )
            if not result.success:
                log.error("Stop-loss sell FAILED: %s", result.message)
        else:
            log.info("[DRY-RUN] Would SELL (stop-loss)")


async def _handle_opening_price(
    window: MarketWindow,
    state: MonitorState,
    buy_token_id: str,
    price: float,
    dry_run: bool,
) -> None:
    """Handle the opening price check and buy decision."""
    if state.bought:
        return  # Already bought

    # Re-entry after stop-loss: clear exit flag so next stop-loss can fire
    if state.exit_triggered:
        state.exit_triggered = False

    in_range = config.BUY_THRESHOLD_LOW < price < config.BUY_THRESHOLD_HIGH

    if in_range:
        log.info(
            "Price %s in buy range (%.0f¢-%.0f¢), placing order.",
            price,
            config.BUY_THRESHOLD_LOW * 100,
            config.BUY_THRESHOLD_HIGH * 100,
        )
        if not dry_run:
            # Set bought=True immediately to prevent duplicate buy (optimistic lock)
            state.bought = True
            result = await buy_token(
                buy_token_id, config.BUY_AMOUNT, window.short_label,
                window_end_epoch=window.end_epoch,
            )
            if result.success:
                # filled_size is in shares for FOK; for GTC fallback, calculate from price
                if result.filled_size > 0:
                    state.holding_size = result.filled_size
                else:
                    # GTC fallback or unknown fill — estimate shares from dollar amount / price
                    state.holding_size = config.BUY_AMOUNT / price if price > 0 else config.BUY_AMOUNT
                state.entry_price = price
                log.info(
                    "Position opened at %s=%s, holding=%.4f shares",
                    config.BUY_SIDE.upper(), price, state.holding_size,
                )
            else:
                # Buy failed — reset flag so we can retry on next price update
                state.bought = False
                log.warning("Buy failed: %s", result.message)
        else:
            state.bought = True
            state.holding_size = config.BUY_AMOUNT / price if price > 0 else config.BUY_AMOUNT
            state.entry_price = price
            log.info(
                "[DRY-RUN] Would BUY $%s %s @ %s (%.4f shares)",
                config.BUY_AMOUNT, config.BUY_SIDE.upper(), price, state.holding_size,
            )
    else:
        log.info(
            "Opening price %s not in buy range (%.0f¢-%.0f¢), skipping %s.",
            price,
            config.BUY_THRESHOLD_LOW * 100,
            config.BUY_THRESHOLD_HIGH * 100,
            window.short_label,
        )
