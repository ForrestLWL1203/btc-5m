"""Trading operations — async buy, sell, heartbeat, and position management."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from polybot.core import config
from polybot.core.client import get_client, get_midpoint, get_order_options, round_to_tick
from polybot.core.log_formatter import TRADE, log_event

log = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Result of an order attempt."""

    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    avg_price: float = 0.0
    message: str = ""


# ─── API Heartbeat ────────────────────────────────────────────────────────────

_heartbeat_task: Optional[asyncio.Task] = None


async def start_heartbeat() -> None:
    """Start the API heartbeat loop — MUST be running while GTC orders are open.

    Polymarket cancels ALL open orders if no heartbeat is sent within 10 seconds.
    """
    global _heartbeat_task
    if _heartbeat_task is not None and not _heartbeat_task.done():
        return  # Already running

    async def _loop() -> None:
        while True:
            try:
                get_client().post_heartbeat(None)
                log.debug("API heartbeat sent")
            except Exception as e:
                log.debug("API heartbeat failed: %s", e)
            await asyncio.sleep(config.API_HEARTBEAT_INTERVAL)

    _heartbeat_task = asyncio.create_task(_loop())
    log.debug("API heartbeat started (interval=%ss)", config.API_HEARTBEAT_INTERVAL)


def stop_heartbeat() -> None:
    """Stop the API heartbeat loop."""
    global _heartbeat_task
    if _heartbeat_task is not None:
        _heartbeat_task.cancel()
        _heartbeat_task = None
        log.debug("API heartbeat stopped")


# ─── Order Helpers ─────────────────────────────────────────────────────────────

def _is_425_error(exc: Exception) -> bool:
    """Check if an exception is caused by HTTP 425 (matching engine restart)."""
    msg = str(exc).lower()
    return "425" in msg or "too early" in msg


async def _post_fok_market(
    token_id: str, amount: float, side: str, retry_count: int, retry_interval: float
) -> OrderResult:
    """
    Attempt a FOK (Fill-Or-Kill) market order with retry.

    FOK either fills the entire order or nothing — no partial fills.
    Retries up to retry_count times with retry_interval between attempts.
    """
    side_const = BUY if side == BUY else SELL
    engine_retry = 0
    max_engine_retries = 3
    engine_backoff = 2.0

    t_total = time.monotonic()
    for attempt in range(1, retry_count + 1):
        try:
            client = get_client()
            args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side_const,
            )
            options = get_order_options(token_id)
            t_attempt = time.monotonic()
            signed = client.create_market_order(args, options=options)
            resp = client.post_order(signed, OrderType.FOK)
            attempt_ms = round((time.monotonic() - t_attempt) * 1000)

            resp_id = resp.get("orderID") or resp.get("orderId") or resp.get("id", "")
            status = resp.get("status", "").upper()
            filled = float(resp.get("sizeFilled", resp.get("filledSize", 0)))
            price = float(resp.get("avgPrice", resp.get("price", 0.0)))

            log_event(log, logging.DEBUG, TRADE, {
                "action": "FOK_RAW_RESP",
                "side": side,
                "order_id": resp_id,
                "status": status,
                "filled": filled,
                "price": price,
                "raw_keys": list(resp.keys()),
                "attempt": attempt,
            })

            # FOK response never includes filled/price — estimate from amount
            # Actual balance will be queried separately after buy/sell
            filled_size = filled if filled > 0 else amount
            avg_price = price if price > 0 else 0.0

            total_ms = round((time.monotonic() - t_total) * 1000)
            log_event(log, logging.INFO, TRADE, {
                "action": "FOK_FILLED",
                "side": side,
                "order_id": resp_id,
                "filled_size": filled_size,
                "avg_price": avg_price,
                "attempt": attempt,
                "attempt_ms": attempt_ms,
                "total_ms": total_ms,
            })

            return OrderResult(
                success=True,
                order_id=str(resp_id),
                filled_size=filled_size,
                avg_price=avg_price,
                message=f"FOK filled (attempt {attempt})",
            )

        except Exception as e:
            if _is_425_error(e) and engine_retry < max_engine_retries:
                engine_retry += 1
                log.warning(
                    "HTTP 425 (matching engine restart), retry %d/%d in %.0fs",
                    engine_retry, max_engine_retries, engine_backoff,
                )
                await asyncio.sleep(engine_backoff)
                engine_backoff = min(engine_backoff * 2, 30.0)
                continue

            log.debug("FOK attempt %d failed: %s", attempt, e)

        if attempt < retry_count:
            await asyncio.sleep(retry_interval)

    return OrderResult(success=False, message=f"FOK failed after {retry_count} attempts")


