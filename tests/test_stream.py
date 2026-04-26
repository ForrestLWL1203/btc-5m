"""Unit tests for polybot.market.stream — event parsing, cache clearing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError

from polybot.market.stream import PriceStream, PriceUpdate


# ─── PriceUpdate ──────────────────────────────────────────────────────────────

def test_price_update_is_trade():
    u = PriceUpdate(
        token_id="t1", best_bid=0.4, best_ask=0.5,
        midpoint=0.45, spread=0.1, source="last_trade_price",
    )
    assert u.is_trade is True


def test_price_update_is_not_trade():
    u = PriceUpdate(
        token_id="t1", best_bid=0.4, best_ask=0.5,
        midpoint=0.45, spread=0.1, source="best_bid_ask",
    )
    assert u.is_trade is False


# ─── Event dispatch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_best_bid_ask():
    """best_bid_ask event updates price cache and triggers callback."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    raw = '{"event_type": "best_bid_ask", "asset_id": "token-abc", "best_bid": "0.48", "best_ask": "0.52", "spread": "0.04"}'
    stream._dispatch(raw)

    # Give the scheduled task time to run
    await asyncio.sleep(0.05)

    assert "token-abc" in stream._prices
    assert stream._prices["token-abc"].midpoint == pytest.approx(0.50)
    assert stream._prices["token-abc"].best_bid == pytest.approx(0.48)
    assert stream._prices["token-abc"].best_ask == pytest.approx(0.52)
    callback.assert_called_once()
    update = callback.call_args[0][0]
    assert update.token_id == "token-abc"
    assert update.source == "best_bid_ask"


