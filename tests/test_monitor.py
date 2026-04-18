"""Unit tests for polybot.trading.monitor — state transitions, lock, sell failure recovery."""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from polybot.predict.momentum import MomentumPredictor
from polybot.market.series import MarketSeries

import pytest

from polybot.core import config
from polybot.market.market import MarketWindow
from polybot.trading.monitor import MonitorState, _check_sl_tp, _handle_opening_price, _on_price_update
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
    defaults = dict(side="up", amount=5.0, tp_pct=0.60, sl_pct=0.30)
    defaults.update(overrides)
    return TradeConfig(**defaults)


# ─── MonitorState ────────────────────────────────────────────────────────────

def test_monitor_state_has_trade_lock():
    """MonitorState should initialize with an asyncio.Lock and started=False."""
    state = MonitorState()
    assert state.trade_lock is not None
    assert isinstance(state.trade_lock, asyncio.Lock)
    assert not state.trade_lock.locked()
    assert state.started is False


# ─── Buy decision ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_price_buy_in_range():
    """Price triggers buy via ImmediateStrategy."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    update = _make_update("up-token-123", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc)
        mock_buy.assert_called_once()


@pytest.mark.asyncio
async def test_on_price_wrong_token_ignored():
    """Price update for a different token is ignored."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    update = _make_update("some-other-token", midpoint=0.50)

    with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc)
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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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
        await _on_price_update(update1, window, state, dry_run=False, trade_config=tc)
        assert state.stop_loss_count == 1
        assert not state.buy_blocked_sl

        # Simulate re-buy
        state.bought = True

        # Second stop-loss: count=2 > max=1 → blocked
        update2 = _make_update("up-token-123", midpoint=0.30)
        await _on_price_update(update2, window, state, dry_run=False, trade_config=tc)
        assert state.stop_loss_count == 2

        # Third price check should be blocked
        state.bought = False
        update3 = _make_update("up-token-123", midpoint=0.50)
        with patch("polybot.trading.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
            await _on_price_update(update3, window, state, dry_run=True, trade_config=tc)
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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

    assert state.tp_count == 1
    assert state.exit_triggered is True


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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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

    update = _make_update("up-token-123", midpoint=0.50)

    call_count = 0

    async def slow_buy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # simulate slow buy

    with patch("polybot.trading.monitor._handle_opening_price", side_effect=slow_buy):
        # Fire two callbacks concurrently
        task1 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True, trade_config=tc))
        await asyncio.sleep(0.01)  # let first callback acquire lock
        task2 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True, trade_config=tc))
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
         patch("polybot.trading.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        await _on_price_update(update, window, state, dry_run=True, trade_config=tc)

    mock_sell.assert_not_called()


# ─── _handle_opening_price ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_opening_price_dry_run():
    """Dry-run buy sets state correctly without calling real buy."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock):
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=True, trade_config=tc)

    assert state.bought is True
    assert state.holding_size == pytest.approx(10.0)  # $5 / $0.50 = 10 shares
    assert state.entry_price == 0.50


@pytest.mark.asyncio
async def test_handle_opening_price_already_bought():
    """Does not buy again if already holding."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    tc = _tc()

    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock) as mock_buy:
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc)

    mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_handle_opening_price_buy_failed_sets_exit():
    """Buy failure sets exit_triggered=True to prevent infinite re-buy."""
    window = _make_window()
    state = _make_state()
    tc = _tc()

    mock_result = MagicMock(success=False, message="Insufficient balance")
    with patch("polybot.trading.monitor.buy_token", new_callable=AsyncMock, return_value=mock_result):
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc)

    assert state.bought is False
    assert state.exit_triggered is True


