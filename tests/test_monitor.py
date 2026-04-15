"""Unit tests for btc5m.monitor — state transitions, lock, sell failure recovery."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from btc5m import config
from btc5m.market import MarketWindow
from btc5m.monitor import MonitorState, _handle_opening_price, _on_price_update
from btc5m.stream import PriceUpdate


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
    """Price in buy range triggers buy."""
    window = _make_window()
    state = _make_state()
    config.BUY_SIDE = "up"
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55
    config.BUY_AMOUNT = 1.0
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.50)

    with patch("btc5m.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True)
        mock_buy.assert_called_once()


@pytest.mark.asyncio
async def test_on_price_buy_out_of_range():
    """Price outside buy range does not trigger buy."""
    window = _make_window()
    state = _make_state()
    config.BUY_SIDE = "up"
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55

    update = _make_update("up-token-123", midpoint=0.60)

    with patch("btc5m.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True)
        mock_buy.assert_not_called()


@pytest.mark.asyncio
async def test_on_price_wrong_token_ignored():
    """Price update for a different token is ignored."""
    window = _make_window()
    state = _make_state()

    update = _make_update("some-other-token", midpoint=0.50)

    with patch("btc5m.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
        await _on_price_update(update, window, state, dry_run=True)
        mock_buy.assert_not_called()


# ─── Stop-loss ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_on_price_stop_loss_triggers_sell():
    """Price below stop-loss triggers sell."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("btc5m.monitor.sell_token", mock_sell), \
         patch("btc5m.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False)

    mock_cancel.assert_called_once()
    mock_sell.assert_called_once()
    assert state.stop_loss_count == 1
    assert state.exit_triggered is True  # no re-entry when MAX=0


@pytest.mark.asyncio
async def test_on_price_stop_loss_with_reentry_allowed():
    """Stop-loss with re-entry allowed: exit_triggered stays False."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 1
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("btc5m.monitor.sell_token", mock_sell), \
         patch("btc5m.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False)

    assert state.stop_loss_count == 1
    assert state.exit_triggered is False  # re-entry allowed
    assert state.bought is False


@pytest.mark.asyncio
async def test_on_price_stop_loss_reentry_exhausted():
    """Second stop-loss when MAX_REENTRY=1 blocks further buying."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    state.holding_size = 10.0
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 1
    config.MAX_TP_REENTRY = 0

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("btc5m.monitor.sell_token", mock_sell), \
         patch("btc5m.monitor.cancel_all_open_orders", mock_cancel):
        # First stop-loss: reentry allowed (count=1 <= MAX=1)
        update1 = _make_update("up-token-123", midpoint=0.30)
        await _on_price_update(update1, window, state, dry_run=False)
        assert state.stop_loss_count == 1
        assert not state.buy_blocked_sl

        # Simulate re-buy
        state.bought = True

        # Second stop-loss: reentry still allowed (count=2 > MAX=1? No: 2 > 1 → blocked)
        update2 = _make_update("up-token-123", midpoint=0.30)
        await _on_price_update(update2, window, state, dry_run=False)
        assert state.stop_loss_count == 2

        # Third price check should be blocked
        state.bought = False  # reset to test buy attempt
        update3 = _make_update("up-token-123", midpoint=0.50)  # back in buy range
        with patch("btc5m.monitor._handle_opening_price", new_callable=AsyncMock) as mock_buy:
            await _on_price_update(update3, window, state, dry_run=True)
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
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.85)

    mock_sell = AsyncMock(return_value=MagicMock(success=True))
    mock_cancel = AsyncMock()

    with patch("btc5m.monitor.sell_token", mock_sell), \
         patch("btc5m.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False)

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
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.30)

    mock_sell = AsyncMock(return_value=MagicMock(success=False, message="Network error"))
    mock_cancel = AsyncMock()

    with patch("btc5m.monitor.sell_token", mock_sell), \
         patch("btc5m.monitor.cancel_all_open_orders", mock_cancel):
        await _on_price_update(update, window, state, dry_run=False)

    assert state.stop_loss_count == 1
    # Key assertion: bought stays True so next SL/TP check still works
    assert state.bought is True


# ─── Trade lock ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_callbacks_skipped_when_locked():
    """Second callback is skipped while first is still processing (lock held)."""
    window = _make_window()
    state = _make_state()
    config.BUY_SIDE = "up"
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55
    config.BUY_AMOUNT = 1.0
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.50)

    call_count = 0

    async def slow_buy(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.1)  # simulate slow buy

    with patch("btc5m.monitor._handle_opening_price", side_effect=slow_buy):
        # Fire two callbacks concurrently
        task1 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True))
        await asyncio.sleep(0.01)  # let first callback acquire lock
        task2 = asyncio.create_task(_on_price_update(update, window, state, dry_run=True))
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
    config.BUY_SIDE = "up"
    config.STOP_LOSS = 0.35
    config.TAKE_PROFIT = 0.80
    config.MAX_STOP_LOSS_REENTRY = 0
    config.MAX_TP_REENTRY = 0

    update = _make_update("up-token-123", midpoint=0.30)

    with patch("btc5m.monitor.sell_token", new_callable=AsyncMock) as mock_sell, \
         patch("btc5m.monitor.cancel_all_open_orders", new_callable=AsyncMock):
        await _on_price_update(update, window, state, dry_run=True)

    mock_sell.assert_not_called()


# ─── _handle_opening_price ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handle_opening_price_dry_run():
    """Dry-run buy sets state correctly without calling real buy."""
    window = _make_window()
    state = _make_state()
    config.BUY_SIDE = "up"
    config.BUY_AMOUNT = 5.0
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55

    with patch("btc5m.monitor.buy_token", new_callable=AsyncMock):
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=True)

    assert state.bought is True
    assert state.holding_size == pytest.approx(10.0)  # $5 / $0.50 = 10 shares
    assert state.entry_price == 0.50


@pytest.mark.asyncio
async def test_handle_opening_price_out_of_range():
    """Price outside buy range does not set bought."""
    window = _make_window()
    state = _make_state()
    config.BUY_SIDE = "up"
    config.BUY_AMOUNT = 5.0
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55

    await _handle_opening_price(window, state, "up-token-123", 0.60, dry_run=True)

    assert state.bought is False


@pytest.mark.asyncio
async def test_handle_opening_price_already_bought():
    """Does not buy again if already holding."""
    window = _make_window()
    state = _make_state()
    state.bought = True
    config.BUY_SIDE = "up"
    config.BUY_AMOUNT = 5.0
    config.BUY_THRESHOLD_LOW = 0.45
    config.BUY_THRESHOLD_HIGH = 0.55

    with patch("btc5m.monitor.buy_token", new_callable=AsyncMock) as mock_buy:
        await _handle_opening_price(window, state, "up-token-123", 0.50, dry_run=False)

    mock_buy.assert_not_called()


# ─── monitor_window WS reuse ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_monitor_window_reuses_existing_ws():
    """When existing_ws is passed, monitor_window calls switch_tokens instead of connect."""
    import datetime
    from btc5m.monitor import monitor_window

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

    with patch("btc5m.monitor.find_next_window", return_value=None):
        next_win, returned_ws = await monitor_window(
            window, dry_run=True, preopened=True, existing_ws=mock_ws,
        )

    # WS was reused, not closed
    mock_ws.set_on_price.assert_called_once()
    mock_ws.switch_tokens.assert_called_once_with(["up-tok", "down-tok"])
    assert returned_ws is mock_ws