async def _post_gtd_limit(
    token_id: str, amount: float, side: str, price: float,
    expiration: Optional[int] = None,
    is_sell: bool = False,
) -> OrderResult:
    """
    Fallback: place a GTD (Good-Til-Date) limit order at the given price.

    GTD orders auto-expire at the given timestamp, eliminating the need for
    API heartbeat management.  Falls back to GTC + heartbeat if no expiration
    is provided.

    Args:
        amount: For BUY, this is dollars; for SELL (is_sell=True), this is shares.
        is_sell: If True, amount is already in shares — don't divide by price.
    """
    side_const = BUY if side == BUY else SELL
    try:
        client = get_client()
        aligned_price = round_to_tick(price, token_id)

        # BUY: amount is dollars → convert to shares via price
        # SELL: amount is already shares — use directly
        if is_sell:
            size = round(amount, 4)
        else:
            size = round(amount / aligned_price, 4) if aligned_price > 0 else amount

        use_gtd = expiration is not None and expiration > 0

        args = OrderArgs(
            token_id=token_id,
            price=aligned_price,
            size=size,
            side=side_const,
            expiration=expiration if use_gtd else 0,
        )
        signed = client.create_order(args, options=get_order_options(token_id))
        order_type = OrderType.GTD if use_gtd else OrderType.GTC
        resp = client.post_order(signed, order_type)

        resp_id = resp.get("orderID") or resp.get("orderId") or resp.get("id", "")
        log_event(log, logging.INFO, TRADE, {
            "action": "GTD_PLACED" if use_gtd else "GTC_PLACED",
            "side": side,
            "order_id": resp_id,
            "price": aligned_price,
            "size": size,
            "expiration": expiration if use_gtd else None,
        })

        if not use_gtd:
            # GTC fallback: start heartbeat to keep order alive (must send within 10s)
            await start_heartbeat()

        return OrderResult(
            success=True,
            order_id=str(resp_id),
            filled_size=0.0,
            avg_price=aligned_price,
            message=f"{'GTD' if use_gtd else 'GTC'} limit placed",
        )

    except Exception as e:
        log.error("Limit order failed: %s", e)
        return OrderResult(success=False, message=str(e))


# ─── Public API ────────────────────────────────────────────────────────────────

async def buy_up(
    token_id: str,
    amount: float,
    label: str,
    window_end_epoch: Optional[int] = None,
) -> OrderResult:
    """
    Buy the Up token using FOK market order with retry.
    Falls back to GTD limit at midpoint if FOK fails.
    """
    log_event(log, logging.INFO, TRADE, {
        "action": "BUY",
        "amount": amount,
        "label": label,
    })

    result = await _post_fok_market(
        token_id=token_id,
        amount=amount,
        side=BUY,
        retry_count=config.FOK_RETRY_COUNT,
        retry_interval=config.FOK_RETRY_INTERVAL,
    )

    if result.success:
        return result

    # FOK failed — try GTD limit at current midpoint
    if config.FALLBACK_GTC:
        log_event(log, logging.WARNING, TRADE, {
            "action": "GTD_FALLBACK",
            "reason": "FOK buy failed",
        })
        mid = get_midpoint(token_id)
        if mid:
            return await _post_gtd_limit(token_id, amount, BUY, mid, expiration=window_end_epoch)
        else:
            return OrderResult(success=False, message="Could not get midpoint for fallback")
    else:
        return result


async def sell_up(
    token_id: str,
    size: float,
    reason: str,
    price_hints: Optional[float] = None,
    window_end_epoch: Optional[int] = None,
) -> OrderResult:
    """
    Sell the Up token using FOK market order with retry.
    ``size`` is in **shares** (MarketOrderArgs.amount for SELL = shares).
    Falls back to GTD limit at best bid / midpoint if FOK fails.
    """
    log_event(log, logging.INFO, TRADE, {
        "action": "SELL",
        "shares": size,
        "reason": reason,
    })

    # Stop heartbeat — we're exiting, no more GTC orders to keep alive
    stop_heartbeat()

    result = await _post_fok_market(
        token_id=token_id,
        amount=size,
        side=SELL,
        retry_count=config.FOK_RETRY_COUNT,
        retry_interval=config.FOK_RETRY_INTERVAL,
    )

    if result.success:
        return result

    if config.FALLBACK_GTC:
        log_event(log, logging.WARNING, TRADE, {
            "action": "GTD_FALLBACK",
            "reason": "FOK sell failed",
        })
        fallback_price = price_hints or get_midpoint(token_id)
        if fallback_price:
            return await _post_gtd_limit(
                token_id, size, SELL, fallback_price,
                expiration=window_end_epoch, is_sell=True,
            )
        else:
            return OrderResult(success=False, message="Could not get price for fallback")
    else:
        return result


async def cancel_all_open_orders() -> None:
    """Cancel all open orders and stop heartbeat (non-blocking)."""
    stop_heartbeat()
    try:
        await asyncio.to_thread(get_client().cancel_all)
        log_event(log, logging.INFO, TRADE, {"action": "CANCEL_ALL"})
    except Exception as e:
        log_event(log, logging.WARNING, TRADE, {
            "action": "CANCEL_ALL_FAILED",
            "message": str(e),
        })


# Generic aliases — these functions accept any token_id (names are historical)
buy_token = buy_up
sell_token = sell_up
