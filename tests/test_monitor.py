"""Unit tests for polybot.trading.monitor — state transitions, lock, sell failure recovery."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from polybot.market.series import MarketSeries

import pytest

from polybot.core import config
from polybot.market.market import MarketWindow
from polybot.trading.monitor import (
    MonitorState,
    _check_sl_tp,
    _handle_opening_price,
    _on_price_update,
    _sanitize_next_window,
    monitor_window,
)
from polybot.market.stream import PriceUpdate
from polybot.trade_config import TradeConfig


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _make_state(**kwargs) -> MonitorState:
    """Create a MonitorState with started=True (simulating active window)."""
    state = MonitorState(**kwargs)
    state.started = True
    return state


def _make_window() -> MarketWindow:
    """Create a test window (epoch 1000-1300, i.e. 5 minutes)."""
    import datetime
    utc = datetime.timezone.utc
    return MarketWindow(
        question="Bitcoin Up or Down - Apr 15 9:40AM-9:45AM ET",
        up_token="up-token-123",
        down_token="down-token-456",
        start_time=datetime.datetime.fromtimestamp(1000, tz=utc),
        end_time=datetime.datetime.fromtimestamp(1300, tz=utc),
        slug="btc-updown-5m-1000",
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


# Default TradeConfig: entry=0.50, TP threshold=0.80 (tp_pct=0.60), SL threshold=0.35 (sl_pct=0.30)
def _tc(**overrides) -> TradeConfig:
    defaults = dict(amount=5.0, tp_pct=0.60, sl_pct=0.30)
    defaults.update(overrides)
    return TradeConfig(**defaults)


def _mock_strategy() -> MagicMock:
    """Create a mock strategy that always buys."""
    strategy = MagicMock()
    strategy.should_buy = MagicMock(return_value=True)
    strategy.check_edge_exit = MagicMock(return_value=None)
    return strategy


def test_sanitize_next_window_rejects_same_window():
    window = _make_window()
    assert _sanitize_next_window(window, window) is None


def test_sanitize_next_window_accepts_later_window():
    import datetime

    current = _make_window()
    utc = datetime.timezone.utc
    next_window = MarketWindow(
        question="Bitcoin Up or Down - Apr 15 9:45AM-9:50AM ET",
        up_token="up-token-next",
        down_token="down-token-next",
        start_time=datetime.datetime.fromtimestamp(1300, tz=utc),
        end_time=datetime.datetime.fromtimestamp(1600, tz=utc),
        slug="btc-updown-5m-1300",
    )
    assert _sanitize_next_window(current, next_window) == next_window


# ─── MonitorState ────────────────────────────────────────────────────────────

def test_monitor_state_has_trade_lock():
    """MonitorState should initialize with an asyncio.Lock and started=False."""
    state = MonitorState()
    assert state.trade_lock is not None
    assert isinstance(state.trade_lock, asyncio.Lock)
    assert not state.trade_lock.locked()
    assert state.started is False
    assert state.entry_count == 0
    assert state.edge_exit_count == 0


# ─── Buy decision ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_price_buy_in_range():
    """Price triggers buy via strategy.should_buy()."""
    window = _make_window()
    state = _make_state()
    tc = _tc()
    strategy = _mock_strategy()

    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up")
        mock_buy.assert_called_once()


@pytest.mark.asyncio
async def test_on_price_wrong_token_ignored():
    """Price update for a different token is ignored."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    update = _make_update("some-other-token", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, side="up")
        mock_buy.assert_not_called()


# ─── Stop-loss ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_price_stop_loss_triggers_sell():
    """Price below stop-loss triggers sell."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    mock_cancel.assert_called_once()
    mock_sell.assert_called_once()
    assert state.stop_loss_count == 1
    assert state.exit_triggered is True  # no re-entry when max=0


@pytest.mark.asyncio
async def test_on_price_stop_loss_with_reentry_allowed():
    """Stop-loss with re-entry allowed: exit_triggered stays False."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(max_sl_reentry=1)

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.stop_loss_count == 1
    assert state.exit_triggered is False  # re-entry allowed
    assert state.bought is False