# ─── monitor_window WS reuse ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_monitor_window_reuses_existing_ws():
    """When existing_ws is passed, monitor_window calls switch_tokens instead of connect."""
    import datetime
    from polybot.trading.monitor import monitor_window

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
        )

    # WS was reused, not closed
    mock_ws.set_on_price.assert_called_once()
    mock_ws.switch_tokens.assert_called_once_with(["up-tok", "down-tok"])
    assert returned_ws is mock_ws


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
    await _on_price_update(trade_update, window, state, dry_run=True, trade_config=tc)

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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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
        await _on_price_update(trade_update, window, state, dry_run=False, trade_config=tc)

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
        await _on_price_update(update, window, state, dry_run=False, trade_config=tc)

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
        task1 = asyncio.create_task(_on_price_update(tp_update, window, state, dry_run=False, trade_config=tc))
        await asyncio.sleep(0.01)  # let first callback acquire lock

        # Second callback should store deferred signal
        state2_update = _make_update("up-token-123", midpoint=0.50)
        await asyncio.create_task(_on_price_update(state2_update, window, state, dry_run=True, trade_config=tc))

        await asyncio.gather(task1)

    # Verify first callback triggered TP
    assert state.tp_count == 1


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
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False, trade_config=tc)

    # Buy succeeded, deferred signal should be discarded (not processed)
    assert state.bought is True
    assert state.tp_count == 0
    mock_sell.assert_not_called()
    assert state._pending_signal is None


class TestDirectionPrediction:
    @pytest.mark.asyncio
    async def test_predictor_sets_side_at_window_start(self):
        """Predictor is called at window start and sets trade_config.side."""
        import datetime
        from polybot.trading.monitor import monitor_window

        utc = datetime.timezone.utc
        now = int(time.time())
        start = (now // 300) * 300
        window = MarketWindow(
            question="Bitcoin Up or Down - Test",
            up_token="up-tok",
            down_token="down-tok",
            start_time=datetime.datetime.fromtimestamp(start, tz=utc),
            end_time=datetime.datetime.fromtimestamp(start + 300, tz=utc),
            slug="btc-updown-5m-test",
        )

        predictor = MomentumPredictor(
            MarketSeries.from_known("btc-updown-5m"),
            fallback_side="down",
        )
        tc = TradeConfig(side="up")

        mock_ws = MagicMock()
        mock_ws.set_on_price = MagicMock()
        mock_ws.switch_tokens = AsyncMock()
        mock_ws.get_latest_price = MagicMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("polybot.trading.monitor.find_next_window", return_value=None), \
             patch("polybot.trading.monitor.get_midpoint_async", new_callable=AsyncMock, return_value=None), \
             patch("polybot.predict.kline.BinanceKlineFetcher") as MockFetcher:
            MockFetcher.return_value.fetch.return_value = []  # empty → fallback
            await monitor_window(
                window, dry_run=True, preopened=True, existing_ws=mock_ws,
                trade_config=tc, predictor=predictor,
            )

        assert tc.side == "down"  # fallback_side used when no candles

    @pytest.mark.asyncio
    async def test_window_skipped_when_direction_unclear_no_fallback(self):
        """Window is skipped when predictor returns None and no fallback_side."""
        import datetime
        from polybot.trading.monitor import monitor_window

        utc = datetime.timezone.utc
        now = int(time.time())
        start = (now // 300) * 300
        window = MarketWindow(
            question="Bitcoin Up or Down - Test",
            up_token="up-tok",
            down_token="down-tok",
            start_time=datetime.datetime.fromtimestamp(start, tz=utc),
            end_time=datetime.datetime.fromtimestamp(start + 300, tz=utc),
            slug="btc-updown-5m-test",
        )

        # No fallback_side → predict returns None when data insufficient
        predictor = MomentumPredictor(
            MarketSeries.from_known("btc-updown-5m"),
        )
        tc = TradeConfig(side="up")

        mock_ws = MagicMock()
        mock_ws.set_on_price = MagicMock()
        mock_ws.switch_tokens = AsyncMock()
        mock_ws.get_latest_price = MagicMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("polybot.trading.monitor.find_next_window", return_value=None), \
             patch("polybot.predict.kline.BinanceKlineFetcher") as MockFetcher:
            MockFetcher.return_value.fetch.return_value = []  # empty → None
            next_win, returned_ws, monitored = await monitor_window(
                window, dry_run=True, preopened=True, existing_ws=mock_ws,
                trade_config=tc, predictor=predictor,
            )

        assert monitored is False  # window skipped
        assert tc.side == "up"  # side unchanged
