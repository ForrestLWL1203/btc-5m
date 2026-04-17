"""Unit tests for polybot.trading.trading — order execution logic."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.trading.trading import (
    OrderResult,
    _post_fok_market,
    _post_gtd_limit,
    cancel_all_open_orders,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _mock_client(fills: list[dict] | Exception):
    """Build a mock ClobClient whose post_order returns fill responses or raises."""
    client = MagicMock()
    client.create_market_order.return_value = {}
    if isinstance(fills, Exception):
        client.post_order.side_effect = fills
    else:
        client.post_order.side_effect = fills
    return client


# ─── FOK BUY ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fok_buy_success():
    """FOK BUY fills entire order in one attempt."""
    fills = [
        {"sizeFilled": "10.0", "avgPrice": "0.50", "orderID": "ord-1", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fok_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0
    assert mock_client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_fok_buy_retries_on_failure():
    """FOK BUY retries and succeeds on second attempt."""
    fills = [
        Exception("No liquidity"),
        {"sizeFilled": "10.0", "avgPrice": "0.50", "orderID": "ord-2", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fok_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert mock_client.post_order.call_count == 2


@pytest.mark.asyncio
async def test_fok_buy_all_retries_fail():
    """FOK BUY returns failure after exhausting retries."""
    fills = [Exception("No liquidity")] * 5
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fok_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=5,
            retry_interval=0.01,
        )

    assert not result.success
    assert "FOK failed" in result.message


@pytest.mark.asyncio
async def test_fok_sell_success():
    """FOK SELL fills entire order."""
    fills = [
        {"sizeFilled": "10.0", "avgPrice": "0.48", "orderID": "ord-1", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fok_market(
            token_id="token-1",
            amount=10.0,
            side="SELL",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0


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
            amount=5.0,
            side="BUY",
            price=0.50,
            expiration=1000,
            is_sell=False,
        )

    assert result.success
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
            amount=10.0,
            side="SELL",
            price=0.50,
            expiration=1000,
            is_sell=True,
        )

    assert result.success
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
