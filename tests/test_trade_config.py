"""Tests for TradeConfig — progressive SL tightening on re-entry + absolute price TP/SL."""

from polybot.core.state import MonitorState
from polybot.trade_config import ExitReason, TradeConfig


def _state(entry: float, sl_count: int = 0) -> MonitorState:
    s = MonitorState()
    s.entry_price = entry
    s.stop_loss_count = sl_count
    return s


# ─── Basic TP/SL (percentage) ────────────────────────────────────────────────

def test_tp_triggers_above_threshold():
    tc = TradeConfig(tp_pct=0.50, sl_pct=0.30)
    state = _state(0.40)
    sig = tc.check_exit(tp_price=0.65, sl_price=0.30, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    assert abs(sig.threshold - 0.60) < 1e-9


def test_sl_triggers_below_threshold():
    tc = TradeConfig(tp_pct=0.50, sl_pct=0.30)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.30, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert abs(sig.threshold - 0.35) < 1e-9


def test_no_exit_when_within_range():
    tc = TradeConfig(tp_pct=0.50, sl_pct=0.30)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.40, state=state)
    assert sig is None


# ─── Progressive SL Tightening (percentage) ──────────────────────────────────

def test_no_tightening_on_first_entry():
    """sl_count=0 means no tightening — full sl_pct used."""
    tc = TradeConfig(sl_pct=0.50)
    state = _state(0.60, sl_count=0)
    # threshold = 0.60 * (1 - 0.50) = 0.30
    sig = tc.check_exit(tp_price=0.60, sl_price=0.29, state=state)
    assert sig is not None
    assert abs(sig.effective_sl_pct - 0.50) < 1e-9
    assert abs(sig.threshold - 0.30) < 1e-9


def test_sl_tightens_on_first_reentry():
    """After 1 SL (sl_count=1), sl_pct 50% → 40%."""
    tc = TradeConfig(sl_pct=0.50)
    state = _state(0.60, sl_count=1)
    # threshold = 0.60 * (1 - 0.40) = 0.36
    sig = tc.check_exit(tp_price=0.60, sl_price=0.35, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert abs(sig.effective_sl_pct - 0.40) < 1e-9
    assert abs(sig.threshold - 0.36) < 1e-9


def test_sl_tightens_on_second_reentry():
    """After 2 SLs (sl_count=2), sl_pct 50% → 30%."""
    tc = TradeConfig(sl_pct=0.50)
    state = _state(0.60, sl_count=2)
    # threshold = 0.60 * (1 - 0.30) = 0.42
    sig = tc.check_exit(tp_price=0.60, sl_price=0.41, state=state)
    assert sig is not None
    assert abs(sig.effective_sl_pct - 0.30) < 1e-9
    assert abs(sig.threshold - 0.42) < 1e-9


def test_sl_tightens_on_third_reentry():
    """After 3 SLs (sl_count=3), sl_pct 50% → 20%."""
    tc = TradeConfig(sl_pct=0.50)
    state = _state(0.60, sl_count=3)
    # threshold = 0.60 * (1 - 0.20) = 0.48
    sig = tc.check_exit(tp_price=0.60, sl_price=0.47, state=state)
    assert sig is not None
    assert abs(sig.effective_sl_pct - 0.20) < 1e-9


def test_sl_floor_at_5pct():
    """SL pct never drops below 5% floor."""
    tc = TradeConfig(sl_pct=0.20)
    state = _state(0.50, sl_count=10)
    # 0.20 - 0.10*10 = -0.80 → clamped to 0.05
    # threshold = 0.50 * (1 - 0.05) = 0.475
    sig = tc.check_exit(tp_price=0.60, sl_price=0.46, state=state)
    assert sig is not None
    assert abs(sig.effective_sl_pct - 0.05) < 1e-9
    assert abs(sig.threshold - 0.475) < 1e-9


def test_tightened_sl_does_not_trigger_when_above_threshold():
    """With sl_count=2, tightened SL threshold is higher — price above it = no exit."""
    tc = TradeConfig(sl_pct=0.50)
    state = _state(0.60, sl_count=2)
    # threshold = 0.60 * (1 - 0.30) = 0.42
    # price 0.43 > 0.42 → no SL trigger
    sig = tc.check_exit(tp_price=0.60, sl_price=0.43, state=state)
    assert sig is None


# ─── Absolute Price TP/SL ────────────────────────────────────────────────────

def test_absolute_tp_triggers():
    """Absolute TP at $0.80 triggers when price > 0.80."""
    tc = TradeConfig(tp_price=0.80, sl_pct=0.30)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.85, sl_price=0.50, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    assert abs(sig.threshold - 0.80) < 1e-9


def test_absolute_tp_no_trigger():
    """Absolute TP at $0.80 does not trigger at 0.79."""
    tc = TradeConfig(tp_price=0.80, sl_pct=0.30)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.79, sl_price=0.50, state=state)
    assert sig is None