@pytest.mark.asyncio
async def test_on_price_stop_loss_reentry_exhausted():
    """Second stop-loss when max_sl_reentry=1 blocks further buying."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(max_sl_reentry=1)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", mock_cancel):
        # First stop-loss: reentry allowed (count=1 <= max=1)
        update1 = _make_update("up-token-123", midpoint=0.30)
        await _on_price_update(update1, window, state, dry_run=False, trade_config=tc, side="up")
        assert state.stop_loss_count == 1
        assert not state.buy_blocked_sl

        # Simulate re-buy
        state.bought = True

        # Second stop-loss: count=2 > max=1 → blocked
        update2 = _make_update("up-token-123", midpoint=0.30)
        await _on_price_update(update2, window, state, dry_run=False, trade_config=tc, side="up")
        assert state.stop_loss_count == 2

        # Third price check should be blocked
        state.bought = False
        update3 = _make_update("up-token-123", midpoint=0.50)
        with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
            await _on_price_update(update3, window, state, dry_run=True, trade_config=tc, side="up")
            mock_buy.assert_not_called()
        assert state.buy_blocked_sl is True


# ─── Take-profit ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_price_take_profit_triggers_sell():
    """Price above take-profit triggers sell."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.85)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.tp_count == 1
    assert state.exit_triggered is True


@pytest.mark.asyncio
async def test_edge_exit_uses_separate_reentry_budget():
    """EDGE_EXIT should not consume take-profit budget."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(max_tp_reentry=0, max_edge_reentry=1)

    strategy = _mock_strategy()
    strategy.check_edge_exit = MagicMock(return_value="edge_decayed")
    update = _make_update("up-token-123", midpoint=0.45)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, strategy=strategy, side="up")

    assert state.edge_exit_count == 1
    assert state.tp_count == 0
    assert state.exit_triggered is False


@pytest.mark.asyncio
async def test_window_entry_cap_blocks_further_entries():
    """max_entries_per_window should hard-block new buys even if strategy still signals."""
    window = _make_window()
    state = _make_state()
    state.entry_count = 5
    tc = _tc(max_entries_per_window=5)
    strategy = _mock_strategy()
    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up")

    mock_buy.assert_not_called()
    assert state.buy_blocked_window_cap is True


@pytest.mark.asyncio
async def test_down_position_take_profit_uses_down_token_updates():
    """DOWN positions should process TP/SL from the held down-token price stream."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    state.target_side = "down"
    tc = _tc()

    update = _make_update("down-token-456", midpoint=0.85)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.tp_count == 1
    mock_sell.assert_called_once()


@pytest.mark.asyncio
async def test_down_position_edge_exit_can_trigger_from_up_token_reference():
    """DOWN positions should still get edge exits from the UP-token reference stream."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    state.target_side = "down"
    tc = _tc(max_tp_reentry=0)

    strategy = MagicMock()
    strategy.check_edge_exit = MagicMock(return_value="max_hold")

    update = _make_update("up-token-123", midpoint=0.45)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, strategy=strategy, side="up")

    strategy.check_edge_exit.assert_called_once_with(state)
    assert state.bought is False
    mock_sell.assert_called_once()


# ─── Sell failure recovery ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sell_failure_keeps_holding():
    """Failed sell after stop-loss keeps state.bought=True for continued monitoring."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=False, message="Network error"))
    mock_cancel = AsyncMock()

    with patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.stop_loss_count == 1
    # Key assertion: bought stays True so next SL/TP check still works
    assert state.bought is True


# ─── Trade lock ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_callbacks_skipped_when_locked():
    """Second callback is deferred (not dropped) while first is still processing (lock held)."""
    window = _make_window()
    state = _make_state()
    tc = _tc()
    strategy = _mock_strategy()

    update = _make_update("up-token-123", midpoint=0.50)

    call_count = 0

    async def slow_buy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # simulate slow buy

    with patch("polybot.trading.monitor._handle_opening_price", side_effect=slow_buy):
        # Fire two callbacks concurrently
        task1 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up"))
        await asyncio.sleep(0.01)  # let first callback acquire lock
        task2 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up"))
        await asyncio.gather(task1, task2)

    # Only one should have entered the trade logic
    assert call_count == 1


