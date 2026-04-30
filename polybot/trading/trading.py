"""Trading operations — async buy and position management."""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from py_clob_client_v2 import MarketOrderArgs, OrderType
from py_clob_client_v2.order_builder.constants import BUY, SELL

from polybot.core import config
from polybot.core.client import get_client, get_order_options
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


# ─── Order Helpers ─────────────────────────────────────────────────────────────

def _is_425_error(exc: Exception) -> bool:
    """Check if an exception is caused by HTTP 425 (matching engine restart)."""
    msg = str(exc).lower()
    return "425" in msg or "too early" in msg


def _is_insufficient_funds_error(exc: Exception) -> bool:
    """Check if an exception is caused by insufficient balance/allowance."""
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "insufficient",
            "not enough",
            "balance",
            "allowance",
            "collateral",
        )
    )


def _extract_error_details(exc: Exception) -> dict:
    """Return structured details from SDK/API exceptions for logging."""
    details = {
        "error_type": type(exc).__name__,
        "message": str(exc),
    }
    status_code = getattr(exc, "status_code", None)
    error_msg = getattr(exc, "error_msg", None)
    if status_code is not None:
        details["status_code"] = status_code
    if error_msg is not None:
        details["error_msg"] = error_msg
    return details


def _safe_float(value) -> float:
    """Best-effort float conversion for API fields."""
    try:
        if value in (None, ""):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_plain_mapping(value) -> dict:
    """Best-effort conversion for SDK objects/dicts used in diagnostics."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "dict"):
        dumped = value.dict()
        return dumped if isinstance(dumped, dict) else {}
    if hasattr(value, "__dict__"):
        return dict(vars(value))
    return {}


def _signed_order_diagnostics(signed_order, requested_side: str) -> dict:
    """
    Extract non-secret fields from a signed order.

    This is used to compare our SDK-generated FAK order with Polymarket web UI
    requests. Signature/private-key material is intentionally not logged.
    """
    payload = _as_plain_mapping(signed_order)
    order = _as_plain_mapping(payload.get("order", payload))

    maker_amount = order.get("makerAmount") or order.get("maker_amount")
    taker_amount = order.get("takerAmount") or order.get("taker_amount")
    signed_side = order.get("side") or requested_side

    maker = _safe_float(maker_amount)
    taker = _safe_float(taker_amount)
    signed_limit_price = 0.0
    if maker > 0 and taker > 0:
        if _side_name(signed_side) == BUY:
            signed_limit_price = maker / taker
        else:
            signed_limit_price = taker / maker

    diagnostics = {
        "signed_side": signed_side,
        "maker_amount": maker_amount,
        "taker_amount": taker_amount,
    }
    if signed_limit_price > 0:
        diagnostics["signed_limit_price"] = signed_limit_price
    return diagnostics


def _side_name(side) -> str:
    """Normalize SDK side constants, enums, and plain strings."""
    if side in (0, "0"):
        return BUY
    if side in (1, "1"):
        return SELL
    name = getattr(side, "name", None)
    if name in (BUY, SELL):
        return name
    value = getattr(side, "value", None)
    if value in (0, "0", BUY):
        return BUY
    if value in (1, "1", SELL):
        return SELL
    text = str(side).upper()
    if text.endswith(".BUY"):
        return BUY
    if text.endswith(".SELL"):
        return SELL
    return BUY if text == BUY else SELL if text == SELL else text


def _derive_fill_from_amounts(
    side: str,
    requested_amount: float,
    taking_amount: float,
    making_amount: float,
    fallback_price: float,
) -> tuple[float, float]:
    """Derive filled shares and avg price from CLOB FAK response amounts."""
    if side == BUY or str(side).upper() == "BUY":
        filled = taking_amount if taking_amount > 0 else 0.0
        price = (
            making_amount / taking_amount
            if making_amount > 0 and taking_amount > 0
            else fallback_price
        )
        if filled <= 0 and price > 0:
            filled = requested_amount / price
        return filled, price

    filled = making_amount if making_amount > 0 else requested_amount
    price = (
        taking_amount / making_amount
        if taking_amount > 0 and making_amount > 0
        else fallback_price
    )
    return filled, price


async def _post_fak_market(
    token_id: str, amount: float, side: str, retry_count: int, retry_interval: float,
    price_hint: Optional[float] = None,
    price_hint_refresher: Optional[Callable[[], Optional[float]]] = None,
) -> OrderResult:
    """
    Attempt a FAK (Fill-And-Kill) market order with retry.

    FAK fills as much as possible immediately and cancels the rest.
    Partial fills are accepted — only retries when sizeFilled == 0 (zero depth).
    Matches Polymarket web UI behavior and avoids all-or-nothing fill failures.
    """
    side_const = BUY if side == BUY else SELL
    engine_retry = 0
    max_engine_retries = 3
    engine_backoff = 2.0
    current_price_hint = price_hint

    t_total = time.monotonic()
    for attempt in range(1, retry_count + 1):
        t_attempt = None
        create_ms = None
        post_ms = None
        if attempt > 1 and price_hint_refresher is not None:
            refreshed_hint = price_hint_refresher()
            if refreshed_hint is None:
                return OrderResult(
                    success=False,
                    message="FAK retry aborted: refreshed price hint unavailable",
                )
            if refreshed_hint != current_price_hint:
                log_event(log, logging.INFO, TRADE, {
                    "action": "FAK_PRICE_HINT_REFRESH",
                    "old_price_hint": current_price_hint,
                    "new_price_hint": refreshed_hint,
                    "attempt": attempt,
                })
            current_price_hint = refreshed_hint

        try:
            client = get_client()
            args = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=side_const,
                order_type=OrderType.FAK,
                price=current_price_hint or 0,  # non-zero skips SDK's internal GET /book
            )
            options = get_order_options(token_id)
            t_attempt = time.monotonic()
            t_create = time.monotonic()
            signed = client.create_market_order(args, options=options)
            create_ms = round((time.monotonic() - t_create) * 1000)
            log_event(log, logging.INFO, TRADE, {
                "action": "FAK_SIGNED_ORDER",
                "side": side,
                "token": token_id[:20],
                "amount": amount,
                "price_hint": current_price_hint,
                "attempt": attempt,
                "create_market_order_ms": create_ms,
                **_signed_order_diagnostics(signed, side_const),
            })
            t_post = time.monotonic()
            resp = client.post_order(signed, OrderType.FAK)
            post_ms = round((time.monotonic() - t_post) * 1000)
            attempt_ms = round((time.monotonic() - t_attempt) * 1000)

            resp_id = resp.get("orderID") or resp.get("orderId") or resp.get("id", "")
            status = resp.get("status", "").upper()
            success = resp.get("success", False)
            filled = _safe_float(resp.get("sizeFilled", resp.get("filledSize", 0)))
            price = _safe_float(resp.get("avgPrice", resp.get("price", 0.0)))
            taking_amount = _safe_float(resp.get("takingAmount"))
            making_amount = _safe_float(resp.get("makingAmount"))

            log_event(log, logging.DEBUG, TRADE, {
                "action": "FAK_RAW_RESP",
                "side": side,
                "order_id": resp_id,
                "status": status,
                "success": success,
                "filled": filled,
                "price": price,
                "taking_amount": taking_amount,
                "making_amount": making_amount,
                "raw_keys": list(resp.keys()),
                "attempt": attempt,
                "create_market_order_ms": create_ms,
                "post_order_ms": post_ms,
            })

            # Some Polymarket FAK responses omit sizeFilled/avgPrice entirely even
            # when the order matched. When that happens, trust MATCHED/success and
            # derive a reasonable filled-size fallback so we don't retry and double-buy.
            if success and status == "MATCHED" and (filled <= 0 or price <= 0):
                derived_filled, derived_price = _derive_fill_from_amounts(
                    side_const,
                    amount,
                    taking_amount,
                    making_amount,
                    current_price_hint or price,
                )
                if filled <= 0:
                    filled = derived_filled
                if price <= 0:
                    price = derived_price

            if filled > 0:
                avg_price = price if price > 0 else 0.0
                total_ms = round((time.monotonic() - t_total) * 1000)
                log_event(log, logging.INFO, TRADE, {
                    "action": "FAK_FILLED",
                    "side": side,
                    "order_id": resp_id,
                    "filled_size": filled,
                    "avg_price": avg_price,
                    "requested": amount,
                    "attempt": attempt,
                    "create_market_order_ms": create_ms,
                    "post_order_ms": post_ms,
                    "attempt_ms": attempt_ms,
                    "total_ms": total_ms,
                })
                return OrderResult(
                    success=True,
                    order_id=str(resp_id),
                    filled_size=filled,
                    avg_price=avg_price,
                    message=f"FAK filled (attempt {attempt})",
                )

        except Exception as e:
            attempt_ms = round((time.monotonic() - t_attempt) * 1000) if t_attempt is not None else None
            if _is_insufficient_funds_error(e):
                log_event(log, logging.ERROR, TRADE, {
                    "action": "FAK_INSUFFICIENT_FUNDS",
                    "side": side,
                    "token": token_id[:20],
                    "amount": amount,
                    "attempt": attempt,
                    "price_hint": current_price_hint,
                    "create_market_order_ms": create_ms,
                    "post_order_ms": post_ms,
                    "attempt_ms": attempt_ms,
                    **_extract_error_details(e),
                })
                return OrderResult(
                    success=False,
                    message=f"INSUFFICIENT_FUNDS: {e}",
                )
            if _is_425_error(e) and engine_retry < max_engine_retries:
                engine_retry += 1
                log.warning(
                    "HTTP 425 (matching engine restart), retry %d/%d in %.0fs",
                    engine_retry, max_engine_retries, engine_backoff,
                )
                await asyncio.sleep(engine_backoff)
                engine_backoff = min(engine_backoff * 2, 30.0)
                continue

            log_event(log, logging.WARNING, TRADE, {
                "action": "FAK_ATTEMPT_FAILED",
                "side": side,
                "token": token_id[:20],
                "amount": amount,
                "attempt": attempt,
                "retry_count": retry_count,
                "price_hint": current_price_hint,
                "create_market_order_ms": create_ms,
                "post_order_ms": post_ms,
                "attempt_ms": attempt_ms,
                **_extract_error_details(e),
            })

        if attempt < retry_count:
            await asyncio.sleep(retry_interval)

    return OrderResult(success=False, message=f"FAK failed after {retry_count} attempts")


# ─── Public API ────────────────────────────────────────────────────────────────

async def buy_token(
    token_id: str,
    amount: float,
    price_hint: Optional[float] = None,
    price_hint_refresher: Optional[Callable[[], Optional[float]]] = None,
    retry_count: Optional[int] = None,
) -> OrderResult:
    """
    Buy a token using FAK market order with retry.

    FAK fills whatever depth is available immediately; partial fills are accepted.
    If all retries return zero fill (no depth), the window is skipped — no GTD
    fallback, since a delayed fill at a stale price is worse than missing the entry.

    price_hint: best_ask from WS feed — skips SDK's internal GET /book when provided.
    """
    result = await _post_fak_market(
        token_id=token_id,
        amount=amount,
        side=BUY,
        retry_count=retry_count or config.FAK_RETRY_COUNT,
        retry_interval=config.FAK_RETRY_INTERVAL,
        price_hint=price_hint,
        price_hint_refresher=price_hint_refresher,
    )

    if result.success:
        return result

    return result


async def sell_token(
    token_id: str,
    shares: float,
    price_hint: Optional[float] = None,
    price_hint_refresher: Optional[Callable[[], Optional[float]]] = None,
    retry_count: Optional[int] = None,
) -> OrderResult:
    """Sell token shares using FAK market order with retry."""
    result = await _post_fak_market(
        token_id=token_id,
        amount=shares,
        side=SELL,
        retry_count=retry_count or config.FAK_RETRY_COUNT,
        retry_interval=config.FAK_RETRY_INTERVAL,
        price_hint=price_hint,
        price_hint_refresher=price_hint_refresher,
    )
    return result
