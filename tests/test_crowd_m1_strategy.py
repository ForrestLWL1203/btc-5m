"""Tests for the crowd_m1 mid-window crowd-following strategy."""

import logging
import time
from unittest.mock import MagicMock

import pytest

from polybot.core.state import MonitorState
from polybot.market.series import MarketSeries
from polybot.strategies.crowd_m1 import CrowdM1Strategy


def _strategy(**kwargs) -> CrowdM1Strategy:
    series = MarketSeries.from_known("btc-updown-5m")
    strat = CrowdM1Strategy(series=series, **kwargs)
    strat._started = True
    strat._feed = MagicMock()
    strat._feed.price_at_or_before.return_value = 100.04
    return strat


def test_should_buy_higher_best_ask_side_when_gap_and_btc_confirm_match():
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16, max_entry_price=0.75)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    state = MonitorState()

    assert strat.should_buy(0.59, state) is True
    assert state.target_side == "up"
    assert state.target_max_entry_price == pytest.approx(0.75)
    assert state.signal_reference_price == pytest.approx(0.61)
    assert state.target_signal_strength == pytest.approx(0.18 / 0.16)
    assert strat.should_buy(0.59, MonitorState()) is False


def test_get_side_defers_direction_to_market_snapshot():
    strat = _strategy()

    assert strat.get_side() is None


def test_should_buy_rejects_if_btc_direction_disagrees():
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 99.95
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    assert strat.should_buy(0.59, MonitorState()) is False


def test_should_buy_allows_btc_direction_noise_inside_deadband():
    strat = _strategy(
        entry_elapsed_sec=120,
        min_ask_gap=0.16,
        btc_direction_deadband_pct=0.015,
        strong_move_pct=0.015,
    )
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.02
    strat._feed.price_at_or_before.return_value = 100.01
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    state = MonitorState()

    assert strat.should_buy(0.59, state) is True
    assert state.target_side == "up"


def test_should_buy_scans_dynamic_entry_band_until_strong_btc_move():
    strat = _strategy(
        entry_start_elapsed_sec=120,
        entry_end_elapsed_sec=180,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        strong_move_pct=0.06,
    )
    now = time.time()
    strat._window_start_epoch = now - 130
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat._feed.price_at_or_before.return_value = 100.04
    strat.set_market_snapshot(up_mid=0.66, down_mid=0.34, up_best_ask=0.67, down_best_ask=0.35)

    assert strat.should_buy(0.66, MonitorState()) is False

    strat._feed.latest_price = 100.07
    state = MonitorState()

    assert strat.should_buy(0.66, state) is True
    assert state.target_side == "up"


def test_should_buy_ignores_prior_btc_direction_after_current_move_confirms():
    strat = _strategy(
        entry_start_elapsed_sec=120,
        entry_end_elapsed_sec=180,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        strong_move_pct=0.06,
    )
    now = time.time()
    strat._window_start_epoch = now - 130
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.07
    strat._feed.price_at_or_before.return_value = 99.99
    strat.set_market_snapshot(up_mid=0.66, down_mid=0.34, up_best_ask=0.67, down_best_ask=0.35)

    state = MonitorState()

    assert strat.should_buy(0.66, state) is True
    assert state.target_side == "up"


def test_should_buy_ignores_prior_btc_move_size_after_current_move_confirms():
    strat = _strategy(
        entry_start_elapsed_sec=120,
        entry_end_elapsed_sec=180,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        strong_move_pct=0.06,
    )
    now = time.time()
    strat._window_start_epoch = now - 130
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.07
    strat._feed.price_at_or_before.return_value = 100.12
    strat.set_market_snapshot(up_mid=0.66, down_mid=0.34, up_best_ask=0.67, down_best_ask=0.35)

    state = MonitorState()

    assert strat.should_buy(0.66, state) is True
    assert state.target_side == "up"


def test_should_buy_rejects_gap_below_min_without_consuming_window():
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.53, down_mid=0.47, up_best_ask=0.56, down_best_ask=0.50)

    assert strat.should_buy(0.53, MonitorState()) is False

    strat.set_market_snapshot(up_mid=0.62, down_mid=0.38, up_best_ask=0.64, down_best_ask=0.40)
    assert strat.should_buy(0.62, MonitorState()) is True