# ─── Dry-run mode ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_stop_loss_no_real_sell():
    """Dry-run mode logs but does not call sell_token."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.30)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock) as mock_cancel:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, side="up")

    mock_sell.assert_not_called()
    mock_cancel.assert_not_called()
    assert state.realized_pnl < 0


@pytest.mark.asyncio
async def test_dry_run_take_profit_does_not_cancel_or_sell():
    """Dry-run TP exits should not call live cancel/sell paths either."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.85)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock) as mock_cancel:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, side="up")

    mock_sell.assert_not_called()
    mock_cancel.assert_not_called()
    assert state.realized_pnl > 0


@pytest.mark.asyncio
async def test_dry_run_edge_exit_does_not_cancel_or_sell():
    """Dry-run edge exits should not touch live cancel/sell endpoints."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(max_edge_reentry=0)
    strategy = _mock_strategy()
    strategy.check_edge_exit = MagicMock(return_value="max_hold")
    update = _make_update("up-token-123", midpoint=0.45)

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock) as mock_cancel:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up")

    mock_cancel.assert_not_called()
    mock_sell.assert_not_called()
    assert state.bought is False
    assert state.exit_triggered is True
    assert state.realized_pnl < 0


# ─── _handle_opening_price ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_opening_price_dry_run():
    """Dry-run buy sets state correctly without calling real buy."""
    window = _make_window()
    state = _make_state()
    tc = _tc()
    strategy = MagicMock()
    strategy.on_buy_confirmed = MagicMock()

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock):
        await _handle_opening_price(
            window, state, "up-token-123", 0.50,
            dry_run=True, trade_config=tc, strategy=strategy, side="up",
        )

    assert state.bought is True
    assert state.entry_count == 1
    assert state.holding_size == pytest.approx(10.0)  # $5 / $0.50 = 10 shares
    assert state.entry_price == 0.50
    strategy.on_buy_confirmed.assert_called_once()


@pytest.mark.asyncio
async def test_handle_opening_price_already_bought():
    """Does not buy again if already holding."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    tc = _tc()

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock) as mock_buy:
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc, side="up")

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_handle_opening_price_buy_failed_sets_exit():
    """Buy failure sets exit_triggered=True to prevent infinite re-buy."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    mock_result = MagicMock(success=False, message="Insufficient balance")
    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock, return_value=mock_result):
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc, side="up")

    assert state.bought is False
    assert state.exit_triggered is True


@pytest.mark.asyncio
async def test_handle_opening_price_live_notifies_strategy_on_buy():
    """Successful live buy starts the strategy hold timer."""
    window = _make_window()
    state = _make_state()
    tc = _tc()
    strategy = MagicMock()
    strategy.on_buy_confirmed = MagicMock()
    mock_result = MagicMock(success=True, filled_size=10.0, avg_price=0.50)

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock, return_value=mock_result):
        await _handle_opening_price(
            window, state, "up-token-123", 0.50,
            dry_run=False, trade_config=tc, strategy=strategy, side="up",
        )

    strategy.on_buy_confirmed.assert_called_once()
    assert state.entry_count == 1
    assert state.holding_size == pytest.approx(10.0)


# ─── monitor_window WS reuse ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_monitor_window_reuses_existing_ws():
    """When existing_ws is passed, monitor_window calls switch_tokens instead of connect."""
    import datetime
    utc = datetime.timezone.utc
    # Window in the past so it's immediately expired (triggers quick return)
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
    mock_ws.close = AsyncMock()

    with patch("polybot.trading.monitor.find_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
            strategy=_mock_strategy(),
        )

    # WS was reused, not closed
    mock_ws.set_on_price.assert_called_once()
    mock_ws.switch_tokens.assert_called_once_with(["up-tok", "down-tok"])
    assert returned_ws is mock_ws


@pytest.mark.asyncio
async def test_monitor_window_opening_price_does_not_buy_without_signal():
    """Opening price should not trigger a buy unless the strategy emits a signal."""
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
    mock_ws.close = AsyncMock()

    strategy = MagicMock()
    strategy.get_side.return_value = "up"
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
async def test_monitor_window_does_not_skip_if_started_within_one_minute():
    """A just-opened window should still be monitored for up to 60 seconds."""
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 30, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 270, tz=utc),
        slug="test",
    )

    mock_ws = MagicMock()
    mock_ws.set_on_price = MagicMock()
    mock_ws.switch_tokens = AsyncMock()
    mock_ws.get_latest_price = MagicMock(return_value=0.50)
    mock_ws.close = AsyncMock()

    strategy = MagicMock()
    strategy.get_side.return_value = "up"
    strategy.should_buy.return_value = False

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
async def test_monitor_window_skips_if_started_more_than_one_minute_ago():
    """Windows older than 60 seconds should still be skipped on fresh attach."""
    import datetime

    now = int(time.time())
    utc = datetime.timezone.utc
    window = MarketWindow(
        question="Test Window",
        up_token="up-tok",
        down_token="down-tok",
        start_time=datetime.datetime.fromtimestamp(now - 61, tz=utc),
        end_time=datetime.datetime.fromtimestamp(now + 239, tz=utc),
        slug="test",
    )

    strategy = MagicMock()
    strategy.get_side.return_value = "up"

    with patch("polybot.trading.monitor._find_and_preopen_next_window", return_value=None):
        next_win, returned_ws, monitored = await monitor_window(
            window, dry_run=True, preopened=False, existing_ws=None,
            trade_config=_tc(), strategy=strategy,
        )

    assert monitored is False
    assert returned_ws is None
    assert next_win is None


# ─── Price signal enhancement tests ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_tp_triggers_on_last_trade_price():
    """last_trade_price at 0.85 triggers TP even when midpoint=0.78 (below TP=0.80)."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    # First: seed a last_trade_price above TP
    trade_update = _make_update("up-token-123", midpoint=0.85, source="last_trade_price")
    await _on_price_update(trade_update, window, state, dry_run=True, trade_config=tc, side="up")

    # The last_trade_price at 0.85 should have triggered TP
    assert state.tp_count == 1
    assert state.exit_triggered is True


