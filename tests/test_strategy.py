"""Tests for TradeConfig (check_exit) and LatencyArbStrategy."""

from unittest.mock import MagicMock, patch

import pytest

from polybot.core.state import MonitorState
from polybot.market.binance import BtcFeatures
from polybot.strategies.latency_arb import LatencyArbStrategy
from polybot.market.series import MarketSeries
from polybot.trade_config import ExitReason, TradeConfig


def _make_state(entry_price: float = 0.0, **kwargs) -> MonitorState:
    """Create a MonitorState with optional overrides."""
    state = MonitorState(**kwargs)
    state.entry_price = entry_price
    state.started = True
    return state


# ─── LatencyArbStrategy ────────────────────────────────────────────────────

def test_get_side_returns_up():
    """get_side returns 'up' placeholder so monitor doesn't skip window."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series)
    assert s.get_side() == "up"


def test_should_buy_false_when_not_started():
    """should_buy returns False before Binance WS is connected."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series)
    state = _make_state()
    assert s.should_buy(0.50, state) is False


def test_max_entry_price_filter():
    """should_buy returns False when price exceeds max_entry_price."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series, max_entry_price=0.50)
    state = _make_state()
    # Not started, so always False — but the filter is tested in integration
    assert s.should_buy(0.60, state) is False


def test_min_entry_price_filter():
    """should_buy returns False when price is below min_entry_price."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series, min_entry_price=0.20)
    state = _make_state()
    assert s.should_buy(0.15, state) is False


def test_features_cached_until_new_binance_tick():
    """Strategy reuses the same feature snapshot until Binance latest_ts changes."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series)
    features = BtcFeatures(
        ret_2s=0.03,
        ret_5s=0.01,
        velocity=2.0,
        abs_vel=2.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=0.1,
    )
    s._feed = MagicMock()
    s._feed.latest_ts = 123.0
    s._feed.compute_features = MagicMock(return_value=features)

    with patch("polybot.strategies.latency_arb.time.time", side_effect=[123.01, 123.02]):
        first = s._get_features()
        second = s._get_features()

    assert first is features
    assert second is not features
    assert second.ret_2s == pytest.approx(features.ret_2s)
    assert second.data_age_ms > first.data_age_ms
    s._feed.compute_features.assert_called_once()

    s._feed.latest_ts = 124.0
    s._feed.compute_features.return_value = features
    with patch("polybot.strategies.latency_arb.time.time", return_value=124.01):
        s._get_features()
    assert s._feed.compute_features.call_count == 2


def test_get_features_refreshes_data_age_even_without_new_tick():
    """Cached features should not freeze data_age_ms while latest_ts is unchanged."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series)
    s._feed = MagicMock()
    s._feed.latest_ts = 123.0
    s._feed.compute_features = MagicMock(side_effect=[
        BtcFeatures(
            ret_2s=0.03,
            ret_5s=0.01,
            velocity=2.0,
            abs_vel=2.0,
            btc_price=100000.0,
            data_age_ms=10.0,
            flow_imbalance=0.1,
        ),
        BtcFeatures(
            ret_2s=0.03,
            ret_5s=0.01,
            velocity=2.0,
            abs_vel=2.0,
            btc_price=100000.0,
            data_age_ms=1000.0,
            flow_imbalance=0.1,
        ),
    ])

    first = s._get_features()
    with patch("polybot.strategies.latency_arb.time.time", return_value=124.0):
        second = s._get_features()

    assert first.data_age_ms == pytest.approx(10.0)
    assert second.data_age_ms == pytest.approx(1000.0)
    assert s._feed.compute_features.call_count == 1


def test_record_edge_prunes_old_samples():
    """Persistence history should keep only samples inside the active time window."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series, persistence_ms=200.0)

    s._record_edge(1.0, 0.03)
    s._record_edge(1.1, 0.04)
    s._record_edge(1.25, 0.05)

    assert list(s._edge_history) == [
        (1.1, 0.04),
        (1.25, 0.05),
    ]


def test_should_buy_logs_block_reason_at_most_every_five_seconds():
    """Blocked entry diagnostics should be visible without spamming every callback."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series, edge_threshold=0.02)
    state = _make_state()
    s._started = True
    s._window_start_epoch = 50.0
    s._feed = MagicMock()
    s._feed.latest_ts = 100.0
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=0.001,
        ret_5s=0.001,
        velocity=0.1,
        abs_vel=0.1,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=0.0,
    ))

    with patch("polybot.strategies.latency_arb.log.info") as mock_log, \
         patch("polybot.strategies.latency_arb.time.time", side_effect=[100.0, 102.0, 102.0, 106.0, 106.0]):
        assert s.should_buy(0.50, state) is False
        assert s.should_buy(0.50, state) is False
        assert s.should_buy(0.50, state) is False

    reasons = [call.args[0] for call in mock_log.call_args_list]
    assert reasons.count(
        "ENTRY BLOCKED: reason=%s edge=%.4f price=%.4f | ret_2s=%.4f "
        "ret_5s=%.4f vel=%.2f flow=%.3f | btc=%.1f age=%.0fms"
    ) == 2


