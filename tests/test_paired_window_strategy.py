"""Tests for paired_window strategy signal behavior."""

import logging
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
    assert state.target_signal_strength == pytest.approx(100.0)
    assert state.target_past_signal_strength == pytest.approx(83.3333333333)
    assert state.target_remaining_sec == pytest.approx(240.0, abs=1.0)


def test_should_buy_rejects_early_window_without_early_entry_config():
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


def test_should_buy_allows_early_window_for_strong_signal():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        persistence_sec=10,
        entry_start_remaining_sec=240.0,
        early_entry_start_remaining_sec=270.0,
        early_entry_strength_threshold=2.5,
        early_entry_past_strength_threshold=1.5,
    )
    now = time.time()
    strat._window_start_epoch = now - 30
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.10  # 3.33x
    strat._feed.price_at_or_before = lambda ts: 100.06  # 2.00x

    state = MonitorState()

    assert strat.should_buy(0.60, state) is True
    assert state.target_side == "up"


def test_should_buy_rejects_early_window_when_past_strength_too_low():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        persistence_sec=10,
        entry_start_remaining_sec=240.0,
        early_entry_start_remaining_sec=270.0,
        early_entry_strength_threshold=2.5,
        early_entry_past_strength_threshold=1.5,
    )
    now = time.time()
    strat._window_start_epoch = now - 30
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.10  # 3.33x
    strat._feed.price_at_or_before = lambda ts: 100.03  # 1.00x

    state = MonitorState()

    assert strat.should_buy(0.60, state) is False


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


def test_should_buy_uses_strength_caps_mid_tier():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        strength_caps=[
            {"threshold": 1.5, "max_entry_price": 0.70},
            {"threshold": 3.5, "max_entry_price": 0.75},
        ],
        persistence_sec=10,
    )
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.054  # +0.054% => 1.8x theta
    strat._feed.price_at_or_before = lambda ts: 100.048

    state = MonitorState()

    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.70)
    assert state.target_signal_confidence == "strong"


def test_should_buy_uses_strength_caps_high_tier():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        strength_caps=[
            {"threshold": 1.5, "max_entry_price": 0.70},
            {"threshold": 3.5, "max_entry_price": 0.75},
        ],
        persistence_sec=10,
    )
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    strat._feed.latest_price = 100.110  # +0.110% => 3.67x theta
    strat._feed.price_at_or_before = lambda ts: 100.090

    state = MonitorState()

    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.75)
    assert state.target_signal_confidence == "strong"


def test_should_log_cap_escalation_when_strength_crosses_new_tier(caplog):
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        strength_caps=[
            {"threshold": 1.5, "max_entry_price": 0.70},
            {"threshold": 3.5, "max_entry_price": 0.75},
        ],
        persistence_sec=10,
    )
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    state = MonitorState()

    with caplog.at_level(logging.INFO):
        strat._feed.latest_price = 100.054  # 1.8x
        strat._feed.price_at_or_before = lambda ts: 100.048
        assert strat.should_buy(0.60, state) is True
        assert state.target_max_entry_price == pytest.approx(0.70)

        strat._feed.latest_price = 100.120  # 4.0x
        strat._feed.price_at_or_before = lambda ts: 100.110
        assert strat.should_buy(0.60, state) is True
        assert state.target_max_entry_price == pytest.approx(0.75)

    assert "CAP_ESCALATED:" in caplog.text


def test_should_keep_strength_cap_after_signal_strength_fades():
    strat = _strategy(
        theta_pct=0.03,
        max_entry_price=0.65,
        strength_caps=[
            {"threshold": 1.5, "max_entry_price": 0.70},
            {"threshold": 3.5, "max_entry_price": 0.75},
        ],
        persistence_sec=10,
    )
    now = time.time()
    strat._window_start_epoch = now - 60
    strat._window_open_btc = 100.0
    state = MonitorState()

    strat._feed.latest_price = 100.054  # 1.8x
    strat._feed.price_at_or_before = lambda ts: 100.048
    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.70)

    strat._feed.latest_price = 100.040  # 1.33x, still valid signal but below cap tier
    strat._feed.price_at_or_before = lambda ts: 100.035
    assert strat.should_buy(0.60, state) is True
    assert state.target_max_entry_price == pytest.approx(0.70)
    assert state.target_signal_confidence == "strong"


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