@pytest.mark.asyncio
async def test_tp_triggers_on_best_ask():
    """best_ask=0.85 triggers TP even when midpoint=0.79 (below TP=0.80)."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    # best_bid_ask with midpoint=0.79 but best_ask=0.85
    update = PriceUpdate(
        token_id="up-token-123",
        best_bid=0.73,
        best_ask=0.85,
        midpoint=0.79,
        spread=0.12,
        source="best_bid_ask",
    )

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.tp_count == 1
    mock_sell.assert_called_once()


@pytest.mark.asyncio
async def test_sl_triggers_on_best_bid():
    """best_bid=0.28 triggers SL even when midpoint=0.35 (above SL=0.30)."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    # STOP_LOSS=0.30 → sl_pct = (0.50-0.30)/0.50 = 0.40
    tc = _tc(sl_pct=0.40)

    # best_bid_ask with midpoint=0.35 but best_bid=0.28
    update = PriceUpdate(
        token_id="up-token-123",
        best_bid=0.28,
        best_ask=0.42,
        midpoint=0.35,
        spread=0.14,
        source="best_bid_ask",
    )

    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.stop_loss_count == 1
    mock_sell.assert_called_once()


@pytest.mark.asyncio
async def test_sl_triggers_on_last_trade_price():
    """last_trade_price at 0.25 triggers SL even when midpoint=0.35."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(sl_pct=0.40)

    # Seed a last_trade_price below SL
    trade_update = _make_update("up-token-123", midpoint=0.25, source="last_trade_price")
    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        mock_sell.return_value = MagicMock(success=True)
        await _on_price_update(trade_update, window, state, dry_run=False, trade_config=tc, side="up")

    assert state.stop_loss_count == 1


@pytest.mark.asyncio
async def test_stale_trade_price_ignored():
    """Trade price older than TTL is not used for SL/TP."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc(sl_pct=0.40)

    # Store a stale trade price (4 seconds ago)
    state._last_trade_price = 0.85  # above TP
    state._last_trade_time = time.monotonic() - 4.0  # stale

    # Send a best_bid_ask with midpoint below TP — should NOT trigger
    update = _make_update("up-token-123", midpoint=0.75)
    with patch("polybot.trading.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc, side="up")

    mock_sell.assert_not_called()
    assert state.tp_count == 0


@pytest.mark.asyncio
async def test_deferred_signal_stored_when_locked():
    """WS update during lock is stored in _pending_signal instead of being dropped."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    tp_update = _make_update("up-token-123", midpoint=0.85)

    async def slow_sell(*args, **kwargs):
        await asyncio.sleep(0.1)  # simulate slow sell
        return MagicMock(success=True)

    # First callback enters the lock with a TP-triggering update
    with patch("polybot.trading.monitor.sell_token", side_effect=slow_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        task1 = asyncio.create_task(_on_price_update(tp_update, window, state, dry_run=False, trade_config=tc, side="up"))
        await asyncio.sleep(0.01)  # let first callback acquire lock

        # Second callback should store deferred signal
        state2_update = _make_update("up-token-123", midpoint=0.50)
        await asyncio.create_task(_on_price_update(state2_update, window, state, dry_run=True, trade_config=tc, side="up"))

        await asyncio.gather(task1)

    # Verify first callback triggered TP
    assert state.tp_count == 1


@pytest.mark.asyncio
async def test_deferred_signal_replayed_after_lock_release():
    """Pending exit signals are replayed immediately after the lock is released."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    tc = _tc()

    first_update = _make_update("up-token-123", midpoint=0.50)
    deferred_update = _make_update("up-token-123", midpoint=0.85)

    entered = asyncio.Event()
    release = asyncio.Event()
    call_midpoints = []

    async def slow_check_sl_tp(update, *args, **kwargs):
        call_midpoints.append(update.midpoint)
        if len(call_midpoints) == 1:
            entered.set()
            await release.wait()
        return False

    with patch("polybot.trading.monitor._check_sl_tp", side_effect=slow_check_sl_tp):
        task1 = asyncio.create_task(
            _on_price_update(first_update, window, state, dry_run=False, trade_config=tc, side="up")
        )
        await entered.wait()
        await _on_price_update(deferred_update, window, state, dry_run=False, trade_config=tc, side="up")
        release.set()
        await task1

    assert call_midpoints == [0.50, 0.85]


@pytest.mark.asyncio
async def test_pending_down_token_signal_replayed_after_up_token_early_return():
    """A queued DOWN-token exit signal should still replay after an UP-token reference update."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    state.entry_price = 0.50
    state.target_side = "down"
    tc = _tc()

    pending_update = _make_update("down-token-456", midpoint=0.85)
    state._pending_signal = pending_update

    strategy = _mock_strategy()
    strategy.check_edge_exit = MagicMock(return_value=None)
    up_update = _make_update("up-token-123", midpoint=0.45)

    with patch("polybot.trading.monitor._check_sl_tp", new_callable=AsyncMock) as mock_check_sl_tp:
        await _on_price_update(
            up_update, window, state, dry_run=True, trade_config=tc, strategy=strategy, side="up",
        )

    mock_check_sl_tp.assert_awaited_once()
    replayed_update = mock_check_sl_tp.await_args.args[0]
    assert replayed_update.token_id == "down-token-456"
    assert state._pending_signal is None


@pytest.mark.asyncio
async def test_post_buy_deferred_signal_discarded():
    """After buy, deferred signal is discarded (stale price context for new position)."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    # Simulate a deferred TP signal that arrived during the buy
    deferred = PriceUpdate(
        token_id="up-token-123",
        best_bid=0.82,
        best_ask=0.88,
        midpoint=0.85,
        spread=0.06,
        source="last_trade_price",
    )
    state._pending_signal = deferred

    mock_buy_result = MagicMock(success=True, filled_size=10.0, avg_price=0.50)
    mock_sell = AsyncMock(return_value=MagicMock(success=True))

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock, return_value=mock_buy_result), \
         patch("polybot.trading.monitor.sell_token", mock_sell), \
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc, side="up")

    # Buy succeeded, deferred signal should be discarded (not processed)
    assert state.bought is True
    assert state.tp_count == 0
    mock_sell.assert_not_called()
    assert state._pending_signal is None
