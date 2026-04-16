"""Unit tests for polybot.trading.trading — order execution logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.core import config
from polybot.trading.trading import (
    OrderResult,
    _post_fak_market,
    _post_gtd_limit,
    cancel_all_open_orders,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _mock_client(fills: list[dict]):
    """
    Build a mock ClobClient whose post_order returns a sequence of fill responses.
    Each fill dict: {"sizeFilled": X, "avgPrice": Y, "orderID": "id", "status": "MATCHED"}
    """
    client = MagicMock()
    client.create_market_order.return_value = {}
    client.post_order.side_effect = fills
    return client


# ─── FAK BUY: partial fill unit conversion ────────────────────────────────────

@pytest.mark.asyncio
async def test_fak_buy_partial_fill_dollar_tracking():
    """
    BUY $5 @ 50¢. First FAK fills 4 shares ($2), second fills remaining ($3 worth).

    Verifies remaining_amount is tracked in dollars, not shares.
    """
    fills = [
        {"sizeFilled": "4.0", "avgPrice": "0.50", "orderID": "ord-1", "status": "MATCHED"},
        {"sizeFilled": "6.0", "avgPrice": "0.50", "orderID": "ord-2", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,  # $5 to spend
            side="BUY",
            retry_count=10,
            retry_interval=0.01,
        )

    assert result.success
    # Total shares: 4 + 6 = 10 shares
    assert result.filled_size == 10.0
    # Total cost: 4*0.50 + 6*0.50 = $5.0
    assert result.avg_price == pytest.approx(0.50)
    # post_order should have been called twice
    assert mock_client.post_order.call_count == 2
    # Second call should have amount=$3 (5 - 4*0.50)
    second_call_args = mock_client.create_market_order.call_args_list[1]
    assert second_call_args[0][0].amount == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_fak_buy_single_full_fill():
    """BUY $5 fully filled in one attempt."""
    fills = [
        {"sizeFilled": "10.0", "avgPrice": "0.50", "orderID": "ord-1", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=10,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0
    assert mock_client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_fak_sell_partial_fill_share_tracking():
    """
    SELL 10 shares. First FAK fills 4 shares, second fills 6.
    For SELL, amount and sizeFilled are both in shares — no conversion needed.
    """
    fills = [
        {"sizeFilled": "4.0", "avgPrice": "0.50", "orderID": "ord-1", "status": "MATCHED"},
        {"sizeFilled": "6.0", "avgPrice": "0.48", "orderID": "ord-2", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=10.0,  # 10 shares to sell
            side="SELL",
            retry_count=10,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0
    assert mock_client.post_order.call_count == 2
    # Second call should have amount=6 (10 - 4 = 6 shares remaining)
    second_call_args = mock_client.create_market_order.call_args_list[1]
    assert second_call_args[0][0].amount == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_fak_no_fill_returns_failure():
    """FAK with no fills returns failure."""
    fills = [
        {"sizeFilled": "0", "avgPrice": "0", "orderID": "", "status": "LIVE"},
    ] * 3
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert not result.success
    assert "no fills" in result.message.lower()


# ─── GTD limit: BUY vs SELL size calculation ──────────────────────────────────

@pytest.mark.asyncio
async def test_gtd_buy_size_is_shares():
    """GTD BUY: size = amount / price (dollars → shares)."""
    mock_client = MagicMock()
    mock_client.create_order.return_value = {}
    mock_client.post_order.return_value = {"orderID": "gtd-1", "status": "LIVE"}

    with patch("polybot.trading.trading.get_client", return_value=mock_client), \
         patch("polybot.trading.trading.round_to_tick", return_value=0.50):
        result = await _post_gtd_limit(
            token_id="token-1",
            amount=5.0,  # $5
            side="BUY",
            price=0.50,
            expiration=1000,
            is_sell=False,
        )

    assert result.success
    # Verify size = 5.0 / 0.50 = 10.0 shares
    call_args = mock_client.create_order.call_args[0][0]
    assert call_args.size == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_gtd_sell_size_is_shares_directly():
    """GTD SELL: size = amount directly (already in shares)."""
    mock_client = MagicMock()
    mock_client.create_order.return_value = {}
    mock_client.post_order.return_value = {"orderID": "gtd-2", "status": "LIVE"}

    with patch("polybot.trading.trading.get_client", return_value=mock_client), \
         patch("polybot.trading.trading.round_to_tick", return_value=0.50):
        result = await _post_gtd_limit(
            token_id="token-1",
            amount=10.0,  # 10 shares
            side="SELL",
            price=0.50,
            expiration=1000,
            is_sell=True,
        )

    assert result.success
    # Verify size = 10.0 (not 10.0/0.50 = 20.0)
    call_args = mock_client.create_order.call_args[0][0]
    assert call_args.size == pytest.approx(10.0)


# ─── cancel_all_open_orders is async ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_all_is_async():
    """cancel_all_open_orders should not block the event loop."""
    mock_client = MagicMock()
    mock_client.cancel_all.return_value = None

    with patch("polybot.trading.trading.get_client", return_value=mock_client), \
         patch("polybot.trading.trading.stop_heartbeat"):
        await cancel_all_open_orders()

    mock_client.cancel_all.assert_called_once()
