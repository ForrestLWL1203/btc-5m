"""Unit tests for the current paired-window monitoring flow."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import logging

from polybot.core.state import MonitorState
from polybot.market.market import MarketWindow
from polybot.market.stream import PriceUpdate
from polybot.strategies.crowd_m1 import CrowdM1Strategy
from polybot.strategies.paired_window import PairedWindowStrategy
from polybot.trade_config import TradeConfig
from polybot.trading.monitor import (
    _cap_limited_depth_quote,
    _handle_opening_price,
    _log_window_summary,
    _maybe_handle_stop_loss,
    _sync_holding_balance_after_buy,
    _monitor_single_window,
    _on_price_update,
    _process_trade_result,
    _sanitize_next_window,
    monitor_window,
)
from polybot.trading.trading import OrderResult


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
    strategy.max_entry_price = 0.65
    return strategy


def _mock_crowd_strategy() -> MagicMock:
    strategy = MagicMock(spec=CrowdM1Strategy)
    strategy.should_buy = MagicMock(return_value=False)
    strategy.get_side.return_value = "up"
    strategy.set_market_snapshot = MagicMock()
    strategy.preload_open_btc = AsyncMock()
    strategy.max_entry_price = 0.75
    return strategy


def _mock_paired_strategy() -> MagicMock:
    strategy = MagicMock(spec=PairedWindowStrategy)
    strategy.should_buy = MagicMock(return_value=True)
    strategy.get_side.return_value = "up"
    strategy.preload_open_btc = AsyncMock()
    strategy.max_entry_price = 0.75
    return strategy


def _bid_book(start: float = 0.41, count: int = 12) -> list[tuple[float, float]]:
    return [(round(start - i * 0.01, 2), 100.0) for i in range(count)]


def test_sanitize_next_window_rejects_same_window():
    window = _make_window()
    assert _sanitize_next_window(window, window) is None


def test_process_trade_result_triggers_dollar_loss_pause():
    state = MonitorState()

    _process_trade_result(
        state,
        direction_correct=False,
        realized_pnl=-1.5,
        trade_config=_tc(
            consecutive_loss_amount_limit=3.0,
            consecutive_loss_pause_windows=2,
        ),
    )
    assert state.windows_to_skip == 0
    assert state.consecutive_loss_amount == pytest.approx(1.5)

    _process_trade_result(
        state,
        direction_correct=False,
        realized_pnl=-1.5,
        trade_config=_tc(
            consecutive_loss_amount_limit=3.0,
            consecutive_loss_pause_windows=2,
        ),
    )
    assert state.windows_to_skip == 2
    assert state.consecutive_loss_amount == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_stop_loss_sells_with_bid_depth_inside_time_band():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=1.3889,
        entry_amount=1.0,
        entry_price=0.72,
        entry_avg_price=0.72,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.38, 12)
    ws.get_latest_best_bid_age.return_value = 0.01
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_sell_bid_level=9,
        stop_loss_retry_count=3,
        stop_loss_min_sell_price=0.20,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor.get_token_balance", return_value=1.522), \
         patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        mock_sell.return_value = OrderResult(
            success=True,
            order_id="sell-1",
            filled_size=1.522,
            avg_price=0.33,
        )
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "up-token-123",
            False,
            trade_config,
            "up",
        )

    mock_sell.assert_awaited_once()
    assert mock_sell.await_args.args[:2] == ("up-token-123", pytest.approx(1.522))
    assert mock_sell.await_args.kwargs["price_hint"] == pytest.approx(0.35)
    assert mock_sell.await_args.kwargs["retry_count"] == 3
    assert state.stop_loss_triggered is True
    assert state.exit_triggered is True
    assert state.bought is False
    assert state.holding_size == pytest.approx(0.0)
    assert state.daily_realized_pnl == pytest.approx(1.522 * 0.33 - 1.0)


@pytest.mark.asyncio
async def test_stop_loss_does_not_trigger_from_deep_book_price_only():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=1.7,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
    )
    ws = MagicMock()
    # Level 1 is well above the 0.38 trigger, but deeper levels cross below it.
    # This must not trigger stop-loss; deep levels are only for execution once
    # the top bid has actually reached the stop area.
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.56, 20)
    ws.get_latest_best_bid_age.return_value = 0.01
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_sell_bid_level=10,
        stop_loss_trigger_price=0.38,
    )

    with patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "down-token-123",
            False,
            trade_config,
            "down",
        )

    mock_sell.assert_not_called()
    assert state.stop_loss_triggered is False


@pytest.mark.asyncio
async def test_stop_loss_logs_check_when_bid_above_trigger(caplog):
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=1.7,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.56, 20)
    ws.get_latest_best_bid_age.return_value = 0.01
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_trigger_price=0.38,
    )

    with patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _maybe_handle_stop_loss(
                window,
                state,
                ws,
                "down-token-123",
                False,
                trade_config,
                "down",
            )

    mock_sell.assert_not_called()
    checks = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "STOP_LOSS_CHECK"
    ]
    assert checks
    assert checks[0]["reason"] == "bid_above_stop"
    assert checks[0]["best_bid_level_1"] == pytest.approx(0.56)
    assert checks[0]["stop_price"] == pytest.approx(0.38)


@pytest.mark.asyncio
async def test_stop_loss_disabled_for_low_entry_price():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 180, end_epoch=int(now) + 120)
    state = _make_state(
        bought=True,
        holding_size=2.381,
        entry_amount=1.0,
        entry_price=0.42,
        entry_avg_price=0.42,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.65, 12)
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_disable_below_entry_price=0.45,
    )

    with patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "up-token-123",
            False,
            trade_config,
            "up",
        )

    mock_sell.assert_not_called()
    assert state.stop_loss_triggered is False


@pytest.mark.asyncio
async def test_stop_loss_ignores_early_window():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 100, end_epoch=int(now) + 200)
    state = _make_state(
        bought=True,
        holding_size=1.3889,
        entry_amount=1.0,
        entry_price=0.72,
        entry_avg_price=0.72,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.41, 12)
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
    )

    with patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "up-token-123",
            False,
            trade_config,
            "up",
        )

    mock_sell.assert_not_called()
    assert state.stop_loss_triggered is False


@pytest.mark.asyncio
async def test_on_price_buy_in_range():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.59

    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(), strategy=strategy, side="up")
        mock_buy.assert_called_once()
        assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.60)


@pytest.mark.asyncio
async def test_on_price_injects_market_snapshot_for_dynamic_strategy():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.should_buy.return_value = False
    strategy.set_market_snapshot = MagicMock()
    ws = MagicMock()
    ws.get_latest_price.side_effect = lambda token: {
        "up-token-123": 0.62,
        "down-token-456": 0.38,
    }.get(token)
    ws.get_latest_best_ask.side_effect = lambda token: {
        "up-token-123": 0.63,
        "down-token-456": 0.39,
    }.get(token)

    update = _make_update("up-token-123", midpoint=0.62)

    await _on_price_update(
        update,
        window,
        state,
        ws=ws,
        dry_run=True,
        trade_config=_tc(),
        strategy=strategy,
        side="up",
    )

    strategy.set_market_snapshot.assert_called_once_with(
        up_mid=pytest.approx(0.62),
        down_mid=pytest.approx(0.38),
        up_best_ask=pytest.approx(0.63),
        down_best_ask=pytest.approx(0.39),
        up_best_ask_age_sec=0.0,
        down_best_ask_age_sec=None,
    )


@pytest.mark.asyncio
async def test_on_price_snapshot_uses_current_update_when_cache_lags():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.should_buy.return_value = False
    strategy.set_market_snapshot = MagicMock()
    ws = MagicMock()
    ws.get_latest_price.side_effect = lambda token: {
        "up-token-123": None,
        "down-token-456": 0.38,
    }.get(token)
    ws.get_latest_best_ask.side_effect = lambda token: {
        "up-token-123": None,
        "down-token-456": 0.39,
    }.get(token)

    update = _make_update("up-token-123", midpoint=0.62)

    await _on_price_update(
        update,
        window,
        state,
        ws=ws,
        dry_run=True,
        trade_config=_tc(),
        strategy=strategy,
        side="up",
    )

    strategy.set_market_snapshot.assert_called_once_with(
        up_mid=pytest.approx(0.62),
        down_mid=pytest.approx(0.38),
        up_best_ask=pytest.approx(0.63),
        down_best_ask=pytest.approx(0.39),
        up_best_ask_age_sec=0.0,
        down_best_ask_age_sec=None,
    )


@pytest.mark.asyncio
async def test_on_price_crowd_strategy_accepts_down_token_update_for_entry_check():
    window = _make_window()
    state = _make_state()
    strategy = _mock_crowd_strategy()
    ws = MagicMock()
    ws.get_latest_price.side_effect = lambda token: {
        "up-token-123": 0.38,
        "down-token-456": 0.62,
    }.get(token)
    ws.get_latest_best_ask.side_effect = lambda token: {
        "up-token-123": 0.39,
        "down-token-456": 0.63,
    }.get(token)

    update = _make_update("down-token-456", midpoint=0.62)

    await _on_price_update(
        update,
        window,
        state,
        ws=ws,
        dry_run=True,
        trade_config=_tc(),
        strategy=strategy,
        side="up",
    )

    strategy.set_market_snapshot.assert_called_once_with(
        up_mid=pytest.approx(0.38),
        down_mid=pytest.approx(0.62),
        up_best_ask=pytest.approx(0.39),
        down_best_ask=pytest.approx(0.63),
        up_best_ask_age_sec=None,
        down_best_ask_age_sec=0.0,
    )
    strategy.should_buy.assert_called_once_with(pytest.approx(0.62), state)


@pytest.mark.asyncio
async def test_on_price_paired_strategy_ignores_down_token_before_entry():
    window = _make_window()
    state = _make_state()
    strategy = _mock_paired_strategy()
    ws = MagicMock()

    update = _make_update("down-token-456", midpoint=0.62)

    await _on_price_update(
        update,
        window,
        state,
        ws=ws,
        dry_run=True,
        trade_config=_tc(),
        strategy=strategy,
        side="up",
    )

    strategy.should_buy.assert_not_called()


@pytest.mark.asyncio
async def test_monitor_window_allows_dynamic_strategy_without_initial_side():
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

    strategy = _mock_crowd_strategy()
    strategy.get_side.return_value = None
    strategy.dynamic_side = True

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy,
        )

    assert next_win is None
    assert returned_ws is mock_ws
    assert monitored is True


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
async def test_max_entries_per_window_blocks_second_entry():
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

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
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

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    buy_signal = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "BUY_SIGNAL")
    assert buy_signal.event_data["side"] == "DOWN"
    assert buy_signal.event_data["price"] == pytest.approx(0.61)
    assert buy_signal.event_data["target_entry_ask"] == pytest.approx(0.61)
    assert buy_signal.event_data["best_ask_level_1"] == pytest.approx(0.61)
    assert buy_signal.event_data["signal_price"] == pytest.approx(0.385)


@pytest.mark.asyncio
async def test_crowd_buy_signal_uses_leading_ask_as_signal_price(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_crowd_strategy()
    ws = MagicMock()
    ws.get_latest_price.side_effect = lambda token: {
        "up-token-123": 0.38,
        "down-token-456": 0.62,
    }.get(token)
    ws.get_latest_best_ask.side_effect = lambda token, **_kwargs: {
        "up-token-123": 0.39,
        "down-token-456": 0.63,
    }.get(token)
    ws.get_latest_best_ask_age.return_value = 0.05
    strategy.should_buy.side_effect = (
        lambda _price, state_obj:
        setattr(state_obj, "target_side", "down")
        or setattr(state_obj, "signal_reference_price", 0.63)
        or setattr(state_obj, "target_max_entry_price", 0.75)
        or True
    )

    update = _make_update("down-token-456", midpoint=0.62)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(
                update,
                window,
                state,
                ws=ws,
                dry_run=True,
                trade_config=_tc(amount=1.0),
                strategy=strategy,
                side="up",
            )

    buy_signal = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "BUY_SIGNAL")
    assert buy_signal.event_data["side"] == "DOWN"
    assert buy_signal.event_data["signal_price"] == pytest.approx(0.63)


@pytest.mark.asyncio
async def test_on_price_skips_when_target_best_ask_outside_band():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.66
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_allows_low_target_best_ask():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.42
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.43)


@pytest.mark.asyncio
async def test_on_price_excludes_first_book_level_from_depth():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 10.0),  # enough by itself, but intentionally ignored
        (0.64, 0.1),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_entry_ask_level_caps_deepest_price_hint_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 0.1),   # level 1 ignored for fillability
        (0.61, 10.0),  # already covers amount within configured max level
        (0.62, 10.0),
        (0.63, 10.0),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    ws.get_latest_best_ask_age.return_value = 0.001
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(
            update,
            window,
            state,
            ws=ws,
            dry_run=True,
            trade_config=_tc(amount=1.0, entry_ask_level=4),
            strategy=strategy,
            side="up",
        )

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["target_entry_ask"] == pytest.approx(0.61)
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.62)
    assert mock_buy.await_args.kwargs["entry_ask_level"] == 4


@pytest.mark.asyncio
async def test_on_price_low_top_ask_allows_deeper_max_price_hint_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.72
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.58, 1.0),
        (0.59, 0.1),
        (0.60, 10.0),
        (0.61, 10.0),
        (0.62, 10.0),
        (0.63, 10.0),
        (0.64, 10.0),
        (0.65, 10.0),
        (0.66, 10.0),
    ]
    ws.get_latest_best_ask.return_value = 0.58
    ws.get_latest_best_ask_age.return_value = 0.001
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=7,
        low_price_threshold=0.60,
        low_price_entry_ask_level=9,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(
            update,
            window,
            state,
            ws=ws,
            dry_run=True,
            trade_config=trade_config,
            strategy=strategy,
            side="up",
        )

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["target_entry_ask"] == pytest.approx(0.60)
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.61)
    assert mock_buy.await_args.kwargs["entry_ask_level"] == 9


@pytest.mark.asyncio
async def test_on_price_non_low_top_ask_uses_base_price_hint_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.72
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 1.0),
        (0.61, 10.0),
        (0.62, 10.0),
        (0.63, 10.0),
        (0.64, 10.0),
        (0.65, 10.0),
        (0.66, 10.0),
        (0.67, 10.0),
        (0.68, 10.0),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    ws.get_latest_best_ask_age.return_value = 0.001
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=7,
        low_price_threshold=0.60,
        low_price_entry_ask_level=9,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(
            update,
            window,
            state,
            ws=ws,
            dry_run=True,
            trade_config=trade_config,
            strategy=strategy,
            side="up",
        )

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["target_entry_ask"] == pytest.approx(0.61)
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.62)
    assert mock_buy.await_args.kwargs["entry_ask_level"] == 7


def test_cap_depth_quote_uses_dynamic_entry_level_from_leading_ask():
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.65, 0.1),
        (0.66, 0.1),
        (0.67, 0.1),
        (0.68, 10.0),
        (0.69, 10.0),
    ]
    ws.get_latest_best_ask.return_value = 0.65
    ws.get_latest_best_ask_age.return_value = 0.001
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=9,
        dynamic_entry_levels=[
            (0.64, 5),
            (0.68, 4),
            (0.72, 2),
            (0.75, 1),
        ],
        max_slippage_from_best_ask=0.04,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        quote = _cap_limited_depth_quote(
            ws,
            "up-token-123",
            trade_config.amount,
            0.75,
            max_entry_level=trade_config.base_entry_ask_level(),
            dynamic_entry_levels=trade_config.dynamic_entry_levels,
            max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
        )

    assert quote.enough is True
    assert quote.entry_ask_level == 4
    assert quote.price == pytest.approx(0.68)
    assert quote.price_hint == pytest.approx(0.69)


def test_cap_depth_quote_skips_when_dynamic_level_exceeds_slippage_cap():
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.65, 0.1),
        (0.66, 0.1),
        (0.67, 0.1),
        (0.68, 0.1),
        (0.70, 10.0),
    ]
    ws.get_latest_best_ask.return_value = 0.65
    ws.get_latest_best_ask_age.return_value = 0.001
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=9,
        dynamic_entry_levels=[
            (0.64, 5),
            (0.68, 4),
            (0.72, 2),
            (0.75, 1),
        ],
        max_slippage_from_best_ask=0.04,
    )

    quote = _cap_limited_depth_quote(
        ws,
        "up-token-123",
        trade_config.amount,
        0.75,
        max_entry_level=trade_config.base_entry_ask_level(),
        dynamic_entry_levels=trade_config.dynamic_entry_levels,
        max_slippage_from_best_ask=trade_config.max_slippage_from_best_ask,
    )

    assert quote.enough is False
    assert quote.entry_ask_level == 4
    assert quote.price is None
    assert quote.cap_notional == pytest.approx(0.201)


@pytest.mark.asyncio
async def test_entry_depth_skip_logs_once_for_repeated_same_quote(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 10.0),
        (0.64, 0.1),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    ws.get_latest_best_ask_age.return_value = 0.01
    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_not_called()
    depth_skips = [
        record for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "ENTRY_DEPTH_SKIP"
    ]
    assert len(depth_skips) == 1


@pytest.mark.asyncio
async def test_entry_depth_skip_throttles_depth_notional_churn(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.side_effect = [
        [(0.66, 10.0), (0.67, 10.0)],
        [(0.66, 100.0), (0.67, 100.0)],
    ]
    ws.get_latest_best_ask.return_value = 0.66
    ws.get_latest_best_ask_age.return_value = 0.01
    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_not_called()
    depth_skips = [
        record for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "ENTRY_DEPTH_SKIP"
    ]
    assert len(depth_skips) == 1


def test_window_summary_includes_depth_skip_aggregate(caplog):
    window = _make_window()
    state = MonitorState(
        depth_skip_count=12,
        depth_skip_last_reason="cap-limited book depth insufficient",
        depth_skip_min_best_ask=0.61,
        depth_skip_max_best_ask=0.74,
        depth_skip_min_entry_ask=0.66,
        depth_skip_max_entry_ask=0.68,
        depth_skip_max_notional=381.0224,
    )

    with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
        _log_window_summary(state, window, dry_run=False)

    summary = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "SUMMARY")
    assert summary.event_data["depth_skip_count"] == 12
    assert summary.event_data["depth_skip_min_best_ask"] == pytest.approx(0.61)
    assert summary.event_data["depth_skip_max_best_ask"] == pytest.approx(0.74)
    assert summary.event_data["depth_skip_max_notional"] == pytest.approx(381.0224)


@pytest.mark.asyncio
async def test_on_price_uses_configured_entry_ask_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.72
    ws.get_latest_best_ask_age.return_value = 0.001

    def _normal_ok_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_strength = 2.1
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _normal_ok_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=2,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(
            update,
            window,
            state,
            ws=ws,
            dry_run=True,
            trade_config=trade_config,
            strategy=strategy,
            side="up",
        )

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.73)


@pytest.mark.asyncio
async def test_on_price_does_not_fallback_to_top_ask_for_deeper_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = None
    ws.get_latest_best_ask_age.return_value = None

    def _up_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_strength = 2.1
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _up_signal
    update = _make_update("up-token-123", midpoint=0.68)
    update.best_ask = 0.60
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=2,
    )

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(
            update,
            window,
            state,
            ws=ws,
            dry_run=True,
            trade_config=trade_config,
            strategy=strategy,
            side="up",
        )

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_rechecks_entry_band_only_when_target_best_ask_changes(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.side_effect = [
        [(0.64, 0.1), (0.66, 10.0)],
        [(0.64, 0.1), (0.66, 10.0)],
        [(0.64, 0.1), (0.64, 10.0)],
    ]
    ws.get_latest_best_ask.side_effect = lambda _token, *_, **__: 0.64
    ws.get_latest_best_ask_age.return_value = 0.001
    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    assert mock_buy.await_count == 1
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.65)
    assert all(
        getattr(record, "event_data", {}).get("action") != "BUY_SKIP"
        for record in caplog.records
    )


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
async def test_handle_opening_price_uses_strength_amount_tier():
    window = _make_window()
    state = MonitorState()
    state.target_side = "up"
    state.target_entry_price = 0.60
    state.target_signal_strength = 2.1

    await _handle_opening_price(
        window,
        state,
        "up-token-123",
        0.60,
        dry_run=True,
        trade_config=_tc(
            amount=1.0,
            amount_tiers=[(2.0, 1.5)],
        ),
        strategy=None,
        side="up",
    )

    assert state.entry_amount == pytest.approx(1.5)
    assert state.holding_size == pytest.approx(1.5 / 0.60)


@pytest.mark.asyncio
async def test_sync_holding_balance_after_buy_updates_live_shares():
    window = _make_window()
    state = _make_state(
        bought=True,
        holding_size=1.3889,
        entry_count=1,
    )

    with patch("polybot.trading.monitor.get_token_balance", return_value=1.522):
        await _sync_holding_balance_after_buy(
            state,
            "up-token-123",
            window,
            "up",
            entry_count=1,
            delay_sec=0,
        )

    assert state.holding_size == pytest.approx(1.522)


@pytest.mark.asyncio
async def test_monitor_window_reuses_existing_ws():
    import datetime

    utc = datetime.timezone.utc
    past_start = int(time.time()) - 100
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

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None):
        _, returned_ws, _ = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            strategy=_mock_strategy(),
        )

    mock_ws.set_on_price.assert_called_once()
    mock_ws.switch_tokens.assert_called_once_with(["up-tok", "down-tok"])
    assert returned_ws is mock_ws


@pytest.mark.asyncio
async def test_monitor_window_resets_started_before_preopen_switch():
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now + 1, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 301, tz=utc),
        slug="test",
    )

    state = MonitorState()
    state.started = True
    state.target_side = "down"
    state.target_entry_price = 0.64
    state.target_max_entry_price = 0.75
    state.last_entry_check_side = "down"
    state.last_entry_check_best_ask = 0.64
    state.latest_midpoint = 0.9

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.get_latest_price = MagicMock(return_value=0.50)

    async def _switch_tokens(_token_ids):
        assert state.started is False
        assert state.target_side is None
        assert state.target_entry_price is None
        assert state.target_max_entry_price is None
        assert state.last_entry_check_side is None
        assert state.last_entry_check_best_ask is None
        assert state.latest_midpoint is None

    mock_ws.switch_tokens = AsyncMock(side_effect=_switch_tokens)

    strategy = _mock_strategy()
    strategy.should_buy.return_value = False

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy, state=state,
        )

    assert state.started is True
    assert next_win is None
    assert returned_ws is mock_ws
    assert monitored is True


@pytest.mark.asyncio
async def test_monitor_single_window_actively_evaluates_snapshot_strategy_at_entry_time(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 120, end_epoch=now + 180)
    state = _make_state()
    state.started = True
    ws = MagicMock()
    ws.get_latest_price.side_effect = lambda token: {
        "up-token-123": 0.62,
        "down-token-456": 0.38,
    }.get(token)
    ws.get_latest_best_ask.side_effect = lambda token: {
        "up-token-123": 0.63,
        "down-token-456": 0.39,
    }.get(token)
    ws.get_latest_best_ask_age.side_effect = lambda token: {
        "up-token-123": 0.12,
        "down-token-456": 0.34,
    }.get(token)

    strategy = _mock_crowd_strategy()
    strategy.entry_start_remaining_sec = 180
    strategy.entry_end_remaining_sec = 175
    strategy.should_buy.return_value = False

    fake_now = {"value": now}
    with patch("polybot.trading.monitor.time.time", side_effect=lambda: fake_now["value"]), \
         patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        async def _after_one_sleep(_seconds):
            fake_now["value"] = window.end_epoch

        mock_sleep.side_effect = _after_one_sleep

        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _monitor_single_window(
                window,
                state,
                ws,
                dry_run=True,
                trade_config=_tc(),
                strategy=strategy,
                prefetch_next_window=False,
            )

    strategy.set_market_snapshot.assert_called_once_with(
        up_mid=pytest.approx(0.62),
        down_mid=pytest.approx(0.38),
        up_best_ask=pytest.approx(0.63),
        down_best_ask=pytest.approx(0.39),
        up_best_ask_age_sec=0.12,
        down_best_ask_age_sec=0.34,
    )
    strategy.should_buy.assert_called_once_with(pytest.approx(0.62), state)
    checks = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "SNAPSHOT_ENTRY_CHECK"
    ]
    assert checks
    assert checks[0]["up_best_ask"] == pytest.approx(0.63)
    assert checks[0]["down_best_ask"] == pytest.approx(0.39)
    assert checks[0]["up_best_ask_age_ms"] == 120
    assert checks[0]["down_best_ask_age_ms"] == 340


@pytest.mark.asyncio
async def test_monitor_single_window_actively_checks_stop_loss_without_price_update():
    now = int(time.time())
    window = _make_window(start_epoch=now - 230, end_epoch=now + 70)
    state = _make_state(
        bought=True,
        holding_size=1.5,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
        target_side="down",
    )
    ws = MagicMock()
    strategy = _mock_strategy()

    fake_now = {"value": now}
    with patch("polybot.trading.monitor.time.time", side_effect=lambda: fake_now["value"]), \
         patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("polybot.trading.monitor._maybe_handle_stop_loss", new_callable=AsyncMock) as mock_stop:
        async def _after_one_sleep(_seconds):
            fake_now["value"] = window.end_epoch

        mock_sleep.side_effect = _after_one_sleep

        await _monitor_single_window(
            window,
            state,
            ws,
            dry_run=True,
            trade_config=_tc(stop_loss_enabled=True),
            strategy=strategy,
            prefetch_next_window=False,
        )

    mock_stop.assert_awaited()
    assert mock_stop.await_args.args[:4] == (window, state, ws, window.down_token)
    assert mock_stop.await_args.args[6] == "down"


@pytest.mark.asyncio
async def test_on_price_bought_position_skips_stop_loss_before_time_band():
    now = int(time.time())
    window = _make_window(start_epoch=now - 100, end_epoch=now + 200)
    state = _make_state(
        bought=True,
        holding_size=1.5,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
        target_side="down",
    )
    strategy = _mock_crowd_strategy()
    update = _make_update("down-token-456", midpoint=0.62)

    with patch("polybot.trading.monitor.time.time", return_value=now), \
         patch("polybot.trading.monitor._maybe_handle_stop_loss", new_callable=AsyncMock) as mock_stop:
        await _on_price_update(
            update,
            window,
            state,
            ws=MagicMock(),
            dry_run=True,
            trade_config=_tc(stop_loss_enabled=True),
            strategy=strategy,
            side="up",
        )

    mock_stop.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_bought_position_logs_stop_loss_prewarm_freshness(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 176, end_epoch=now + 124)
    state = _make_state(
        bought=True,
        holding_size=1.5,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
        target_side="down",
    )
    strategy = _mock_crowd_strategy()
    update = _make_update("down-token-456", midpoint=0.62)
    ws = MagicMock()
    ws.get_latest_best_bid_age.return_value = 0.456

    with patch("polybot.trading.monitor.time.time", return_value=now), \
         patch("polybot.trading.monitor._maybe_handle_stop_loss", new_callable=AsyncMock) as mock_stop:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(
                update,
                window,
                state,
                ws=ws,
                dry_run=True,
                trade_config=_tc(stop_loss_enabled=True),
                strategy=strategy,
                side="up",
            )

    mock_stop.assert_not_called()
    events = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "STOP_LOSS_BOOK_FRESHNESS"
    ]
    assert events
    assert events[0]["phase"] == "prewarm"
    assert events[0]["best_bid_age_ms"] == 456


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


@pytest.mark.asyncio
async def test_monitor_window_final_round_skips_fallback_lookup():
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
    mock_ws.get_latest_best_ask = MagicMock(return_value=0.59)

    strategy = _mock_strategy()
    strategy.should_buy.return_value = False

    with patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", side_effect=AssertionError("should not fetch next window")):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(rounds=1), strategy=strategy,
            prefetch_next_window=False,
        )

    assert next_win is None
    assert returned_ws is mock_ws
    assert monitored is True


@pytest.mark.asyncio
async def test_monitor_single_window_final_round_does_not_prefetch_next():
    now = int(time.time())
    window = _make_window(start_epoch=now - 10, end_epoch=now - 1)
    state = MonitorState()
    state.started = True

    with patch("polybot.trading.monitor._find_next_window_after", side_effect=AssertionError("should not prefetch next window")):
        next_win = await _monitor_single_window(
            window,
            state,
            ws=None,
            dry_run=True,
            trade_config=_tc(rounds=1),
            strategy=_mock_strategy(),
            series=None,
            side="up",
            prefetch_next_window=False,
        )

    assert next_win is None


@pytest.mark.asyncio
async def test_monitor_single_window_awaits_prefetch_on_expired_window():
    now = int(time.time())
    window = _make_window(start_epoch=now - 310, end_epoch=now - 10)
    next_window = _make_window(start_epoch=now + 1, end_epoch=now + 301)
    state = MonitorState()
    state.started = True

    with patch("polybot.trading.monitor._find_next_window_after", return_value=next_window):
        next_win = await _monitor_single_window(
            window,
            state,
            ws=None,
            dry_run=True,
            trade_config=_tc(),
            strategy=_mock_strategy(),
            series=None,
            side="up",
            prefetch_next_window=True,
        )

    assert next_win is next_window
