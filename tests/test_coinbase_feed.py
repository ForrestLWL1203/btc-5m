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


@pytest.mark.asyncio
async def test_coinbase_fetch_open_at_uses_matching_candle(monkeypatch):
    feed = CoinbasePriceFeed(product_id="BTC-USD")
    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                [1777593540, 61000.0, 60900.0, 60950.0, 1.0],
                [1777593600, 61200.0, 61100.0, 61123.45, 1.0],
            ]

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, params):
            calls.append((url, params, self.timeout))
            return FakeResponse()

    monkeypatch.setattr("polybot.market.coinbase.httpx.AsyncClient", FakeClient)

    price = await feed.fetch_open_at(1777593600)

    assert price == pytest.approx(61123.45)
    assert feed.price_at_or_before(1777593600) == pytest.approx(61123.45)
    assert calls[0][0].endswith("/BTC-USD/candles")
    assert calls[0][1]["granularity"] == 60
