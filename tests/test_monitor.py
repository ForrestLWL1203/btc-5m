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
    _monitor_single_window,
    _on_price_update,
    _process_trade_result,
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
    strategy.max_entry_price = 0.65
    return strategy


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
    assert buy_signal.event_data["target_entry_ask"] == pytest.approx(0.61)
    assert buy_signal.event_data["best_ask_level_1"] == pytest.approx(0.61)
    assert buy_signal.event_data["signal_price"] == pytest.approx(0.385)


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
async def test_on_price_allows_low_target_best_ask_below_legacy_min():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.42
    strategy.should_buy.side_effect = lambda price, state_obj: setattr(state_obj, "target_side", "up") or True

    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
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
async def test_on_price_skips_normal_full_cap_entry_when_guard_matches(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.68
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.68

    def _normal_weak_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "normal"
        state_obj.target_signal_strength = 1.04
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _normal_weak_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        normal_full_cap_guard_enabled=True,
        normal_full_cap_min_signal_strength=1.05,
        normal_full_cap_min_remaining_sec=210,
    )

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
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
    skip = next(record for record in caplog.records if getattr(record, "event_data", {}).get("action") == "ENTRY_GUARD_SKIP")
    assert skip.event_data["reason"] == "signal_strength_below_min"


@pytest.mark.asyncio
async def test_on_price_allows_normal_full_cap_entry_when_guard_thresholds_pass():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.68
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.68

    def _normal_ok_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "normal"
        state_obj.target_signal_strength = 1.06
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _normal_ok_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        normal_full_cap_guard_enabled=True,
        normal_full_cap_min_signal_strength=1.05,
        normal_full_cap_min_remaining_sec=210,
    )

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
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
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.68)


@pytest.mark.asyncio
async def test_on_price_uses_strength_based_entry_ask_level():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.75
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.72
    ws.get_latest_best_ask_age.return_value = 0.001

    def _normal_ok_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "normal"
        state_obj.target_signal_strength = 2.1
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _normal_ok_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=1,
        ask_level_tiers=[(2.0, 2), (3.5, 4)],
    )

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
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

    def _strong_up_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "strong"
        state_obj.target_signal_strength = 2.1
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _strong_up_signal
    update = _make_update("up-token-123", midpoint=0.68)
    update.best_ask = 0.60
    trade_config = _tc(
        amount=1.0,
        entry_ask_level=1,
        ask_level_tiers=[(2.0, 2)],
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
async def test_normal_full_cap_guard_logs_once_for_repeated_same_skip(caplog):
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.68
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.68

    def _normal_weak_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "normal"
        state_obj.target_signal_strength = 1.06
        state_obj.target_remaining_sec = 209
        return True

    strategy.should_buy.side_effect = _normal_weak_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        normal_full_cap_guard_enabled=True,
        normal_full_cap_min_signal_strength=1.05,
        normal_full_cap_min_remaining_sec=210,
    )

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        with caplog.at_level(logging.INFO, logger="polybot.trading.monitor"):
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=trade_config, strategy=strategy, side="up")
            await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=trade_config, strategy=strategy, side="up")

    mock_buy.assert_not_called()
    skips = [
        record for record in caplog.records
        if getattr(record, "event_data", {}).get("action") == "ENTRY_GUARD_SKIP"
    ]
    assert len(skips) == 1
    assert skips[0].event_data["reason"] == "remaining_sec_below_min"


@pytest.mark.asyncio
async def test_normal_full_cap_guard_does_not_cache_skipped_ask():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.68
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.68
    strengths = iter([1.04, 1.06])

    def _normal_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_signal_confidence = "normal"
        state_obj.target_signal_strength = next(strengths)
        state_obj.target_remaining_sec = 240
        return True

    strategy.should_buy.side_effect = _normal_signal
    update = _make_update("up-token-123", midpoint=0.68)
    trade_config = _tc(
        amount=1.0,
        normal_full_cap_guard_enabled=True,
        normal_full_cap_min_signal_strength=1.05,
        normal_full_cap_min_remaining_sec=210,
    )

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=trade_config, strategy=strategy, side="up")
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=trade_config, strategy=strategy, side="up")

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.68)


@pytest.mark.asyncio
async def test_on_price_allows_high_conf_dynamic_cap():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.61
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.74

    def _high_conf_signal(price, state_obj):
        state_obj.target_side = "up"
        state_obj.target_max_entry_price = 0.75
        state_obj.target_signal_confidence = "high"
        return True

    strategy.should_buy.side_effect = _high_conf_signal
    update = _make_update("up-token-123", midpoint=0.74)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, ws=ws, dry_run=True, trade_config=_tc(amount=1.0), strategy=strategy, side="up")

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.75)


@pytest.mark.asyncio
async def test_on_price_uses_dynamic_cap_as_initial_hint_for_strong_signal():
    window = _make_window()
    state = _make_state()
    strategy = _mock_strategy()
    strategy.max_entry_price = 0.65
    ws = MagicMock()
    ws.get_latest_best_ask.return_value = 0.66

    def _strong_signal(price, state_obj):
        state_obj.target_side = "down"
        state_obj.target_max_entry_price = 0.70
        state_obj.target_signal_confidence = "strong"
        return True

    strategy.should_buy.side_effect = _strong_signal
    update = _make_update("up-token-123", midpoint=0.34)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
         patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
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

    mock_buy.assert_awaited_once()
    assert mock_buy.await_args.args[2] == "down-token-456"
    assert mock_buy.await_args.kwargs["best_ask"] == pytest.approx(0.67)


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
    ws.get_latest_best_ask.side_effect = [0.64, 0.64, 0.64]
    ws.get_latest_best_ask_age.return_value = 0.001
    update = _make_update("up-token-123", midpoint=0.58)

    with patch("polybot.trading.monitor.get_tick_size", return_value=0.01), \
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
    state.target_signal_confidence = "strong"
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
        assert state.target_signal_confidence is None
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
