"""Unit tests for polybot.trading.trading — order execution logic."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from py_clob_client_v2 import OrderType

from polybot.trading.trading import (
    OrderResult,
    _derive_fill_from_amounts,
    _post_fak_market,
    _signed_order_diagnostics,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _mock_client(fills: list[dict] | Exception):
    """Build a mock ClobClient whose post_order returns fill responses or raises."""
    client = MagicMock()
    client.create_market_order.return_value = {}
    client.post_order.side_effect = fills
    return client


# ─── Signed Order Diagnostics ────────────────────────────────────────────────

def test_signed_order_diagnostics_derives_buy_limit_price():
    """BUY limit price is makerAmount / takerAmount, matching web UI payloads."""
    signed = {
        "order": {
            "makerAmount": "1000000",
            "takerAmount": "1315700",
            "side": "BUY",
        }
    }

    diag = _signed_order_diagnostics(signed, "BUY")

    assert diag["signed_side"] == "BUY"
    assert diag["maker_amount"] == "1000000"
    assert diag["taker_amount"] == "1315700"
    assert diag["signed_limit_price"] == pytest.approx(1000000 / 1315700)


def test_signed_order_diagnostics_derives_sell_limit_price():
    """SELL limit price is takerAmount / makerAmount."""
    signed = {
        "order": {
            "makerAmount": "2000000",
            "takerAmount": "900000",
            "side": "SELL",
        }
    }

    diag = _signed_order_diagnostics(signed, "SELL")

    assert diag["signed_limit_price"] == pytest.approx(0.45)


def test_signed_order_diagnostics_handles_v2_numeric_buy_side():
    """V2 SDK signed orders encode side as numeric enum values."""
    signed = {
        "makerAmount": "1000000",
        "takerAmount": "1315700",
        "side": 0,
    }

    diag = _signed_order_diagnostics(signed, "BUY")

    assert diag["signed_limit_price"] == pytest.approx(1000000 / 1315700)


def test_derive_buy_fill_from_taking_and_making_amounts():
    filled, price = _derive_fill_from_amounts(
        "BUY",
        requested_amount=1.0,
        taking_amount=1.42857,
        making_amount=1.0,
        fallback_price=0.72,
    )

    assert filled == pytest.approx(1.42857)
    assert price == pytest.approx(1.0 / 1.42857)


def test_derive_sell_fill_from_taking_and_making_amounts():
    filled, price = _derive_fill_from_amounts(
        "SELL",
        requested_amount=1.7,
        taking_amount=0.918,
        making_amount=1.7,
        fallback_price=0.32,
    )

    assert filled == pytest.approx(1.7)
    assert price == pytest.approx(0.54)


# ─── FAK BUY ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fak_buy_full_fill():
    """FAK BUY fills entire order in one attempt."""
    fills = [
        {"sizeFilled": "10.0", "avgPrice": "0.50", "orderID": "ord-1", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0
    assert mock_client.post_order.call_count == 1
    order_args = mock_client.create_market_order.call_args.args[0]
    assert order_args.order_type == OrderType.FAK
    assert mock_client.post_order.call_args.args[1] == OrderType.FAK


@pytest.mark.asyncio
async def test_fak_buy_partial_fill_above_threshold_is_success():
    """FAK BUY with fill ≥ 60% of requested amount is accepted immediately."""
    fills = [
        {"sizeFilled": "14.0", "avgPrice": "0.50", "orderID": "ord-2", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=10.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 14.0
    assert mock_client.post_order.call_count == 1



@pytest.mark.asyncio
async def test_fak_buy_retries_on_zero_fill():
    """FAK BUY retries when filled=0 (zero depth), succeeds on second attempt."""
    fills = [
        {"sizeFilled": "0", "avgPrice": "0", "orderID": "ord-3", "status": "UNMATCHED", "success": False},
        {"sizeFilled": "10.0", "avgPrice": "0.50", "orderID": "ord-4", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert mock_client.post_order.call_count == 2


@pytest.mark.asyncio
async def test_fak_buy_matched_without_sizefilled_is_treated_as_success():
    """Real API can return MATCHED without sizeFilled/avgPrice; don't retry that."""
    fills = [
        {
            "orderID": "ord-matched",
            "status": "MATCHED",
            "success": True,
            "takingAmount": "1.6667",
            "makingAmount": "1.0",
            "transactionsHashes": ["0xabc"],
        },
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=1.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
            price_hint=0.60,
        )

    assert result.success
    assert result.avg_price == pytest.approx(1.0 / 1.6667)
    assert result.filled_size == pytest.approx(1.6667)
    assert mock_client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_fak_buy_refreshes_price_hint_before_retry():
    """After a failed FAK attempt, retry uses the refreshed WS-derived hint."""
    fills = [
        Exception("no orders found to match with FAK order"),
        {
            "orderID": "ord-matched",
            "status": "MATCHED",
            "success": True,
            "takingAmount": "1.6129",
            "makingAmount": "1.0",
        },
    ]
    mock_client = _mock_client(fills)
    refresher = MagicMock(return_value=0.62)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=1.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
            price_hint=0.60,
            price_hint_refresher=refresher,
        )

    assert result.success
    assert result.avg_price == pytest.approx(1.0 / 1.6129)
    assert refresher.call_count == 1
    prices = [
        call.args[0].price
        for call in mock_client.create_market_order.call_args_list
    ]
    assert prices == [0.60, 0.62]


@pytest.mark.asyncio
async def test_fak_buy_aborts_retry_when_refreshed_hint_unavailable():
    """If retry ask is unavailable/above cap, don't keep posting stale FAKs."""
    mock_client = _mock_client([Exception("no orders found to match with FAK order")] * 3)
    refresher = MagicMock(return_value=None)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=1.0,
            side="BUY",
            retry_count=3,
            retry_interval=0.01,
            price_hint=0.60,
            price_hint_refresher=refresher,
        )

    assert not result.success
    assert "retry aborted" in result.message
    assert refresher.call_count == 1
    assert mock_client.post_order.call_count == 1


@pytest.mark.asyncio
async def test_fak_buy_all_retries_fail():
    """FAK BUY returns failure after exhausting retries with zero fills."""
    fills = [Exception("No liquidity")] * 5
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=5.0,
            side="BUY",
            retry_count=5,
            retry_interval=0.01,
        )

    assert not result.success
    assert "FAK failed" in result.message


@pytest.mark.asyncio
async def test_fak_sell_success():
    """FAK SELL fills entire order."""
    fills = [
        {"sizeFilled": "10.0", "avgPrice": "0.48", "orderID": "ord-1", "status": "MATCHED"},
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=10.0,
            side="SELL",
            retry_count=3,
            retry_interval=0.01,
        )

    assert result.success
    assert result.filled_size == 10.0


@pytest.mark.asyncio
async def test_fak_sell_matched_without_sizefilled_uses_response_amounts():
    """SELL fallback price must use proceeds/shares, not the FAK price hint."""
    fills = [
        {
            "orderID": "sell-matched",
            "status": "MATCHED",
            "success": True,
            "takingAmount": "0.918",
            "makingAmount": "1.7",
        },
    ]
    mock_client = _mock_client(fills)

    with patch("polybot.trading.trading.get_client", return_value=mock_client):
        result = await _post_fak_market(
            token_id="token-1",
            amount=1.7,
            side="SELL",
            retry_count=3,
            retry_interval=0.01,
            price_hint=0.32,
        )

    assert result.success
    assert result.filled_size == pytest.approx(1.7)
    assert result.avg_price == pytest.approx(0.54)