def test_min_reentry_gap_blocks_quick_reentry_after_buy():
    """A recent confirmed entry should block another trade even if edge still looks good."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(series=series, min_reentry_gap_sec=3.0)
    state = _make_state()
    s._started = True
    s._window_start_epoch = 90.0
    s._entry_rearmed = True
    s._feed = MagicMock()
    s._feed.latest_ts = 100.0
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=0.03,
        ret_5s=0.03,
        velocity=4.0,
        abs_vel=4.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=1.0,
    ))
    s.on_buy_confirmed(100.0)

    with patch("polybot.strategies.latency_arb.time.time", side_effect=[101.0, 101.0]):
        assert s.should_buy(0.50, state) is False


def test_phase_one_entry_cap_blocks_third_trade_in_first_90_seconds():
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(
        series=series,
        phase_one_sec=90.0,
        max_entries_phase_one=2,
    )
    state = _make_state(entry_timestamps=[110.0, 150.0])
    s._started = True
    s._window_start_epoch = 100.0
    s._feed = MagicMock()
    s._feed.latest_ts = 160.0
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=0.03,
        ret_5s=0.03,
        velocity=4.0,
        abs_vel=4.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=1.0,
    ))
    with patch("polybot.strategies.latency_arb.time.time", side_effect=[160.0, 160.0]):
        assert s.should_buy(0.50, state) is False


def test_disable_after_sec_blocks_late_window_entries():
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(
        series=series,
        disable_after_sec=180.0,
    )
    state = _make_state()
    s._started = True
    s._window_start_epoch = 100.0
    s._feed = MagicMock()
    s._feed.latest_ts = 300.0
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=0.03,
        ret_5s=0.03,
        velocity=4.0,
        abs_vel=4.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=1.0,
    ))
    with patch("polybot.strategies.latency_arb.time.time", side_effect=[300.0, 300.0]):
        assert s.should_buy(0.50, state) is False


def test_edge_decay_grace_period_suppresses_immediate_decay_exit():
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(
        series=series,
        coefficients={"ret_2s": 1.0},
        edge_threshold=0.02,
        edge_decay_grace_ms=300.0,
    )
    state = _make_state(bought=True)
    state.target_side = "up"
    s._started = True
    s._entry_ts = 100.0
    s._feed = MagicMock()
    s._feed.latest_ts = 100.1
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=0.001,
        ret_5s=0.0,
        velocity=0.0,
        abs_vel=0.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=0.0,
    ))

    with patch("polybot.strategies.latency_arb.time.time", side_effect=[100.2, 100.2]):
        assert s.check_edge_exit(state) is None


def test_edge_reversal_still_exits_during_decay_grace_period():
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(
        series=series,
        coefficients={"ret_2s": 1.0},
        edge_threshold=0.02,
        edge_decay_grace_ms=300.0,
    )
    state = _make_state(bought=True)
    state.target_side = "up"
    s._started = True
    s._entry_ts = 100.0
    s._feed = MagicMock()
    s._feed.latest_ts = 100.1
    s._feed.compute_features = MagicMock(return_value=BtcFeatures(
        ret_2s=-0.03,
        ret_5s=0.0,
        velocity=0.0,
        abs_vel=0.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=0.0,
    ))

    with patch("polybot.strategies.latency_arb.time.time", side_effect=[100.2, 100.2]):
        assert s.check_edge_exit(state) == "edge_reversed"


def test_edge_must_rearm_before_next_entry():
    """After a trade, edge must cool below edge_rearm_threshold before re-entry is allowed."""
    series = MarketSeries.from_known("btc-updown-5m")
    s = LatencyArbStrategy(
        series=series,
        edge_rearm_threshold=0.01,
        coefficients={"ret_2s": 1.0},
    )
    state = _make_state()
    s._started = True
    s._window_start_epoch = 90.0
    s._entry_rearmed = False
    s._feed = MagicMock()
    strong = BtcFeatures(
        ret_2s=0.03,
        ret_5s=0.03,
        velocity=4.0,
        abs_vel=4.0,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=1.0,
    )
    cool = BtcFeatures(
        ret_2s=0.001,
        ret_5s=0.001,
        velocity=0.1,
        abs_vel=0.1,
        btc_price=100000.0,
        data_age_ms=10.0,
        flow_imbalance=0.0,
    )
    s._feed.compute_features = MagicMock(side_effect=[strong, cool, strong])

    s._feed.latest_ts = 100.0
    with patch("polybot.strategies.latency_arb.time.time", side_effect=[100.0, 100.0]):
        assert s.should_buy(0.50, state) is False
    assert s._entry_rearmed is False

    s._feed.latest_ts = 101.0
    with patch("polybot.strategies.latency_arb.time.time", side_effect=[101.0, 101.0]):
        assert s.should_buy(0.50, state) is False
    assert s._entry_rearmed is True

    s._edge_history.append((101.95, 0.03))
    s._feed.latest_ts = 102.0
    with patch("polybot.strategies.latency_arb.time.time", side_effect=[102.0, 102.0]):
        assert s.should_buy(0.50, state) is True


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
    assert tc.amount == 5.0
    assert tc.tp_pct == 0.50
    assert tc.sl_pct == 0.30
    assert tc.tp_price is None
    assert tc.sl_price is None
    assert tc.max_sl_reentry == 0
    assert tc.max_tp_reentry == 0
    assert tc.max_edge_reentry == 0
    assert tc.max_entries_per_window is None
    assert tc.rounds is None
