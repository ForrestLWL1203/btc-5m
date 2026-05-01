"""Tests for Coinbase BTC price feed."""

import json
from unittest.mock import AsyncMock

import pytest

from polybot.market.coinbase import CoinbasePriceFeed


def test_coinbase_feed_records_advanced_trade_ticker_event():
    feed = CoinbasePriceFeed(product_id="BTC-USD")

    feed._handle_message(json.dumps({
        "channel": "ticker",
        "events": [
            {
                "type": "update",
                "tickers": [
                    {
                        "product_id": "BTC-USD",
                        "price": "61234.50",
                        "time": "2026-05-01T00:00:01Z",
                    }
                ],
            }
        ],
    }))

    assert feed.latest_price == pytest.approx(61234.50)
    assert feed.price_at_or_before(1777593601.5) == pytest.approx(61234.50)
    assert feed.first_price_at_or_after(1777593600.5) == pytest.approx(61234.50)


def test_coinbase_feed_uses_outer_timestamp_for_nested_tickers():
    feed = CoinbasePriceFeed(product_id="BTC-USD")

    feed._handle_message(json.dumps({
        "channel": "ticker",
        "timestamp": "2026-05-01T00:00:02Z",
        "events": [
            {
                "type": "update",
                "tickers": [{"product_id": "BTC-USD", "price": "61235.00"}],
            }
        ],
    }))

    assert feed.price_at_or_before(1777593602.5) == pytest.approx(61235.00)


def test_coinbase_feed_records_legacy_ticker_shape_and_inserts_late_ticks():
    feed = CoinbasePriceFeed(product_id="btcusdt")

    for raw in [
        {"type": "ticker", "product_id": "BTC-USD", "price": "100.0", "time": "2026-05-01T00:00:01Z"},
        {"type": "ticker", "product_id": "BTC-USD", "price": "102.0", "time": "2026-05-01T00:00:03Z"},
        {"type": "ticker", "product_id": "BTC-USD", "price": "101.0", "time": "2026-05-01T00:00:02Z"},
    ]:
        feed._handle_message(json.dumps(raw))

    assert feed.price_at_or_before(1777593601.5) == pytest.approx(100.0)
    assert feed.price_at_or_before(1777593602.5) == pytest.approx(101.0)
    assert feed.latest_price == pytest.approx(102.0)


def test_coinbase_feed_ignores_other_products_and_bad_values():
    feed = CoinbasePriceFeed(product_id="BTC-USD")

    feed._handle_message(json.dumps({"product_id": "ETH-USD", "price": "3000.0"}))
    feed._handle_message(json.dumps({"product_id": "BTC-USD", "price": ""}))
    feed._handle_message(json.dumps({"product_id": "BTC-USD", "price": "NaN"}))

    assert feed.latest_price is None


@pytest.mark.asyncio
async def test_coinbase_subscribe_uses_ticker_channel():
    feed = CoinbasePriceFeed(product_id="BTC-USD")
    ws = AsyncMock()
    feed._ws = ws

    await feed._subscribe()

    sent = json.loads(ws.send.await_args.args[0])
    assert sent == {
        "type": "subscribe",
        "product_ids": ["BTC-USD"],
        "channel": "ticker",
    }