def test_absolute_sl_triggers():
    """Absolute SL at $0.35 triggers when price < 0.35."""
    tc = TradeConfig(tp_pct=0.50, sl_price=0.35)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.30, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert abs(sig.threshold - 0.35) < 1e-9


def test_absolute_sl_no_trigger():
    """Absolute SL at $0.35 does not trigger at 0.36."""
    tc = TradeConfig(tp_pct=0.50, sl_price=0.35)
    state = _state(0.50)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.36, state=state)
    assert sig is None


def test_absolute_tp_and_sl_together():
    """Both TP and SL as absolute prices."""
    tc = TradeConfig(tp_price=0.80, sl_price=0.35)
    state = _state(0.50)
    # TP triggers
    sig = tc.check_exit(tp_price=0.85, sl_price=0.50, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    # SL triggers
    sig = tc.check_exit(tp_price=0.60, sl_price=0.30, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS


def test_mixed_absolute_tp_pct_sl():
    """Absolute TP + percentage SL."""
    tc = TradeConfig(tp_price=0.75, sl_pct=0.30)
    state = _state(0.50)
    # TP at $0.75 (absolute), SL at 0.50 * 0.70 = $0.35 (pct)
    sig = tc.check_exit(tp_price=0.76, sl_price=0.40, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    assert abs(sig.threshold - 0.75) < 1e-9

    sig = tc.check_exit(tp_price=0.60, sl_price=0.34, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert abs(sig.threshold - 0.35) < 1e-9


def test_mixed_pct_tp_absolute_sl():
    """Percentage TP + absolute SL."""
    tc = TradeConfig(tp_pct=0.50, sl_price=0.35)
    state = _state(0.50)
    # TP at 0.50 * 1.50 = $0.75 (pct), SL at $0.35 (absolute)
    sig = tc.check_exit(tp_price=0.76, sl_price=0.40, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.TAKE_PROFIT
    assert abs(sig.threshold - 0.75) < 1e-9


# ─── Absolute SL with progressive tightening ──────────────────────────────────

def test_absolute_sl_tightens_on_reentry():
    """Absolute SL $0.35, entry=$0.50, gap=$0.15.
    After 1 SL re-entry: gap shrinks 10% → $0.135, threshold=$0.365."""
    tc = TradeConfig(sl_price=0.35)
    state = _state(0.50, sl_count=1)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.36, state=state)
    assert sig is not None
    assert sig.reason == ExitReason.STOP_LOSS
    assert abs(sig.threshold - 0.365) < 1e-9
    # effective_sl_pct = 0.135 / 0.50 = 0.27
    assert abs(sig.effective_sl_pct - 0.27) < 1e-4


def test_absolute_sl_tightens_twice():
    """After 2 SL re-entries: gap=$0.15 * 0.80 = $0.12, threshold=$0.38."""
    tc = TradeConfig(sl_price=0.35)
    state = _state(0.50, sl_count=2)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.37, state=state)
    assert sig is not None
    assert abs(sig.threshold - 0.38) < 1e-9


def test_absolute_sl_floor():
    """Gap can't shrink below 5% of entry.
    SL=$0.35, entry=$0.50, gap=$0.15. With sl_count=10: gap * 0 = 0 → floor = 0.50 * 0.05 = $0.025.
    Threshold = $0.50 - $0.025 = $0.475."""
    tc = TradeConfig(sl_price=0.35)
    state = _state(0.50, sl_count=10)
    sig = tc.check_exit(tp_price=0.60, sl_price=0.47, state=state)
    assert sig is not None
    assert abs(sig.threshold - 0.475) < 1e-9


# ─── Absolute price takes priority ───────────────────────────────────────────

def test_absolute_tp_overrides_pct():
    """When both tp_price and tp_pct set, tp_price wins."""
    tc = TradeConfig(tp_pct=0.50, tp_price=0.70)
    state = _state(0.50)
    # pct would give 0.75, but absolute gives 0.70
    sig = tc.check_exit(tp_price=0.72, sl_price=0.50, state=state)
    assert sig is not None
    assert abs(sig.threshold - 0.70) < 1e-9


def test_absolute_sl_overrides_pct():
    """When both sl_price and sl_pct set, sl_price wins."""
    tc = TradeConfig(sl_pct=0.30, sl_price=0.40)
    state = _state(0.50)
    # pct would give 0.35, but absolute gives 0.40
    sig = tc.check_exit(tp_price=0.60, sl_price=0.39, state=state)
    assert sig is not None
    assert abs(sig.threshold - 0.40) < 1e-9
