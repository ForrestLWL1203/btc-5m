"""Tests for polybot.predict.indicators — EMA, RSI, trend, volume, MACD, Bollinger, ROC."""

import pytest
from polybot.predict.kline import KlineCandle
from polybot.predict.indicators import ema, rsi, trend_direction, volume_trend, macd, bollinger_pctb, price_roc


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


class TestMACD:
    def test_rising_prices_positive_histogram(self):
        """Strong uptrend → MACD histogram should be positive."""
        candles = [_candle(100 + i * 0.5) for i in range(40)]
        result = macd(candles, fast=12, slow=26, signal=9)
        assert result > 0.0

    def test_falling_prices_negative_histogram(self):
        """Strong downtrend → MACD histogram should be negative."""
        candles = [_candle(200 - i * 0.5) for i in range(40)]
        result = macd(candles, fast=12, slow=26, signal=9)
        assert result < 0.0

    def test_insufficient_data_returns_zero(self):
        assert macd([], fast=12, slow=26, signal=9) == 0.0

    def test_too_few_candles_returns_zero(self):
        candles = [_candle(100 + i) for i in range(10)]
        assert macd(candles, fast=12, slow=26, signal=9) == 0.0


class TestBollingerPctB:
    def test_price_at_middle_band(self):
        """Flat prices → %B should be ~0.5."""
        candles = [_candle(100.0) for _ in range(25)]
        result = bollinger_pctb(candles, period=20, num_std=2.0)
        assert abs(result - 0.5) < 0.01

    def test_price_above_upper_band(self):
        """Last price above upper band → %B > 1.0."""
        candles = [_candle(100.0 + i * 0.1) for i in range(19)] + [_candle(110.0)]
        result = bollinger_pctb(candles, period=20, num_std=2.0)
        assert result > 1.0

    def test_price_below_lower_band(self):
        """Last price below lower band → %B < 0.0."""
        candles = [_candle(100.0 + i * 0.1) for i in range(19)] + [_candle(90.0)]
        result = bollinger_pctb(candles, period=20, num_std=2.0)
        assert result < 0.0

    def test_insufficient_data_returns_half(self):
        assert bollinger_pctb([], period=20) == 0.5


class TestPriceROC:
    def test_rising_prices_positive(self):
        candles = [_candle(100 + i) for i in range(15)]
        result = price_roc(candles, period=10)
        assert result > 0.0

    def test_falling_prices_negative(self):
        candles = [_candle(200 - i) for i in range(15)]
        result = price_roc(candles, period=10)
        assert result < 0.0

    def test_insufficient_data_returns_zero(self):
        assert price_roc([], period=10) == 0.0

    def test_exact_period_matches(self):
        """11 candles, period=10 → should compute."""
        candles = [_candle(100 + i) for i in range(11)]
        result = price_roc(candles, period=10)
        expected = (candles[-1].close - candles[0].close) / candles[0].close
        assert abs(result - expected) < 1e-9
