"""Tests for polybot.predict.indicators — EMA, RSI, trend, volume."""

import pytest
from polybot.predict.kline import KlineCandle
from polybot.predict.indicators import ema, rsi, trend_direction, volume_trend


def _candle(close: float, open_val=None, volume=100.0, offset=0) -> KlineCandle:
    o = open_val if open_val is not None else close - 1
    return KlineCandle(
        open_time=1000 + offset,
        open=o,
        high=max(o, close) + 0.5,
        low=min(o, close) - 0.5,
        close=close,
        volume=volume,
    )


class TestEMA:
    def test_rising_prices(self):
        candles = [_candle(c) for c in range(100, 110)]
        result = ema(candles, 5)
        assert result > 100  # EMA should be above first price

    def test_empty_returns_zero(self):
        assert ema([], 5) == 0.0

    def test_single_candle(self):
        candles = [_candle(100)]
        assert ema(candles, 5) == 100.0


class TestRSI:
    def test_all_up_returns_high(self):
        # All candles going up → RSI should be high (> 70)
        candles = [_candle(100 + i) for i in range(20)]
        assert rsi(candles, 14) > 70

    def test_all_down_returns_low(self):
        # All candles going down → RSI should be low (< 30)
        candles = [_candle(200 - i) for i in range(20)]
        assert rsi(candles, 14) < 30

    def test_insufficient_data(self):
        candles = [_candle(100), _candle(101)]
        assert rsi(candles, 14) == 50.0  # neutral

    def test_empty_returns_neutral(self):
        assert rsi([], 14) == 50.0


class TestTrendDirection:
    def test_all_bullish(self):
        # close > open for all
        candles = [_candle(100 + i, open_val=99 + i) for i in range(10)]
        assert trend_direction(candles, 10) == 1.0

    def test_all_bearish(self):
        # open > close for all
        candles = [_candle(99 + i, open_val=100 + i) for i in range(10)]
        assert trend_direction(candles, 10) == 0.0

    def test_mixed(self):
        candles = [
            _candle(101, open_val=100),  # bullish
            _candle(99, open_val=100),    # bearish
            _candle(102, open_val=100),   # bullish
            _candle(98, open_val=100),    # bearish
        ]
        assert trend_direction(candles, 4) == 0.5

    def test_insufficient_data(self):
        assert trend_direction([], 5) == 0.5  # neutral


class TestVolumeTrend:
    def test_increasing_volume(self):
        candles = [_candle(100, volume=100.0 + i * 10) for i in range(20)]
        result = volume_trend(candles, 10)
        assert result > 1.0  # recent volume > prior volume

    def test_decreasing_volume(self):
        candles = [_candle(100, volume=300.0 - i * 10) for i in range(20)]
        result = volume_trend(candles, 10)
        assert result < 1.0

    def test_insufficient_data(self):
        assert volume_trend([], 5) == 1.0
