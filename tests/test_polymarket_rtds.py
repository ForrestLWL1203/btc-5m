"""Tests for Polymarket RTDS crypto price feed."""

import json
from unittest.mock import AsyncMock

import pytest

from polybot.market.polymarket_rtds import PolymarketRTDSPriceFeed


def test_rtds_feed_records_btcusdt_crypto_updates():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "timestamp": 1753314088421,
        "payload": {
            "symbol": "btcusdt",
            "timestamp": 1753314088395,
            "value": 67234.50,
        },
    }))

    assert feed.latest_price == pytest.approx(67234.50)
    assert feed.price_at_or_before(1753314088.5) == pytest.approx(67234.50)
    assert feed.first_price_at_or_after(1753314088.3) == pytest.approx(67234.50)


def test_rtds_feed_records_filtered_history_items_without_symbol():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {
            "symbol": "btcusdt",
            "data": [
                {"timestamp": 1753314088000, "value": 67234.50},
                {"timestamp": 1753314089000, "value": 67235.25},
            ],
        },
    }))

    assert feed.latest_price == pytest.approx(67235.25)
    assert feed.price_at_or_before(1753314088.5) == pytest.approx(67234.50)


def test_rtds_feed_keeps_inner_symbol_when_outer_symbol_differs():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {
            "symbol": "ethusdt",
            "data": [
                {"symbol": "btcusdt", "timestamp": 1753314088000, "value": 67234.50},
            ],
        },
    }))

    assert feed.latest_price == pytest.approx(67234.50)


def test_rtds_feed_ignores_bad_value_without_dropping_connection():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {"symbol": "btcusdt", "timestamp": 1753314088395, "value": ""},
    }))
    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {"symbol": "btcusdt", "timestamp": 1753314088395, "value": "NaN"},
    }))

    assert feed.latest_price is None


def test_rtds_feed_appends_ordered_ticks_and_inserts_late_ticks():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    for ts_ms, value in [
        (1753314088000, 100.0),
        (1753314090000, 102.0),
        (1753314089000, 101.0),
    ]:
        feed._handle_message(json.dumps({
            "topic": "crypto_prices",
            "type": "update",
            "payload": {"symbol": "btcusdt", "timestamp": ts_ms, "value": value},
        }))

    assert feed.price_at_or_before(1753314088.5) == pytest.approx(100.0)
    assert feed.price_at_or_before(1753314089.5) == pytest.approx(101.0)
    assert feed.latest_price == pytest.approx(102.0)


def test_rtds_feed_ignores_other_symbols():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message(json.dumps({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {"symbol": "ethusdt", "timestamp": 1753314088395, "value": 3456.78},
    }))

    assert feed.latest_price is None


@pytest.mark.asyncio
async def test_rtds_subscribe_uses_unfiltered_crypto_stream_for_live_updates():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")
    ws = AsyncMock()
    feed._ws = ws

    await feed._subscribe()

    sent = json.loads(ws.send.await_args.args[0])
    sub = sent["subscriptions"][0]
    assert sub["topic"] == "crypto_prices"
    assert sub["type"] == "update"
    assert "filters" not in sub


def test_rtds_feed_ignores_empty_messages():
    feed = PolymarketRTDSPriceFeed(symbol="btcusdt")

    feed._handle_message("")

    assert feed.latest_price is None
