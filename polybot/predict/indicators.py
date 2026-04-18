"""Technical indicators computed from K-line candle data.

All functions are pure: input list[KlineCandle], output float.
Return neutral values when data is insufficient.
"""

import math
from typing import List

from .kline import KlineCandle


def _ema_series(candles: List[KlineCandle], period: int) -> List[float]:
    """EMA of close prices at each candle position."""
    if not candles:
        return []
    multiplier = 2.0 / (period + 1)
    result = [candles[0].close]
    for c in candles[1:]:
        result.append(c.close * multiplier + result[-1] * (1 - multiplier))
    return result


def _ema_from_values(values: List[float], period: int) -> float:
    """EMA of an arbitrary list of floats. Returns 0.0 if empty."""
    if not values:
        return 0.0
    multiplier = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = v * multiplier + result * (1 - multiplier)
    return result


def ema(candles: List[KlineCandle], period: int) -> float:
    """Exponential moving average of close prices."""
    if not candles:
        return 0.0
    if len(candles) == 1:
        return candles[0].close

    multiplier = 2.0 / (period + 1)
    result = candles[0].close
    for c in candles[1:]:
        result = c.close * multiplier + result * (1 - multiplier)
    return result


def rsi(candles: List[KlineCandle], period: int = 14) -> float:
    """Relative Strength Index (0-100). Returns 50.0 if insufficient data."""
    if len(candles) < period + 1:
        return 50.0

    gains = []
    losses = []
    for i in range(1, len(candles)):
        change = candles[i].close - candles[i - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    # Use simple average for initial values
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    # Wilder's smoothing for remaining values
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def trend_direction(candles: List[KlineCandle], n: int) -> float:
    """Fraction of last N candles where close > open. Returns 0.5 if insufficient."""
    if len(candles) < n or n == 0:
        return 0.5
    recent = candles[-n:]
    bullish = sum(1 for c in recent if c.close > c.open)
    return bullish / n


def volume_trend(candles: List[KlineCandle], n: int) -> float:
    """Ratio of recent N candles volume vs prior N candles. Returns 1.0 if insufficient."""
    if len(candles) < n * 2 or n == 0:
        return 1.0
    recent_vol = sum(c.volume for c in candles[-n:])
    prior_vol = sum(c.volume for c in candles[-n * 2:-n])
    if prior_vol == 0:
        return 1.0
    return recent_vol / prior_vol


def macd(candles: List[KlineCandle], fast: int = 12, slow: int = 26, signal: int = 9) -> float:
    """MACD histogram (MACD line - signal line). Returns 0.0 if insufficient data."""
    if len(candles) < slow + signal:
        return 0.0

    fast_series = _ema_series(candles, fast)
    slow_series = _ema_series(candles, slow)
    macd_line = [f - s for f, s in zip(fast_series, slow_series)]
    signal_val = _ema_from_values(macd_line, signal)
    return macd_line[-1] - signal_val


def bollinger_pctb(candles: List[KlineCandle], period: int = 20, num_std: float = 2.0) -> float:
    """Bollinger Band %B: price position relative to bands. Returns 0.5 if insufficient."""
    if len(candles) < period:
        return 0.5

    recent_closes = [c.close for c in candles[-period:]]
    sma = sum(recent_closes) / period
    variance = sum((c - sma) ** 2 for c in recent_closes) / period
    stddev = math.sqrt(variance)

    upper = sma + num_std * stddev
    lower = sma - num_std * stddev

    if upper == lower:
        return 0.5
    return (candles[-1].close - lower) / (upper - lower)


def price_roc(candles: List[KlineCandle], period: int = 10) -> float:
    """Price rate of change over N periods. Returns 0.0 if insufficient."""
    if len(candles) < period + 1:
        return 0.0

    past_close = candles[-(period + 1)].close
    current_close = candles[-1].close
    if past_close == 0:
        return 0.0
    return (current_close - past_close) / past_close
