"""Reusable FAK order execution gateway."""

from typing import Callable, Optional

from polybot.trading.trading import OrderResult, buy_token, sell_token


async def place_fak_buy(
    token_id: str,
    amount: float,
    *,
    price_hint: Optional[float] = None,
    price_hint_refresher: Optional[Callable[[], Optional[float]]] = None,
    retry_count: Optional[int] = None,
) -> OrderResult:
    """Place a BUY FAK order through the shared trading implementation."""
    return await buy_token(
        token_id,
        amount,
        price_hint=price_hint,
        price_hint_refresher=price_hint_refresher,
        retry_count=retry_count,
    )


async def place_fak_stop_loss_sell(
    token_id: str,
    shares: float,
    *,
    price_hint: Optional[float] = None,
    price_hint_refresher: Optional[Callable[[], Optional[float]]] = None,
    retry_count: Optional[int] = None,
) -> OrderResult:
    """Place a stop-loss SELL FAK order through the shared trading implementation."""
    return await sell_token(
        token_id,
        shares,
        price_hint=price_hint,
        price_hint_refresher=price_hint_refresher,
        retry_count=retry_count,
    )
