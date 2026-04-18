"""DirectionPredictor ABC and MomentumPredictor V2 — Binance K-line signals."""

from abc import ABC, abstractmethod
from typing import List, Optional

from polybot.market.series import MarketSeries
from .indicators import ema, rsi, trend_direction, volume_trend
from .kline import KlineCandle


class DirectionPredictor(ABC):
    """Abstract direction predictor — returns 'up', 'down', or None (skip)."""

    @abstractmethod
    def predict(self, candles: List[KlineCandle]) -> Optional[str]:
        ...


class MomentumPredictor(DirectionPredictor):
    """V2 predictor: weighted voting on 4 technical indicators.

    Signals:
      1. Short-term trend (40%) — fraction of bullish candles
      2. EMA crossover (30%) — short EMA vs long EMA
      3. RSI (20%) — oversold → up, overbought → down
      4. Volume confirmation (10%) — volume trend direction
    """

    def __init__(self, series: MarketSeries):
        self.fallback_side = None

        if series.slug_step <= 300:  # 5m
            self.trend_n = 12
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 15
        elif series.slug_step <= 900:  # 15m
            self.trend_n = 8
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 30
        else:  # 4h
            self.trend_n = 6
            self.ema_short = 8
            self.ema_long = 20
            self.rsi_period = 14
            self.min_candles = 20

    def predict(self, candles: List[KlineCandle]) -> Optional[str]:
        if len(candles) < self.min_candles:
            return self.fallback_side

        score = 0.0
        score += self._trend_signal(candles) * 0.40
        score += self._ema_signal(candles) * 0.30
        score += self._rsi_signal(candles) * 0.20
        score += self._volume_signal(candles) * 0.10

        if score > 0:
            return "up"
        elif score < 0:
            return "down"
        return self.fallback_side

    def _trend_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = bullish trend. Range: [-1, 1]."""
        td = trend_direction(candles, self.trend_n)
        return (td - 0.5) * 2.0  # map [0,1] → [-1,1]

    def _ema_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = short EMA above long EMA."""
        short = ema(candles, self.ema_short)
        long = ema(candles, self.ema_long)
        if long == 0:
            return 0.0
        return (short - long) / long * 10.0  # scale up for sensitivity

    def _rsi_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = bet on up. RSI < 40 = oversold → buy up. RSI > 60 = overbought → buy down."""
        r = rsi(candles, self.rsi_period)
        if r < 40:
            return (40 - r) / 40.0  # 0..1
        elif r > 60:
            return -(r - 60) / 40.0  # -1..0
        return 0.0

    def _volume_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = volume confirming uptrend."""
        vt = volume_trend(candles, self.trend_n)
        td = trend_direction(candles, self.trend_n)
        if td > 0.5 and vt > 1.0:
            return min(vt - 1.0, 1.0)
        elif td < 0.5 and vt > 1.0:
            return -min(vt - 1.0, 1.0)
        return 0.0
