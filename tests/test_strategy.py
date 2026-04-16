"""Tests for TradeConfig (check_exit) and ImmediateStrategy (should_buy)."""

import pytest

from polybot.core.state import MonitorState
from polybot.strategies.immediate import ImmediateStrategy
from polybot.trade_config import ExitReason, TradeConfig


def _make_state(entry_price: float = 0.0, **kwargs) -> MonitorState:
    """Create a MonitorState with optional overrides."""
    state = MonitorState(**kwargs)
    state.entry_price = entry_price
    state.started = True
    return state


# ─── ImmediateStrategy ─────────────────────────────────────────────────────

def test_should_buy_always_true():
    """should_buy returns True for any price."""
    s = ImmediateStrategy()
    state = _make_state()
    assert s.should_buy(0.50, state) is True
    assert s.should_buy(0.30, state) is True
    assert s.should_buy(0.70, state) is True
    assert s.should_buy(0.01, state) is True
    assert s.should_buy(0.99, state) is True


# ─── TradeConfig.check_exit — TP ────────────────────────────────────────────

def test_check_exit_tp_triggered():
    """TP triggers when tp_price > entry * (1 + tp_pct)."""
    tc = TradeConfig(tp_pct=0.50)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.80, sl_price=0.50, state=state)
    assert signal is not None
    assert signal.reason == ExitReason.TAKE_PROFIT
    assert signal.threshold == pytest.approx(0.75)


def test_check_exit_tp_no_trigger():
    tc = TradeConfig(tp_pct=0.50)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.70, sl_price=0.50, state=state)
    assert signal is None


def test_check_exit_tp_at_threshold():
    """TP at exact threshold does NOT trigger (strict >)."""
    tc = TradeConfig(tp_pct=0.50)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.75, sl_price=0.50, state=state)
    assert signal is None


# ─── TradeConfig.check_exit — SL ────────────────────────────────────────────

def test_check_exit_sl_triggered():
    """SL triggers when sl_price < entry * (1 - sl_pct)."""
    tc = TradeConfig(sl_pct=0.30)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.50, sl_price=0.20, state=state)
    assert signal is not None
    assert signal.reason == ExitReason.STOP_LOSS
    assert signal.threshold == pytest.approx(0.35)


def test_check_exit_sl_no_trigger():
    tc = TradeConfig(sl_pct=0.30)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.50, sl_price=0.40, state=state)
    assert signal is None


def test_check_exit_sl_at_threshold():
    """SL at exact threshold does NOT trigger (strict <)."""
    tc = TradeConfig(sl_pct=0.30)
    state = _make_state(entry_price=0.50)

    signal = tc.check_exit(tp_price=0.50, sl_price=0.35, state=state)
    assert signal is None


# ─── TradeConfig.check_exit — edge cases ────────────────────────────────────

def test_check_exit_no_entry_price():
    """No exit when entry_price is 0 (haven't bought yet)."""
    tc = TradeConfig(tp_pct=0.50)
    state = _make_state(entry_price=0.0)

    signal = tc.check_exit(tp_price=0.99, sl_price=0.01, state=state)
    assert signal is None


def test_check_exit_different_entry_prices():
    """Thresholds scale with entry price."""
    tc = TradeConfig(tp_pct=0.60, sl_pct=0.40)

    # High entry: TP at 0.70 * 1.60 = 1.12, SL at 0.70 * 0.60 = 0.42
    state_high = _make_state(entry_price=0.70)
    sig = tc.check_exit(tp_price=1.15, sl_price=0.50, state=state_high)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    assert sig.threshold == pytest.approx(1.12)

    # Low entry: TP at 0.30 * 1.60 = 0.48, SL at 0.30 * 0.60 = 0.18
    state_low = _make_state(entry_price=0.30)
    sig = tc.check_exit(tp_price=0.40, sl_price=0.15, state=state_low)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert sig.threshold == pytest.approx(0.18)


# ─── Re-entry ─────────────────────────────────────────────────────────────────

def test_tp_reentry_allowed():
    tc = TradeConfig(tp_pct=0.50, max_tp_reentry=1)
    state = _make_state(entry_price=0.50)
    state.tp_count = 0  # first TP

    signal = tc.check_exit(tp_price=0.80, sl_price=0.50, state=state)
    assert signal is not None
    assert signal.can_reenter is True  # count(1) <= max(1)


def test_tp_reentry_exhausted():
    tc = TradeConfig(tp_pct=0.50, max_tp_reentry=0)
    state = _make_state(entry_price=0.50)
    state.tp_count = 0  # first TP

    signal = tc.check_exit(tp_price=0.80, sl_price=0.50, state=state)
    assert signal is not None
    assert signal.can_reenter is False  # count(1) > max(0)


def test_sl_reentry_allowed():
    tc = TradeConfig(sl_pct=0.30, max_sl_reentry=2)
    state = _make_state(entry_price=0.50)
    state.stop_loss_count = 1  # second SL

    signal = tc.check_exit(tp_price=0.50, sl_price=0.20, state=state)
    assert signal is not None
    assert signal.can_reenter is True  # count(2) <= max(2)


# ─── Defaults check ──────────────────────────────────────────────────────────

def test_default_tp_sl():
    """Default TradeConfig at entry=0.50: TP at 0.75, SL at 0.35."""
    tc = TradeConfig()
    state = _make_state(entry_price=0.50)
    # TP at 0.80 should trigger (0.80 > 0.75)
    assert tc.check_exit(tp_price=0.80, sl_price=0.50, state=state) is not None
    # SL at 0.30 should trigger (0.30 < 0.35)
    assert tc.check_exit(tp_price=0.50, sl_price=0.30, state=state) is not None


# ─── TradeConfig properties ──────────────────────────────────────────────────

def test_default_properties():
    """Default TradeConfig has expected property values."""
    tc = TradeConfig()
    assert tc.side == "up"
    assert tc.amount == 5.0
    assert tc.tp_pct == 0.50
    assert tc.sl_pct == 0.30
    assert tc.max_sl_reentry == 0
    assert tc.max_tp_reentry == 0
    assert tc.rounds is None
