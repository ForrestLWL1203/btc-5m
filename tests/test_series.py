"""Tests for active BTC 5-minute MarketSeries."""

import pytest

from polybot.market.series import (
    ACTIVE_SERIES_KEY,
    ACTIVE_WINDOW_END_BUFFER,
    ACTIVE_WINDOW_SECONDS,
    KNOWN_SERIES,
    MarketSeries,
)


def test_from_known_btc_5m():
    s = MarketSeries.from_known(ACTIVE_SERIES_KEY)
    assert s.asset == "btc"
    assert s.timeframe == "5m"
    assert s.slug_prefix == ACTIVE_SERIES_KEY
    assert s.slug_step == ACTIVE_WINDOW_SECONDS
    assert s.window_end_buffer == ACTIVE_WINDOW_END_BUFFER


def test_only_active_series_is_known():
    assert set(KNOWN_SERIES) == {ACTIVE_SERIES_KEY}


def test_from_known_unknown_key_raises():
    with pytest.raises(KeyError):
        MarketSeries.from_known("other-updown-5m")


def test_epoch_to_slug():
    s = MarketSeries.from_known(ACTIVE_SERIES_KEY)
    assert s.epoch_to_slug(1776182700) == "btc-updown-5m-1776182700"


def test_series_key():
    s = MarketSeries.from_known(ACTIVE_SERIES_KEY)
    assert s.series_key == ACTIVE_SERIES_KEY


def test_frozen():
    s = MarketSeries.from_known(ACTIVE_SERIES_KEY)
    with pytest.raises(AttributeError):
        s.asset = "btc"


def test_manual_construction_for_focused_tests():
    s = MarketSeries(
        asset="btc",
        timeframe="5m",
        slug_prefix=ACTIVE_SERIES_KEY,
        slug_step=ACTIVE_WINDOW_SECONDS,
        window_end_buffer=ACTIVE_WINDOW_END_BUFFER,
    )
    assert s.asset == "btc"
    assert s.slug_step == ACTIVE_WINDOW_SECONDS
    assert s.epoch_to_slug(1234567890) == "btc-updown-5m-1234567890"
