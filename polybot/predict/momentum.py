"""DirectionPredictor ABC and MomentumPredictor V3 — 7 technical indicators."""

from abc import ABC, abstractmethod
from typing import List, Optional

from polybot.market.series import MarketSeries
from .indicators import bollinger_pctb, ema, macd, price_roc, rsi, trend_direction, volume_trend
from .kline import KlineCandle


class DirectionPredictor(ABC):
    """Abstract direction predictor — returns 'up', 'down', or None (skip)."""

    @abstractmethod
    def predict(self, candles: List[KlineCandle]) -> Optional[str]:
        ...


class MomentumPredictor(DirectionPredictor):
    """V3 predictor: weighted voting on 7 technical indicators.

    Signals:
      1. Trend direction (20%)
      2. EMA crossover (15%)
      3. RSI (10%)
      4. Volume confirmation (5%)
      5. MACD histogram (20%)
      6. Bollinger %B (15%)
      7. Price ROC (15%)
    """

    def __init__(self, series: MarketSeries):
        self.fallback_side = None

        if series.slug_step <= 300:  # 5m
            self.trend_n = 12
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 15
            self.macd_fast = 12
            self.macd_slow = 26
            self.macd_signal = 9
            self.boll_period = 20
            self.boll_std = 2.0
            self.roc_period = 10
        elif series.slug_step <= 900:  # 15m
            self.trend_n = 8
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 30
            self.macd_fast = 12
            self.macd_slow = 26
            self.macd_signal = 9
            self.boll_period = 20
            self.boll_std = 2.0
            self.roc_period = 8
        else:  # 4h
            self.trend_n = 6
            self.ema_short = 8
            self.ema_long = 20
            self.rsi_period = 14
            self.min_candles = 15
            self.macd_fast = 6
            self.macd_slow = 13
            self.macd_signal = 5
            self.boll_period = 12
            self.boll_std = 2.0
            self.roc_period = 6

    def predict(self, candles: List[KlineCandle]) -> Optional[str]:
        if len(candles) < self.min_candles:
            return self.fallback_side

        score = 0.0
        score += self._trend_signal(candles) * 0.20
        score += self._ema_signal(candles) * 0.15
        score += self._rsi_signal(candles) * 0.10
        score += self._volume_signal(candles) * 0.05
        score += self._macd_signal(candles) * 0.20
        score += self._bollinger_signal(candles) * 0.15
        score += self._roc_signal(candles) * 0.15

        if score > 0:
            return "up"
        elif score < 0:
            return "down"
        return self.fallback_side

    def _trend_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = bullish trend. Range: [-1, 1]."""
        td = trend_direction(candles, self.trend_n)
        return (td - 0.5) * 2.0

    def _ema_signal(self, candles: List[KlineCandle]) -> float:
        """Positive = short EMA above long EMA."""
        short = ema(candles, self.ema_short)
        long = ema(candles, self.ema_long)
        if long == 0:
            return 0.0
        return max(-1.0, min(1.0, (short - long) / long * 10.0))

    def _rsi_signal(self, candles: List[KlineCandle]) -> float:
        """RSI < 40 = oversold → buy up. RSI > 60 = overbought → buy down."""
        r = rsi(candles, self.rsi_period)
        if r < 40:
            return (40 - r) / 40.0
        elif r > 60:
            return -(r - 60) / 40.0
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

    def _macd_signal(self, candles: List[KlineCandle]) -> float:
        """MACD histogram → momentum direction. Range: [-1, 1]."""
        h = macd(candles, self.macd_fast, self.macd_slow, self.macd_signal)
        price = candles[-1].close
        if price == 0:
            return 0.0
        normalized = h / price * 100.0
        return max(-1.0, min(1.0, normalized))

    def _bollinger_signal(self, candles: List[KlineCandle]) -> float:
        """Bollinger %B → trend-following. Above band = strong momentum."""
        pctb = bollinger_pctb(candles, self.boll_period, self.boll_std)
        return max(-1.0, min(1.0, (pctb - 0.5) * 2.0))

    def _roc_signal(self, candles: List[KlineCandle]) -> float:
        """Price rate of change → direct momentum. 2% move = full signal."""
        roc = price_roc(candles, self.roc_period)
        return max(-1.0, min(1.0, roc * 50.0))
