"""Unit tests for the current paired-window monitoring flow."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import logging

from polybot.core.state import MonitorState
from polybot.market.market import MarketWindow
from polybot.market.series import MarketSeries
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
    strategy.entry_start_remaining_sec = 120
    strategy.entry_end_remaining_sec = 115
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
    assert mock_sell.await_args.kwargs["price_hint"] == pytest.approx(0.36)
    assert mock_sell.await_args.kwargs["retry_count"] == 3
    assert state.stop_loss_triggered is True
    assert state.exit_triggered is True
    assert state.bought is False
    assert state.holding_size == pytest.approx(0.0)
    assert state.daily_realized_pnl == pytest.approx(1.522 * 0.33 - 1.0)


@pytest.mark.asyncio
async def test_stop_loss_insufficient_funds_marks_fatal():
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
         patch("polybot.trading.monitor.get_token_balance", return_value=1.3889), \
         patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
        mock_sell.return_value = OrderResult(
            success=False,
            message="INSUFFICIENT_FUNDS: not enough balance / allowance",
        )

        with pytest.raises(RuntimeError, match="INSUFFICIENT_FUNDS"):
            await _maybe_handle_stop_loss(
                window,
                state,
                ws,
                "up-token-123",
                False,
                trade_config,
                "up",
            )

    assert state.fatal_error == "INSUFFICIENT_FUNDS: not enough balance / allowance"
    assert state.stop_loss_attempted is True


@pytest.mark.asyncio
async def test_stop_loss_dry_run_simulates_latency_and_buffered_bid():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=2.0,
        entry_amount=1.0,
        entry_price=0.70,
        entry_avg_price=0.70,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.34, 12)
    ws.get_latest_best_bid_age.return_value = 0.01
    ws.get_latest_best_bid.return_value = 0.33
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_trigger_price=0.35,
        stop_loss_min_sell_price=0.20,
    )

    with patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "up-token-123",
            True,
            trade_config,
            "up",
        )

    mock_sleep.assert_awaited_once_with(0.4)
    assert state.stop_loss_triggered is True
    assert state.exit_triggered is True
    assert state.daily_realized_pnl == pytest.approx((2.0 * 0.28) - 1.0)


@pytest.mark.asyncio
async def test_stop_loss_uses_entry_price_drop_trigger():
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=2.0,
        entry_amount=1.0,
        entry_price=0.70,
        entry_avg_price=0.70,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.return_value = _bid_book(0.45, 12)
    ws.get_latest_best_bid_age.return_value = 0.01
    ws.get_latest_best_bid.return_value = 0.39
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_trigger_price=0.38,
        stop_loss_trigger_drop_pct=0.35,
        stop_loss_min_sell_price=0.20,
    )

    with patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock), \
         patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        await _maybe_handle_stop_loss(
            window,
            state,
            ws,
            "up-token-123",
            True,
            trade_config,
            "up",
        )

    assert state.stop_loss_triggered is True
    assert state.stop_loss_price == pytest.approx(0.455)
    assert state.daily_realized_pnl == pytest.approx(-0.32)


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

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
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
async def test_stop_loss_logs_when_bid_above_trigger(caplog):
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

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
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
    assert len(checks) == 1
    assert checks[0]["reason"] == "bid_above_stop"
    assert checks[0]["best_bid_level_1"] == 0.56
    assert checks[0]["quote_enough"] is True


@pytest.mark.asyncio
async def test_stop_loss_insufficient_depth_logs_once_per_window(caplog):
    now = time.time()
    window = _make_window(start_epoch=int(now) - 240, end_epoch=int(now) + 60)
    state = _make_state(
        bought=True,
        holding_size=1001.0,
        entry_amount=1.0,
        entry_price=0.66,
        entry_avg_price=0.66,
    )
    ws = MagicMock()
    ws.get_latest_bid_levels_with_size.side_effect = [
        _bid_book(0.34, 12),
        _bid_book(0.33, 12),
    ]
    ws.get_latest_best_bid_age.return_value = 0.01
    trade_config = _tc(
        stop_loss_enabled=True,
        stop_loss_start_remaining_sec=120,
        stop_loss_end_remaining_sec=15,
        stop_loss_trigger_price=0.38,
    )

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor.place_fak_stop_loss_sell", new_callable=AsyncMock) as mock_sell:
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
    assert len(checks) == 1
    assert checks[0]["reason"] == "insufficient_bid_depth"


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

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
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
async def test_on_price_includes_first_book_level_in_entry_depth():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 10.0),  # enough by itself
        (0.64, 0.1),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    ws.get_latest_best_ask_age.return_value = 0.001
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["target_entry_ask"] == pytest.approx(0.60)
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.61)


@pytest.mark.asyncio
async def test_on_price_entry_ask_level_caps_deepest_price_hint_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 0.1),
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


@pytest.mark.asyncio
async def test_entry_depth_skip_logs_once_for_repeated_same_quote(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True
    ws = MagicMock()
    ws.get_latest_ask_levels_with_size.return_value = [
        (0.60, 0.1),
        (0.64, 0.1),
    ]
    ws.get_latest_best_ask.return_value = 0.60
    ws.get_latest_best_ask_age.return_value = 0.01
    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0, entry_ask_level=2), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0, entry_ask_level=2), strategy=strategy, side="up")

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


def test_window_summary_includes_dry_run_replay_aggregates(caplog):
    window = _make_window()
    state = MonitorState(
        entry_replay_check_count=7,
        entry_replay_signal_count=3,
        entry_replay_buy_signal_count=1,
        entry_replay_min_leading_ask=0.66,
        entry_replay_max_leading_ask=0.74,
        entry_replay_min_best_ask=0.64,
        entry_replay_max_best_ask=0.72,
        entry_replay_min_selected_ask=0.69,
        entry_replay_max_selected_ask=0.75,
        entry_replay_max_depth_notional=12.3456,
        entry_replay_min_best_ask_age_ms=0,
        entry_replay_max_best_ask_age_ms=220,
        stop_replay_check_count=5,
        stop_replay_triggered_count=1,
        stop_replay_missing_or_stale_bid_count=1,
        stop_replay_insufficient_depth_count=2,
        stop_replay_min_best_bid=0.31,
        stop_replay_max_best_bid=0.43,
        stop_replay_min_selected_bid=0.28,
        stop_replay_max_selected_bid=0.36,
        stop_replay_max_bid_shares_available=4.321,
        stop_replay_min_best_bid_age_ms=2,
        stop_replay_max_best_bid_age_ms=140,
    )

    with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
        _log_window_summary(state, window, dry_run=True)

    summary = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "SUMMARY")
    assert summary.event_data["entry_replay"] == {
        "checks": 7,
        "signals": 3,
        "buy_signals": 1,
        "leading_ask_min": 0.66,
        "leading_ask_max": 0.74,
        "best_ask_min": 0.64,
        "best_ask_max": 0.72,
        "selected_ask_min": 0.69,
        "selected_ask_max": 0.75,
        "depth_notional_max": 12.3456,
        "best_ask_age_ms_min": 0,
        "best_ask_age_ms_max": 220,
    }
    assert summary.event_data["stop_replay"] == {
        "checks": 5,
        "triggered": 1,
        "missing_or_stale_bid": 1,
        "insufficient_depth": 2,
        "best_bid_min": 0.31,
        "best_bid_max": 0.43,
        "selected_bid_min": 0.28,
        "selected_bid_max": 0.36,
        "bid_shares_available_max": 4.321,
        "best_bid_age_ms_min": 2,
        "best_bid_age_ms_max": 140,
    }


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
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0, entry_ask_level=2), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0, entry_ask_level=2), strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0, entry_ask_level=2), strategy=strategy, side="up")

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
async def test_handle_opening_price_dry_run_uses_depth_quote_without_latency():
    window = _make_window()
    state = MonitorState()
    state.target_side = "up"
    state.target_entry_price = 0.65
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.74

    with patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("polybot.trading.fak_quotes.get_tick_size", return_value=0.01):
        await _handle_opening_price(
            window,
            state,
            "up-token-123",
            0.65,
            dry_run=True,
            trade_config=_tc(amount=1.0),
            strategy=strategy,
            side="up",
            target_entry_ask=0.67,
            best_ask=0.68,
            ws=ws,
        )

    mock_sleep.assert_not_awaited()
    ws.get_latest_best_ask.assert_not_called()
    assert state.entry_price == pytest.approx(0.67)
    assert state.entry_avg_price == pytest.approx(0.67)
    assert state.holding_size == pytest.approx(1.0 / 0.67)


@pytest.mark.asyncio
async def test_handle_opening_price_dry_run_depth_quote_above_cap_clears_target_side():
    window = _make_window()
    state = MonitorState()
    state.target_side = "up"
    state.target_entry_price = 0.76
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()

    with patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock):
        await _handle_opening_price(
            window,
            state,
            "up-token-123",
            0.65,
            dry_run=True,
            trade_config=_tc(amount=1.0),
            strategy=strategy,
            side="up",
            target_entry_ask=0.76,
            ws=ws,
        )

    assert state.exit_triggered is True
    assert state.buy_blocked_window_cap is True
    assert state.target_entry_price is None
    assert state.target_side is None


@pytest.mark.asyncio
async def test_handle_opening_price_live_insufficient_funds_marks_fatal():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()

    with patch("polybot.trading.monitor.place_fak_buy", new_callable=AsyncMock) as mock_buy:
        mock_buy.return_value = OrderResult(
            success=False,
            message="INSUFFICIENT_FUNDS: not enough balance / allowance",
        )

        with pytest.raises(RuntimeError, match="INSUFFICIENT_FUNDS"):
            await _handle_opening_price(
                window,
                state,
                "up-token-123",
                0.65,
                dry_run=False,
                trade_config=_tc(amount=1.0),
                strategy=strategy,
                side="up",
                best_ask=0.66,
                target_entry_ask=0.66,
            )

    assert state.fatal_error == "INSUFFICIENT_FUNDS: not enough balance / allowance"
    assert state.exit_triggered is True
    assert state.buy_blocked_window_cap is True


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
    mock_ws.get_latest_price = MagicMock(return_value=0.50)
    strategy = _mock_strategy()
    strategy.should_buy.return_value = False

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock, return_value=None), \
         patch("polybot.trading.monitor.find_next_window", return_value=None):
        _, returned_ws, _ = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            strategy=strategy,
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

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
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
async def test_monitor_single_window_logs_snapshot_entry_check_once_per_window(caplog):
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
    ws.get_latest_best_ask_age.return_value = 0.01

    strategy = _mock_crowd_strategy()
    strategy.entry_start_remaining_sec = 180
    strategy.entry_end_remaining_sec = 175
    strategy.should_buy.return_value = False

    fake_now = {"value": now}

    async def _advance_time(_seconds):
        fake_now["value"] += 1
        if fake_now["value"] >= now + 2:
            fake_now["value"] = window.end_epoch

    with patch("polybot.trading.monitor.time.time", side_effect=lambda: fake_now["value"]), \
         patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = _advance_time

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

    assert strategy.set_market_snapshot.call_count == 2
    checks = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "SNAPSHOT_ENTRY_CHECK"
    ]
    assert len(checks) == 1


@pytest.mark.asyncio
async def test_monitor_single_window_does_not_check_stop_loss_without_price_update():
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

    mock_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_single_window_refreshes_settlement_mark_before_end(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 298, end_epoch=now + 2)
    state = _make_state(
        bought=True,
        holding_size=1.25,
        entry_amount=1.0,
        entry_price=0.80,
        entry_avg_price=0.80,
        target_side="down",
    )
    ws = MagicMock()
    strategy = _mock_strategy()

    fake_now = {"value": now}
    with patch("polybot.trading.monitor.time.time", side_effect=lambda: fake_now["value"]), \
         patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep, \
         patch("polybot.trading.monitor.get_midpoint_async", new_callable=AsyncMock) as mock_midpoint:
        async def _after_one_sleep(_seconds):
            fake_now["value"] = window.end_epoch

        mock_sleep.side_effect = _after_one_sleep
        mock_midpoint.return_value = 0.71

        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _monitor_single_window(
                window,
                state,
                ws,
                dry_run=False,
                trade_config=_tc(stop_loss_enabled=True),
                strategy=strategy,
                prefetch_next_window=False,
            )

    mock_midpoint.assert_awaited_once_with(window.down_token)
    assert state.settlement_mark_refreshed is True
    events = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "SETTLEMENT_MARK_REFRESH"
    ]
    assert events
    assert events[0]["mark_price"] == pytest.approx(0.71)
    assert events[0]["remaining_sec"] == pytest.approx(2)


@pytest.mark.asyncio
async def test_monitor_single_window_exits_on_fatal_state_without_waiting():
    now = int(time.time())
    window = _make_window(start_epoch=now - 100, end_epoch=now + 200)
    state = _make_state(fatal_error="INSUFFICIENT_FUNDS: not enough balance / allowance")

    with patch("polybot.trading.monitor.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        with pytest.raises(RuntimeError, match="INSUFFICIENT_FUNDS"):
            await _monitor_single_window(
                window,
                state,
                ws=None,
                dry_run=False,
                trade_config=_tc(),
                strategy=_mock_strategy(),
                prefetch_next_window=False,
            )

    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_monitor_single_window_settles_winning_dry_run_at_one(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 300, end_epoch=now)
    state = _make_state(
        bought=True,
        holding_size=1.25,
        entry_amount=1.0,
        entry_price=0.80,
        entry_avg_price=0.80,
        latest_midpoint=0.695,
        latest_midpoint_received_at=window.end_epoch - 1,
    )

    with patch("polybot.trading.monitor.time.time", return_value=window.end_epoch):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _monitor_single_window(
                window,
                state,
                ws=None,
                dry_run=True,
                trade_config=_tc(),
                strategy=None,
                prefetch_next_window=False,
            )

    resolved = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "TRADE_RESOLVED"
    ]
    assert resolved
    assert resolved[0]["result"] == "WIN"
    assert resolved[0]["price"] == pytest.approx(1.0)
    assert resolved[0]["mark_price"] == pytest.approx(0.695)
    assert resolved[0]["mark_price_fresh"] is True
    assert resolved[0]["realized_pnl"] == pytest.approx(0.25)
    assert state.daily_realized_pnl == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_monitor_single_window_does_not_binary_win_stale_mark(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 300, end_epoch=now)
    state = _make_state(
        bought=True,
        holding_size=1.25,
        entry_amount=1.0,
        entry_price=0.80,
        entry_avg_price=0.80,
        latest_midpoint=0.695,
        latest_midpoint_received_at=window.end_epoch - 30,
    )

    with patch("polybot.trading.monitor.time.time", return_value=window.end_epoch):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _monitor_single_window(
                window,
                state,
                ws=None,
                dry_run=True,
                trade_config=_tc(),
                strategy=None,
                prefetch_next_window=False,
            )

    resolved = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "TRADE_RESOLVED"
    ]
    assert resolved
    assert resolved[0]["result"] == "MARK_STALE"
    assert resolved[0]["price"] == pytest.approx(0.695)
    assert resolved[0]["mark_price_fresh"] is False
    assert resolved[0]["mark_price_age_sec"] == pytest.approx(30)
    assert resolved[0]["realized_pnl"] == pytest.approx(round((1.25 * 0.695) - 1.0, 4))
    assert state.daily_realized_pnl == pytest.approx((1.25 * 0.695) - 1.0)


@pytest.mark.asyncio
async def test_monitor_single_window_does_not_count_ambiguous_half_mark_as_loss(caplog):
    now = int(time.time())
    window = _make_window(start_epoch=now - 300, end_epoch=now)
    state = _make_state(
        bought=True,
        holding_size=1.408448,
        entry_amount=1.0,
        entry_price=0.64,
        entry_avg_price=0.64,
        latest_midpoint=0.5,
        latest_midpoint_received_at=window.end_epoch,
    )

    with patch("polybot.trading.monitor.time.time", return_value=window.end_epoch):
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _monitor_single_window(
                window,
                state,
                ws=None,
                dry_run=False,
                trade_config=_tc(),
                strategy=None,
                prefetch_next_window=False,
            )

    resolved = [
        record.event_data
        for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "TRADE_RESOLVED"
    ]
    assert resolved
    assert resolved[0]["result"] == "MARK_AMBIGUOUS"
    assert resolved[0]["price"] == pytest.approx(0.5)
    assert resolved[0]["mark_price"] == pytest.approx(0.5)
    assert resolved[0]["mark_price_fresh"] is True
    assert resolved[0]["realized_pnl"] == pytest.approx(0.0)
    assert state.daily_realized_pnl == pytest.approx(0.0)


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

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
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

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
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
async def test_monitor_window_skips_stale_preopened_after_strategy_entry_band_ends(caplog):
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 240, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 60, tz=utc),
        slug="test",
    )

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock()

    strategy = CrowdM1Strategy(
        MarketSeries.from_known("btc-updown-5m"),
        entry_elapsed_sec=180,
        entry_timeout_sec=5,
    )

    with caplog.at_level(logging.INFO), \
         patch("polybot.trading.monitor._find_and_preopen_next_window", return_value=None), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock) as monitor_single:
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy,
        )

    assert monitored is False
    assert returned_ws is mock_ws
    assert next_win is None
    mock_ws.switch_tokens.assert_not_called()
    monitor_single.assert_not_called()
    assert any(
        getattr(record, "event_data", {}).get("action") == "SKIP"
        and getattr(record, "event_data", {}).get("preopened") is True
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_monitor_window_skips_when_connect_delay_misses_snapshot_entry_band(caplog):
    import datetime

    base_now = int(time.time())
    current_time = float(base_now)
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(base_now - 100, tz=utc),
        end_time=datetime.datetime.fromtimestamp(base_now + 200, tz=utc),
        slug="test",
    )

    async def delayed_switch_tokens(_token_ids):
        nonlocal current_time
        current_time = float(base_now + 100)

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock(side_effect=delayed_switch_tokens)
    mock_ws.get_latest_price = MagicMock(return_value=0.50)

    strategy = CrowdM1Strategy(
        MarketSeries.from_known("btc-updown-5m"),
        entry_elapsed_sec=180,
        entry_timeout_sec=5,
    )

    with caplog.at_level(logging.INFO), \
         patch("polybot.trading.monitor.time.time", side_effect=lambda: current_time), \
         patch("polybot.trading.monitor.prefetch_order_params", create=True, new=MagicMock()), \
         patch("polybot.trading.monitor._find_and_preopen_next_window", return_value=None), \
         patch("polybot.trading.monitor._monitor_single_window", new_callable=AsyncMock) as monitor_single:
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            trade_config=_tc(), strategy=strategy,
        )

    assert monitored is False
    assert returned_ws is mock_ws
    assert next_win is None
    mock_ws.switch_tokens.assert_awaited_once()
    monitor_single.assert_not_called()
    assert any(
        getattr(record, "event_data", {}).get("action") == "SKIP"
        and getattr(record, "event_data", {}).get("phase") == "post_connect"
        for record in caplog.records
    )


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

    with patch("polybot.core.client.prefetch_order_params", new=MagicMock()), \
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
