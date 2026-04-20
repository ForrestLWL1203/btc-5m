"""Tests for BinanceTradeFeed feature computation."""

from collections import deque
from unittest.mock import patch

import pytest

from polybot.market.binance import BinanceTradeFeed


def _seed_feed(feed: BinanceTradeFeed, history: list[tuple[float, float]], flow: list[tuple[float, float, bool]]) -> None:
    feed._history_ts = [ts for ts, _ in history]
    feed._history_prices = [price for _, price in history]
    feed._history_start = 0
    feed._flow = deque(flow)
    feed._latest_ts = history[-1][0]
    feed._latest_price = history[-1][1]


def test_compute_features_returns_expected_windows():
    feed = BinanceTradeFeed()
    history = [
        (995.0, 100.0),
        (998.0, 102.0),
        (999.0, 103.0),
        (1000.0, 104.0),
    ]
    flow = [
        (999.3, 1.0, True),
        (999.7, 3.0, False),
        (999.8, 2.0, True),
    ]
    _seed_feed(feed, history, flow)

    with patch("polybot.market.binance.time.time", return_value=1000.0):
        features = feed.compute_features()

    assert features is not None
    assert features.btc_price == pytest.approx(104.0)
    assert features.ret_2s == pytest.approx((104.0 - 102.0) / 102.0 * 100)
    assert features.ret_5s == pytest.approx((104.0 - 100.0) / 100.0 * 100)
    assert features.velocity == pytest.approx((104.0 - 103.0) / 1.0)
    assert features.abs_vel == pytest.approx(abs(features.velocity))
    assert features.data_age_ms == pytest.approx(0.0)
    assert features.flow_imbalance == pytest.approx((2.0 - 3.0) / 5.0)


def test_compute_features_uses_history_start_without_copying():
    feed = BinanceTradeFeed()
    history = [
        (990.0, 90.0),
        (994.0, 95.0),
        (995.0, 100.0),
        (998.0, 102.0),
        (999.0, 103.0),
        (1000.0, 104.0),
    ]
    flow = [(999.9, 1.0, True)]
    _seed_feed(feed, history, flow)
    feed._history_start = 2

    with patch("polybot.market.binance.time.time", return_value=1000.0):
        features = feed.compute_features()

    assert features is not None
    assert features.ret_5s == pytest.approx((104.0 - 100.0) / 100.0 * 100)


def test_prune_history_and_flow_remove_old_data():
    feed = BinanceTradeFeed()
    history = [
        (100.0, 1.0),
        (105.0, 2.0),
        (111.0, 3.0),
    ]
    flow = [
        (107.0, 1.0, True),
        (109.5, 1.0, False),
        (111.0, 1.0, True),
    ]
    _seed_feed(feed, history, flow)

    feed._prune_history(111.0)
    feed._prune_flow(111.0)

    assert feed._history_start == 1
    assert list(feed._flow) == [
        (109.5, 1.0, False),
        (111.0, 1.0, True),
    ]
