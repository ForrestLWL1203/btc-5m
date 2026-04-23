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


def test_should_buy_uses_base_cap_when_dynamic_cap_disabled():
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


def test_should_buy_uses_strong_cap_for_strong_signal():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        strong_signal_threshold=1.5,
        strong_signal_max_entry_price=0.67,
        persistence_sec=10,
    )
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 104.6  # +4.6% => 1.53x theta
    strat._feed.price_at_or_before = lambda ts: 104.0

    state = MonitorState()

    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.67)
    assert state.target_signal_confidence == "strong"
