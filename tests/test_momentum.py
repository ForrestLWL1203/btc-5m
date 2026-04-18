"""Tests for polybot.predict.momentum V2 — Binance K-line signals."""

import pytest
from polybot.predict.kline import KlineCandle
from polybot.predict.momentum import MomentumPredictor, DirectionPredictor
from polybot.market.series import MarketSeries


def _btc_5m() -> MarketSeries:
    return MarketSeries.from_known("btc-updown-5m")


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


class TestMomentumPredictorV2:
    def test_predict_up_on_rising_prices(self):
        """Consistently rising prices → predict 'up'."""
        p = MomentumPredictor(_btc_5m())
        candles = [_candle(100 + i * 2, offset=i) for i in range(20)]
        assert p.predict(candles) == "up"

    def test_predict_down_on_falling_prices(self):
        """Consistently falling prices with bearish candles → predict 'down'."""
        p = MomentumPredictor(_btc_5m())
        candles = [_candle(140 - i * 2, open_val=141 - i * 2, offset=i) for i in range(20)]
        assert p.predict(candles) == "down"

    def test_predict_none_on_insufficient_data(self):
        p = MomentumPredictor(_btc_5m())
        candles = [_candle(100, offset=i) for i in range(3)]
        assert p.predict(candles) is None

    def test_predict_none_on_empty(self):
        p = MomentumPredictor(_btc_5m())
        assert p.predict([]) is None

    def test_is_direction_predictor_subclass(self):
        assert issubclass(MomentumPredictor, DirectionPredictor)

    def test_timeframe_scaling_5m(self):
        p = MomentumPredictor(_btc_5m())
        assert p.trend_n == 12
        assert p.ema_short == 10
        assert p.ema_long == 30

    def test_timeframe_scaling_4h(self):
        p = MomentumPredictor(MarketSeries.from_known("btc-updown-4h"))
        assert p.trend_n == 6
        assert p.ema_short == 8
        assert p.ema_long == 20