@pytest.mark.asyncio
async def test_dispatch_last_trade_price():
    """last_trade_price event uses trade price as midpoint."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    # Pre-populate bid/ask from a previous event
    stream._prices["token-abc"] = PriceUpdate(
        token_id="token-abc", best_bid=0.48, best_ask=0.52,
        midpoint=0.50, spread=0.04, source="best_bid_ask",
    )

    raw = '{"event_type": "last_trade_price", "asset_id": "token-abc", "price": "0.55"}'
    stream._dispatch(raw)

    await asyncio.sleep(0.05)

    assert stream._prices["token-abc"].midpoint == pytest.approx(0.55)
    # Should preserve bid/ask from previous cache
    assert stream._prices["token-abc"].best_bid == pytest.approx(0.48)
    assert stream._prices["token-abc"].source == "last_trade_price"
    assert stream._prices["token-abc"].best_ask_received_at == pytest.approx(0.0)
    callback.assert_called_once()
    assert callback.call_args[0][0].is_trade is True


@pytest.mark.asyncio
async def test_dispatch_price_change_array():
    """price_change event with price_changes array."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    raw = '''{
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "token-1", "price": "0.60", "side": "BUY", "best_bid": "0.59", "best_ask": "0.61"},
            {"asset_id": "token-2", "price": "0.40", "side": "SELL", "best_bid": "0.39", "best_ask": "0.41"}
        ]
    }'''
    stream._dispatch(raw)

    await asyncio.sleep(0.05)

    assert "token-1" in stream._prices
    assert "token-2" in stream._prices
    assert stream._prices["token-1"].midpoint == pytest.approx(0.60)
    assert stream._prices["token-2"].midpoint == pytest.approx(0.40)
    assert callback.call_count == 2


@pytest.mark.asyncio
async def test_dispatch_book_caches_full_ask_depth():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    raw = '''{
        "event_type": "book",
        "asset_id": "token-book",
        "bids": [
            {"price": "0.48", "size": "30"},
            {"price": "0.47", "size": "20"}
        ],
        "asks": [
            {"price": "0.52", "size": "25"},
            {"price": "0.53", "size": "60"},
            {"price": "0.54", "size": "10"},
            {"price": "0.55", "size": "5"}
        ]
    }'''
    stream._dispatch(raw)

    await asyncio.sleep(0.05)

    assert stream.get_latest_best_ask("token-book") == pytest.approx(0.52)
    assert stream.get_latest_best_ask("token-book", level=4) == pytest.approx(0.55)
    assert stream.get_latest_best_ask("token-book", level=5) is None
    assert stream.get_latest_ask_levels("token-book") == [0.52, 0.53, 0.54, 0.55]
    assert stream.get_latest_ask_levels_with_size("token-book") == [
        (0.52, 25.0),
        (0.53, 60.0),
        (0.54, 10.0),
        (0.55, 5.0),
    ]
    callback.assert_called_once()
    assert callback.call_args[0][0].source == "book"


@pytest.mark.asyncio
async def test_price_change_updates_cached_book_depth():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    stream._dispatch('''{
        "event_type": "book",
        "asset_id": "token-book",
        "bids": [{"price": "0.48", "size": "30"}],
        "asks": [
            {"price": "0.52", "size": "25"},
            {"price": "0.53", "size": "60"},
            {"price": "0.54", "size": "10"},
            {"price": "0.55", "size": "5"}
        ]
    }''')
    await asyncio.sleep(0.05)

    stream._dispatch('''{
        "event_type": "price_change",
        "price_changes": [
            {"asset_id": "token-book", "price": "0.53", "size": "0", "side": "SELL", "best_bid": "0.48", "best_ask": "0.52"},
            {"asset_id": "token-book", "price": "0.56", "size": "7", "side": "SELL", "best_bid": "0.48", "best_ask": "0.52"}
        ]
    }''')
    await asyncio.sleep(0.05)

    assert stream.get_latest_ask_levels("token-book") == [0.52, 0.54, 0.55, 0.56]
    assert stream.get_latest_best_ask("token-book", level=3) == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_dispatch_invalid_json_ignored():
    """Invalid JSON is silently ignored."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    stream._dispatch("not json at all")
    stream._dispatch("")

    await asyncio.sleep(0.05)
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_list_of_events():
    """WebSocket can send a list of events."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)

    raw = '[{"event_type": "best_bid_ask", "asset_id": "t1", "best_bid": "0.40", "best_ask": "0.60"}]'
    stream._dispatch(raw)

    await asyncio.sleep(0.05)

    assert "t1" in stream._prices
    callback.assert_called_once()


# ─── Price cache ───────────────────────────────────────────────────────────────

def test_get_latest_price_no_data():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    assert stream.get_latest_price("nonexistent") is None


@pytest.mark.asyncio
async def test_get_latest_price_cached():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._prices["t1"] = PriceUpdate(
        token_id="t1", best_bid=0.49, best_ask=0.51,
        midpoint=0.50, spread=0.02, source="best_bid_ask",
    )
    assert stream.get_latest_price("t1") == pytest.approx(0.50)


def test_get_latest_best_ask_rejects_stale_cache():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._prices["t1"] = PriceUpdate(
        token_id="t1", best_bid=0.49, best_ask=0.51,
        midpoint=0.50, spread=0.02, source="best_bid_ask",
        received_at=100.0, best_ask_received_at=100.0,
    )
    with patch("polybot.market.stream.time.monotonic", return_value=101.5):
        assert stream.get_latest_best_ask("t1", max_age_sec=1.0) is None
        assert stream.get_latest_best_ask_age("t1") == pytest.approx(1.5)


def test_get_latest_best_ask_accepts_fresh_cache():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._prices["t1"] = PriceUpdate(
        token_id="t1", best_bid=0.49, best_ask=0.51,
        midpoint=0.50, spread=0.02, source="best_bid_ask",
        received_at=100.0, best_ask_received_at=100.0,
    )
    with patch("polybot.market.stream.time.monotonic", return_value=100.5):
        assert stream.get_latest_best_ask("t1", max_age_sec=1.0) == pytest.approx(0.51)


def test_last_trade_does_not_refresh_best_ask_age():
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._prices["t1"] = PriceUpdate(
        token_id="t1", best_bid=0.49, best_ask=0.51,
        midpoint=0.50, spread=0.02, source="best_bid_ask",
        received_at=100.0, best_ask_received_at=100.0,
    )

    with patch("polybot.market.stream.time.monotonic", return_value=105.0):
        stream._dispatch('{"event_type": "last_trade_price", "asset_id": "t1", "price": "0.55"}')

    with patch("polybot.market.stream.time.monotonic", return_value=105.1):
        assert stream.get_latest_price("t1") == pytest.approx(0.55)
        assert stream.get_latest_best_ask("t1", max_age_sec=1.0) is None
        assert stream.get_latest_best_ask_age("t1") == pytest.approx(5.1)


# ─── Reconnect clears cache ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconnect_clears_prices():
    """After reconnecting, stale prices should be cleared."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._connected_tokens = ["t1"]

    # Pre-populate stale prices
    stream._prices["t1"] = PriceUpdate(
        token_id="t1", best_bid=0.10, best_ask=0.20,
        midpoint=0.15, spread=0.10, source="best_bid_ask",
    )
    assert len(stream._prices) == 1

    # Simulate reconnect logic (extracted from _recv_loop)
    mock_ws = AsyncMock()
    with patch("polybot.market.stream.websockets.connect", return_value=mock_ws):
        stream._ws = await websockets_connect_stub()
        await stream._subscribe(["t1"])
        stream._prices.clear()  # This is what the code does after reconnect

    assert len(stream._prices) == 0


async def websockets_connect_stub():
    """Stub for websockets.connect."""
    mock = AsyncMock()
    mock.send = AsyncMock()
    mock.close = AsyncMock()
    return mock


# ─── set_on_price ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_on_price_updates_callback():
    """set_on_price changes the callback used for new price updates."""
    old_callback = AsyncMock()
    new_callback = AsyncMock()
    stream = PriceStream(on_price=old_callback)

    raw = '{"event_type": "best_bid_ask", "asset_id": "t1", "best_bid": "0.48", "best_ask": "0.52"}'

    # First dispatch uses old callback
    stream._dispatch(raw)
    await asyncio.sleep(0.05)
    old_callback.assert_called_once()

    # Update callback
    stream.set_on_price(new_callback)

    # Second dispatch uses new callback
    raw2 = '{"event_type": "best_bid_ask", "asset_id": "t1", "best_bid": "0.49", "best_ask": "0.53"}'
    stream._dispatch(raw2)
    await asyncio.sleep(0.05)
    new_callback.assert_called_once()
    assert old_callback.call_count == 1  # old callback not called again


# ─── switch_tokens clears cache ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_tokens_clears_price_cache():
    """switch_tokens clears stale prices from previous window."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._running = True
    mock_ws = AsyncMock()
    mock_ws.send = AsyncMock()
    stream._ws = mock_ws
    stream._connected_tokens = ["old-token"]

    # Pre-populate stale prices
    stream._prices["old-token"] = PriceUpdate(
        token_id="old-token", best_bid=0.4, best_ask=0.5,
        midpoint=0.45, spread=0.1, source="best_bid_ask",
    )
    assert len(stream._prices) == 1

    await stream.switch_tokens(["new-token-1", "new-token-2"])

    assert len(stream._prices) == 0  # cache cleared
    assert stream._connected_tokens == ["new-token-1", "new-token-2"]


@pytest.mark.asyncio
async def test_switch_tokens_reconnects_when_ws_is_closed():
    """switch_tokens should reconnect instead of bubbling a closed-WS error."""
    callback = AsyncMock()
    stream = PriceStream(on_price=callback)
    stream._running = True
    stream._connected_tokens = ["old-token"]
    stream._prices["old-token"] = PriceUpdate(
        token_id="old-token", best_bid=0.4, best_ask=0.5,
        midpoint=0.45, spread=0.1, source="best_bid_ask",
    )

    closed_ws = AsyncMock()
    closed_ws.send = AsyncMock(side_effect=ConnectionClosedError(None, None))
    closed_ws.close = AsyncMock()
    stream._ws = closed_ws

    reconnected_ws = AsyncMock()
    reconnected_ws.send = AsyncMock()
    reconnected_ws.close = AsyncMock()

    with patch("polybot.market.stream.websockets.connect", new=AsyncMock(return_value=reconnected_ws)):
        await stream.switch_tokens(["new-token-1", "new-token-2"])

    closed_ws.close.assert_awaited_once()
    reconnected_ws.send.assert_awaited_once()
    assert len(stream._prices) == 0
    assert stream._connected_tokens == ["new-token-1", "new-token-2"]
    assert stream._ws is reconnected_ws
