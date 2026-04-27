"""Tests for paired_window strategy signal behavior."""

import time
from unittest.mock import MagicMock

import pytest

from polybot.core.state import MonitorState
from polybot.market.series import MarketSeries
from polybot.strategies.paired_window import PairedWindowStrategy


def _strategy(**kwargs) -> PairedWindowStrategy:
    series = MarketSeries.from_known("btc-updown-5m")
    strat = PairedWindowStrategy(series=series, **kwargs)
    strat._started = True
    strat._feed = MagicMock()
    return strat


def test_should_buy_uses_fixed_cap():
    strat = _strategy(theta_pct=0.03, max_entry_price=0.65, persistence_sec=10)
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 103.0
    strat._feed.price_at_or_before = lambda ts: 102.5

    state = MonitorState()

    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.65)
    assert state.target_signal_confidence == "normal"
    assert state.target_signal_strength == pytest.approx(100.0)
    assert state.target_past_signal_strength == pytest.approx(83.3333333333)
    assert state.target_remaining_sec == pytest.approx(240.0, abs=1.0)


def test_should_buy_rejects_before_entry_band():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        persistence_sec=10,
        entry_start_remaining_sec=240.0,
    )
    now = time.time()
    strat._window_start_epoch = now - 30
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.10
    strat._feed.price_at_or_before = lambda ts: 100.08

    state = MonitorState()

    assert strat.should_buy(0.60, state) is False


def test_should_keep_direction_locked_within_window():
    strat = _strategy(theta_pct=0.03, max_entry_price=0.65, persistence_sec=10)
    now = time.time()
    strat._window_start_epoch = now - 80
    strat._window_open_btc = 100.0
    state = MonitorState()

    strat._feed.latest_price = 100.04
    strat._feed.price_at_or_before = lambda ts: 100.035
    assert strat.should_buy(0.60, state) is True
    assert state.target_side == "up"

    strat._feed.latest_price = 99.96
    strat._feed.price_at_or_before = lambda ts: 99.965
    assert strat.should_buy(0.60, state) is False
    assert strat._committed_direction == "up"


def test_should_buy_rejects_if_past_move_direction_differs():
    strat = _strategy(theta_pct=0.03, max_entry_price=0.65, persistence_sec=10)
    now = time.time()
    strat._window_start_epoch = now - 80
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.04
    strat._feed.price_at_or_before = lambda ts: 99.98

    assert strat.should_buy(0.60, MonitorState()) is False


def test_should_buy_rejects_if_current_move_fades_too_much():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        persistence_sec=10,
        min_move_ratio=0.7,
    )
    now = time.time()
    strat._window_start_epoch = now - 80
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.04
    strat._feed.price_at_or_before = lambda ts: 100.10

    assert strat.should_buy(0.60, MonitorState()) is False