def test_should_buy_rejects_before_entry_elapsed_without_consuming_window():
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 100
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    assert strat.should_buy(0.59, MonitorState()) is False

    strat._window_start_epoch = now - 121
    assert strat.should_buy(0.59, MonitorState()) is True


def test_should_buy_rejects_after_entry_timeout():
    strat = _strategy(entry_elapsed_sec=120, entry_timeout_sec=60, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 181
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    assert strat.should_buy(0.62, MonitorState()) is False


def test_should_buy_logs_gap_rejection_reason(caplog):
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.55, down_mid=0.45, up_best_ask=0.57, down_best_ask=0.47)

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.55, MonitorState()) is False
    assert "M1_DECISION_SKIP: reason=ask_gap_below_min" in caplog.text
    assert "up_mid=0.550" in caplog.text
    assert "down_mid=0.450" in caplog.text
    assert "up_best_ask=0.570" in caplog.text
    assert "down_best_ask=0.470" in caplog.text
    assert "leading_ask=0.570" in caplog.text
    assert "min_ask_gap=0.160" in caplog.text


def test_should_buy_logs_btc_mismatch_rejection_reason(caplog):
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 99.95
    strat.set_market_snapshot(up_mid=0.59, down_mid=0.41, up_best_ask=0.61, down_best_ask=0.43)

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.59, MonitorState()) is False
    assert "M1_DECISION_SKIP: reason=btc_direction_mismatch" in caplog.text
    assert "dir=UP" in caplog.text
    assert "btc_open=100.0" in caplog.text
    assert "btc_now=100.0" in caplog.text


def test_should_buy_logs_missing_market_snapshot_once(caplog):
    strat = _strategy(entry_elapsed_sec=120, min_ask_gap=0.16)
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.0, MonitorState()) is False
    assert strat.should_buy(0.0, MonitorState()) is False
    assert caplog.text.count("M1_DECISION_SKIP: reason=missing_market_snapshot") == 1


def test_should_buy_rejects_leading_ask_above_cap_without_signal_log(caplog):
    strat = _strategy(
        entry_elapsed_sec=120,
        entry_timeout_sec=5,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        max_entry_price=0.75,
        btc_direction_confirm=False,
    )
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.955, down_mid=0.045, up_best_ask=0.96, down_best_ask=0.05)

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.955, MonitorState()) is False
    assert strat.should_buy(0.955, MonitorState()) is False
    assert caplog.text.count("M1_DECISION_SKIP: reason=leading_ask_above_max_entry") == 1
    assert "CROWD_M1_SIGNAL" not in caplog.text


def test_should_buy_does_not_log_actionable_signal(caplog):
    strat = _strategy(
        entry_elapsed_sec=120,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        max_entry_price=0.75,
        btc_direction_confirm=False,
    )
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.66, down_mid=0.34, up_best_ask=0.67, down_best_ask=0.35)

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.66, MonitorState()) is True
    assert "CROWD_M1_SIGNAL" not in caplog.text


def test_should_buy_rejects_stale_cross_leg_best_ask(caplog):
    strat = _strategy(
        entry_elapsed_sec=120,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        max_entry_price=0.75,
        btc_direction_confirm=False,
    )
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(
        up_mid=0.66,
        down_mid=0.34,
        up_best_ask=0.67,
        down_best_ask=0.35,
        up_best_ask_age_sec=0.2,
        down_best_ask_age_sec=1.5,
    )

    caplog.set_level(logging.INFO, logger="polybot.strategies.crowd_m1")

    assert strat.should_buy(0.66, MonitorState()) is False
    assert "M1_DECISION_SKIP: reason=stale_cross_leg_book" in caplog.text
    assert "down_best_ask_age_ms=1500" in caplog.text


def test_should_buy_leaves_paired_theta_field_empty_for_crowd_gapless_signal():
    strat = _strategy(
        entry_elapsed_sec=120,
        min_ask_gap=0.0,
        min_leading_ask=0.62,
        max_entry_price=0.75,
        btc_direction_confirm=False,
    )
    now = time.time()
    strat._window_start_epoch = now - 121
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.05
    strat.set_market_snapshot(up_mid=0.66, down_mid=0.34, up_best_ask=0.67, down_best_ask=0.35)

    state = MonitorState()

    assert strat.should_buy(0.66, state) is True
    assert state.target_signal_strength is None
    assert state.target_active_theta_pct is None
