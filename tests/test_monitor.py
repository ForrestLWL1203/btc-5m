"""Unit tests for the current paired-window monitoring flow."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import logging

from polybot.core.state import MonitorState
from polybot.market.market import MarketWindow
from polybot.market.stream import PriceUpdate
from polybot.trade_config import TradeConfig
from polybot.trading.monitor import (
    _handle_opening_price,
    _on_price_update,
    _sanitize_next_window,
    monitor_window,
)


def _make_state(**kwargs) -> MonitorState:
    state = MonitorState(**kwargs)
    state.started = True
    return state


def _make_window(start_epoch: int = 1000, end_epoch: int = 1300) -> MarketWindow:
    import datetime

    utc = datetime.timezone.utc
    return MarketWindow(
        question="Bitcoin Up or Down - Apr 15 9:40AM-9:45AM ET",
        up_token="up-token-123",
        down_token="down-token-456",
        start_time=datetime.datetime.fromtimestamp(start_epoch, tz=utc),
        end_time=datetime.datetime.fromtimestamp(end_epoch, tz=utc),
        slug=f"btc-updown-5m-{start_epoch}",
    )


def _make_update(token_id: str, midpoint: float, source: str = "best_bid_ask") -> PriceUpdate:
    return PriceUpdate(
        token_id=token_id,
        best_bid=midpoint - 0.01,
        best_ask=midpoint + 0.01,
        midpoint=midpoint,
        spread=0.02,
        source=source,
    )


def _tc(**overrides) -> TradeConfig:
    defaults = dict(amount=5.0, max_entries_per_window=None, rounds=None)
    defaults.update(overrides)
    return TradeConfig(**defaults)


def _mock_strategy() -> MagicMock:
    strategy = MagicMock()
    strategy.should_buy = MagicMock(return_value=True)
    strategy.get_side.return_value = "up"
    strategy.preload_open_btc = AsyncMock()
    strategy.min_entry_price = 0.57
    strategy.max_entry_price = 0.65
    return strategy


def test_sanitize_next_window_rejects_same_window():
    window = _make_window()
    assert _sanitize_next_window(window, window) is None


@pytest.mark.asyncio
async def test_on_price_buy_in_range():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.59

    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(), strategy=strategy, side="up")
        mock_buy.assert_called_once()
        assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_on_price_wrong_token_ignored_before_entry():
    window = _make_window()
    state = _make_state()
    ws = MagicMock()

    update = _make_update("some-other-token", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(), strategy=_mock_strategy(), side="up")
        mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_max_entries_per_window_blocks_reentry():
    window = _make_window()
    state = _make_state(entry_count=1)
    strategy = _mock_strategy()
    ws = MagicMock()
    strategy.should_buy.return_value = True
    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(max_entries_per_window=1), strategy=strategy, side="up")

    mock_buy.assert_not_called()
    assert state.buy_blocked_window_cap is True


@pytest.mark.asyncio
async def test_on_price_uses_target_token_best_ask_for_down_entry():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.61
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "down") or setattr(state_obj, "target_entry_price", 0.605) or True

    update = _make_update("up-token-123", midpoint=0.395)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.args[2] == "down-token-456"
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.62)


@pytest.mark.asyncio
async def test_buy_signal_logs_target_price_for_down_entry(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.61
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "down") or setattr(state_obj, "target_entry_price", 0.605) or True

    update = _make_update("up-token-123", midpoint=0.385)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    buy_signal = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "BUY_SIGNAL")
    assert buy_signal.event_data["side"] == "DOWN"
    assert buy_signal.event_data["price"] == pytest.approx(0.61)
    assert buy_signal.event_data["signal_price"] == pytest.approx(0.385)


@pytest.mark.asyncio
async def test_on_price_skips_when_target_best_ask_outside_band():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.min_entry_price = 0.57
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.66
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_handle_opening_price_dry_run_uses_strategy_target_side():
    window = _make_window()
    state = MonitorState()
    state.target_side = "down"
    state.target_entry_price = 0.65

    await _handle_opening_price(
        window,
        state,
        "up-token-123",
        0.35,
        dry_run=True,
        trade_config=_tc(amount=1.0),
        strategy=None,
        side="up",
    )

    assert state.bought is True
    assert state.entry_price == pytest.approx(0.65)
    assert state.holding_size == pytest.approx(1.0 / 0.65)


@pytest.mark.asyncio
async def test_monitor_window_reuses_existing_ws():
    import datetime

    utc = datetime.timezone.utc
    past_start = int(asyncio.get_event_loop().time()) - 100
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(past_start, tz=utc),
        end_time=datetime.datetime.fromtimestamp(past_start + 300, tz=utc),
        slug="test",
    )

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock()
    mock_ws.get_latest_price = MagicMock(return_value=None)

    with patch("polybot.trading.monitor.find_next_window", return_value=None):
        _, returned_ws, _ = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            strategy=_mock_strategy(),
        )

    mock_ws.set_on_price.assert_called_once()
    mock_ws.switch_tokens.assert_called_once_with(["up-tok", "down-tok"])
    assert returned_ws is mock_ws


@pytest.mark.asyncio
async def test_monitor_window_opening_price_does_not_buy_without_signal():
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 1, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 299, tz=utc),
        slug="test",
    )

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock()
    mock_ws.get_latest_price = MagicMock(return_value=0.50)

    strategy = _mock_strategy()
    strategy.should_buy.return_value = False

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy,
        )

    strategy.should_buy.assert_called_once()
    mock_buy.assert_not_called()
    assert next_win is None
    assert returned_ws is mock_ws
    assert monitored is True


@pytest.mark.asyncio
async def test_monitor_window_does_not_skip_inside_strategy_entry_band():
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 100, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 200, tz=utc),
        slug="test",
    )

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock()
    mock_ws.get_latest_price = MagicMock(return_value=0.50)

    strategy = _mock_strategy()
    strategy.should_buy.return_value = False
    strategy.entry_end_remaining_sec = 120

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=False, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy,
        )

    assert next_win is None
    assert returned_ws is mock_ws
    assert monitored is True


@pytest.mark.asyncio
async def test_monitor_window_skips_after_strategy_entry_band_ends():
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 181, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 119, tz=utc),
        slug="test",
    )

    strategy = _mock_strategy()
    strategy.entry_end_remaining_sec = 120

    with patch("polybot.trading.monitor._find_and_preopen_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=False, existing_ws=None,
            trade_config=_tc(), strategy=strategy,
        )

    assert monitored is False
    assert returned_ws is None
    assert next_win is None
