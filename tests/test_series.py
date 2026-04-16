"""Tests for polybot.market.series — MarketSeries dataclass and TIMEFRAME_SECONDS."""

import pytest

from polybot.market.series import MarketSeries, KNOWN_SERIES, TIMEFRAME_SECONDS, _default_buffer


# ─── TIMEFRAME_SECONDS ────────────────────────────────────────────────────────

def test_all_timeframes_defined():
    assert TIMEFRAME_SECONDS == {
        "5m": 300,
        "15m": 900,
        "1h": 3600,
        "4h": 14400,
        "1d": 86400,
    }


# ─── _default_buffer ──────────────────────────────────────────────────────────

def test_buffer_5m():
    assert _default_buffer(300) == 5

def test_buffer_15m():
    assert _default_buffer(900) == 15

def test_buffer_1h():
    assert _default_buffer(3600) == 60

def test_buffer_4h():
    assert _default_buffer(14400) == 240

def test_buffer_1d():
    assert _default_buffer(86400) == 1440


# ─── MarketSeries ─────────────────────────────────────────────────────────────

def test_from_known_btc_5m():
    s = MarketSeries.from_known("btc-updown-5m")
    assert s.asset == "btc"
    assert s.timeframe == "5m"
    assert s.slug_prefix == "btc-updown-5m"
    assert s.slug_step == 300
    assert s.window_end_buffer == 5


def test_from_known_unknown_key_raises():
    with pytest.raises(KeyError):
        MarketSeries.from_known("dogecoin-updown-1w")


@pytest.mark.parametrize("key,asset,timeframe,slug_step,buffer", [
    ("btc-updown-15m", "btc", "15m", 900, 15),
    ("btc-updown-4h", "btc", "4h", 14400, 240),
    ("eth-updown-5m", "eth", "5m", 300, 5),
    ("eth-updown-15m", "eth", "15m", 900, 15),
    ("eth-updown-4h", "eth", "4h", 14400, 240),
])
def test_from_known_new_markets(key, asset, timeframe, slug_step, buffer):
    s = MarketSeries.from_known(key)
    assert s.asset == asset
    assert s.timeframe == timeframe
    assert s.slug_prefix == key
    assert s.slug_step == slug_step
    assert s.window_end_buffer == buffer


def test_epoch_to_slug():
    s = MarketSeries.from_known("btc-updown-5m")
    assert s.epoch_to_slug(1776182700) == "btc-updown-5m-1776182700"


def test_series_key():
    s = MarketSeries.from_known("btc-updown-5m")
    assert s.series_key == "btc-updown-5m"


def test_frozen():
    """MarketSeries is frozen — cannot mutate attributes."""
    s = MarketSeries.from_known("btc-updown-5m")
    with pytest.raises(AttributeError):
        s.asset = "eth"


def test_manual_construction():
    """Can construct MarketSeries without KNOWN_SERIES."""
    s = MarketSeries(
        asset="eth",
        timeframe="15m",
        slug_prefix="eth-updown-15m",
        slug_step=900,
        window_end_buffer=15,
    )
    assert s.asset == "eth"
    assert s.slug_step == 900
    assert s.epoch_to_slug(1234567890) == "eth-updown-15m-1234567890"
