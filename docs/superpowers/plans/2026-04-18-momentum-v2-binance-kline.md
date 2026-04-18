# MomentumPredictor V2 — Binance K-line Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MomentumPredictor V1 (Polymarket token prices) with V2 (Binance BTC/ETH K-line data + technical indicators).

**Architecture:** New `kline.py` fetches OHLCV data from Binance API. New `indicators.py` computes EMA/RSI/trend/volume. MomentumPredictor V2 accepts `list[KlineCandle]` instead of `WindowHistory`. Monitor fetches K-lines before each window start.

**Tech Stack:** Python 3.11, `requests` (Binance REST), `pytest` (tests)

---

## File Structure

### New Files
- `polybot/predict/kline.py` — `KlineCandle` dataclass + `BinanceKlineFetcher`
- `polybot/predict/indicators.py` — Pure functions: `ema()`, `rsi()`, `trend_direction()`, `volume_trend()`
- `tests/test_kline.py` — Tests for K-line fetcher
- `tests/test_indicators.py` — Tests for indicators

### Modified Files
- `polybot/predict/momentum.py` — Rewrite MomentumPredictor V2 (new `predict(candles)` signature)
- `polybot/predict/__init__.py` — Update exports
- `polybot/trading/monitor.py` — Fetch K-lines, pass to predictor
- `run.py` — Remove Gamma backfill, simplify history setup
- `tests/test_momentum.py` — Update for V2
- `tests/test_monitor.py` — Update DirectionPrediction test

---

### Task 1: KlineCandle dataclass + BinanceKlineFetcher

**Files:**
- Create: `polybot/predict/kline.py`
- Create: `tests/test_kline.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_kline.py`:

```python
"""Tests for polybot.predict.kline — Binance K-line fetcher."""

import pytest
from unittest.mock import patch, MagicMock
from polybot.predict.kline import KlineCandle, BinanceKlineFetcher
from polybot.market.series import MarketSeries


class TestKlineCandle:
    def test_create_candle(self):
        c = KlineCandle(open_time=1000, open=100.0, high=105.0, low=99.0, close=103.0, volume=50.0)
        assert c.close == 103.0
        assert c.volume == 50.0


class TestBinanceKlineFetcher:
    def test_symbol_btc(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.symbol == "BTCUSDT"

    def test_symbol_eth(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("eth-updown-5m"))
        assert f.symbol == "ETHUSDT"

    def test_interval_5m(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.interval == "1m"
        assert f.limit == 60

    def test_interval_15m(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-15m"))
        assert f.interval == "5m"
        assert f.limit == 48

    def test_interval_4h(self):
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-4h"))
        assert f.interval == "1h"
        assert f.limit == 24

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_parses_klines(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            [1000, "100.0", "105.0", "99.0", "103.0", "50.0", 1060000, "2600", 0, "10", "5000", "0"],
            [2000, "103.0", "108.0", "102.0", "107.0", "60.0", 2060000, "3600", 0, "12", "6000", "0"],
        ]
        mock_get.return_value = mock_resp

        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        candles = f.fetch()

        assert len(candles) == 2
        assert candles[0].open == 100.0
        assert candles[0].close == 103.0
        assert candles[1].volume == 60.0

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_network_error_returns_empty(self, mock_get):
        mock_get.side_effect = Exception("Network error")
        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        candles = f.fetch()
        assert candles == []

    @patch("polybot.predict.kline.requests.get")
    def test_fetch_empty_response(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        f = BinanceKlineFetcher(MarketSeries.from_known("btc-updown-5m"))
        assert f.fetch() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_kline.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.predict.kline'`

- [ ] **Step 3: Implement BinanceKlineFetcher**

Create `polybot/predict/kline.py`:

```python
"""Binance K-line data fetcher for BTC/ETH price history."""

import logging
from dataclasses import dataclass
from typing import List

import requests

from polybot.market.series import MarketSeries

log = logging.getLogger(__name__)

_BINANCE_API = "https://api.binance.com/api/v3/klines"

# (interval, limit) per window timeframe
_TIMEFRAME_MAP = {
    "5m": ("1m", 60),
    "15m": ("5m", 48),
    "4h": ("1h", 24),
}


@dataclass
class KlineCandle:
    """Single OHLCV candle from Binance."""

    open_time: int   # epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float


class BinanceKlineFetcher:
    """Fetches K-line data from Binance for a given market series."""

    def __init__(self, series: MarketSeries):
        self.symbol = "BTCUSDT" if series.asset == "btc" else "ETHUSDT"
        interval, limit = _TIMEFRAME_MAP.get(series.timeframe, ("1m", 60))
        self.interval = interval
        self.limit = limit

    def fetch(self) -> List[KlineCandle]:
        """Fetch K-line candles from Binance. Returns empty list on failure."""
        try:
            resp = requests.get(
                _BINANCE_API,
                params={
                    "symbol": self.symbol,
                    "interval": self.interval,
                    "limit": self.limit,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []
            return [
                KlineCandle(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                for row in data
            ]
        except Exception as e:
            log.warning("Binance K-line fetch failed: %s", e)
            return []
```

- [ ] **Step 4: Run tests**

Run: `python3.11 -m pytest tests/test_kline.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/predict/kline.py tests/test_kline.py
git commit -m "feat: add KlineCandle dataclass and BinanceKlineFetcher"
```

---

### Task 2: Technical indicators

**Files:**
- Create: `polybot/predict/indicators.py`
- Create: `tests/test_indicators.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_indicators.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_indicators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.predict.indicators'`

- [ ] **Step 3: Implement indicators**

Create `polybot/predict/indicators.py`:

```python
"""Technical indicators computed from K-line candle data.

All functions are pure: input list[KlineCandle], output float.
Return neutral values when data is insufficient.
"""

from typing import List

from .kline import KlineCandle


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
```

- [ ] **Step 4: Run tests**

Run: `python3.11 -m pytest tests/test_indicators.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/predict/indicators.py tests/test_indicators.py
git commit -m "feat: add technical indicators (EMA, RSI, trend, volume)"
```

---

### Task 3: MomentumPredictor V2 rewrite

**Files:**
- Modify: `polybot/predict/momentum.py`
- Modify: `tests/test_momentum.py`

- [ ] **Step 1: Rewrite tests for V2**

Replace entire `tests/test_momentum.py`:

```python
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
        """Consistently falling prices → predict 'down'."""
        p = MomentumPredictor(_btc_5m())
        candles = [_candle(140 - i * 2, offset=i) for i in range(20)]
        assert p.predict(candles) == "down"

    def test_predict_none_on_insufficient_data(self):
        p = MomentumPredictor(_btc_5m())
        candles = [_candle(100, offset=i) for i in range(3)]
        assert p.predict(candles) is None

    def test_predict_none_on_empty(self):
        p = MomentumPredictor(_btc_5m())
        assert p.predict([]) is None

    def test_predict_uses_fallback_side(self):
        """When candles insufficient but fallback_side set, return fallback."""
        p = MomentumPredictor(_btc_5m(), fallback_side="down")
        assert p.predict([]) == "down"

    def test_predict_fallback_none_when_not_set(self):
        """When candles insufficient and no fallback, return None."""
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_momentum.py -v`
Expected: FAIL — V1 signature mismatch

- [ ] **Step 3: Rewrite MomentumPredictor V2**

Replace entire `polybot/predict/momentum.py`:

```python
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

    def __init__(self, series: MarketSeries, fallback_side: Optional[str] = None):
        self.fallback_side = fallback_side

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
```

- [ ] **Step 4: Run tests**

Run: `python3.11 -m pytest tests/test_momentum.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/predict/momentum.py tests/test_momentum.py
git commit -m "feat: rewrite MomentumPredictor V2 with Binance K-line signals"
```

---

### Task 4: Update config_loader for V2

**Files:**
- Modify: `polybot/config_loader.py`
- Modify: `tests/test_config_loader.py`

- [ ] **Step 1: Update build_direction_config to pass fallback_side**

In `polybot/config_loader.py`, the `build_direction_config` function needs to pass `fallback_side` to the predictor constructor. Current code:

```python
    return {
        "predictor": cls(series),
        "fallback_side": fallback,
    }
```

Change to:

```python
    return {
        "predictor": cls(series, fallback_side=fallback),
        "fallback_side": fallback,
    }
```

- [ ] **Step 2: Update tests**

In `tests/test_config_loader.py`, the `test_momentum_type_creates_predictor` test should still pass — no change needed since it only checks `isinstance`. But verify:

Run: `python3.11 -m pytest tests/test_config_loader.py::TestBuildDirectionConfig -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add polybot/config_loader.py tests/test_config_loader.py
git commit -m "feat: pass fallback_side to MomentumPredictor constructor"
```

---

### Task 5: Integrate K-line fetching into monitor

**Files:**
- Modify: `polybot/trading/monitor.py`
- Modify: `tests/test_monitor.py`

- [ ] **Step 1: Update monitor.py — change predictor integration**

In `polybot/trading/monitor.py`, replace the direction prediction block. Find the block starting with `# Direction prediction — runs once per window` and replace it:

Old:
```python
    # Direction prediction — runs once per window
    if predictor is not None and history is not None:
        direction = predictor.predict(history)
        if direction is not None:
            trade_config.side = direction
            log_event(log, logging.INFO, SIGNAL, {
                "action": "DIRECTION_PREDICTED",
                "side": direction.upper(),
                "window": window.short_label,
                "history_len": len(history),
            })
        else:
            log_event(log, logging.WARNING, SIGNAL, {
                "action": "DIRECTION_UNCLEAR",
                "window": window.short_label,
                "history_len": len(history),
            })
```

New:
```python
    # Direction prediction — runs once per window
    if predictor is not None:
        from polybot.predict.kline import BinanceKlineFetcher
        candles = []
        if series is not None:
            fetcher = BinanceKlineFetcher(series)
            candles = fetcher.fetch()
        direction = predictor.predict(candles)
        if direction is not None:
            trade_config.side = direction
            log_event(log, logging.INFO, SIGNAL, {
                "action": "DIRECTION_PREDICTED",
                "side": direction.upper(),
                "window": window.short_label,
                "candles": len(candles),
            })
        else:
            log_event(log, logging.WARNING, SIGNAL, {
                "action": "DIRECTION_UNCLEAR",
                "window": window.short_label,
                "candles": len(candles),
            })
```

Also remove the `history` parameter from `monitor_window` signature since V2 no longer needs it:

Change signature from:
```python
    predictor: Optional[DirectionPredictor] = None,
    history: Optional[WindowHistory] = None,
```
To:
```python
    predictor: Optional[DirectionPredictor] = None,
```

Remove `history` param from `_monitor_single_window` signature and its call site.

Remove the history recording block in `_monitor_single_window` (the `if history is not None and state.bought:` block).

Remove the `from polybot.predict.history import WindowHistory, WindowRecord` import.

- [ ] **Step 2: Update test_monitor.py DirectionPrediction test**

Replace `TestDirectionPrediction` class in `tests/test_monitor.py`:

```python
class TestDirectionPrediction:
    @pytest.mark.asyncio
    async def test_predictor_sets_side_at_window_start(self):
        """Predictor is called at window start and sets trade_config.side."""
        import datetime
        from polybot.trading.monitor import monitor_window

        utc = datetime.timezone.utc
        now = int(time.time())
        start = (now // 300) * 300
        window = MarketWindow(
            question="Bitcoin Up or Down - Test",
            up_token="up-tok",
            down_token="down-tok",
            start_time=datetime.datetime.fromtimestamp(start, tz=utc),
            end_time=datetime.datetime.fromtimestamp(start + 300, tz=utc),
            slug="btc-updown-5m-test",
        )

        predictor = MomentumPredictor(
            MarketSeries.from_known("btc-updown-5m"),
            fallback_side="down",
        )
        tc = TradeConfig(side="up")

        mock_ws = MagicMock()
        mock_ws.set_on_price = MagicMock()
        mock_ws.switch_tokens = AsyncMock()
        mock_ws.get_latest_price = MagicMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("polybot.trading.monitor.find_next_window", return_value=None), \
             patch("polybot.trading.monitor.get_midpoint_async", new_callable=AsyncMock, return_value=None), \
             patch("polybot.trading.monitor.BinanceKlineFetcher") as MockFetcher:
            MockFetcher.return_value.fetch.return_value = []  # empty → fallback
            await monitor_window(
                window, dry_run=True, preopened=True, existing_ws=mock_ws,
                trade_config=tc, predictor=predictor,
            )

        assert tc.side == "down"  # fallback_side used when no candles
```

- [ ] **Step 3: Run tests**

Run: `python3.11 -m pytest tests/test_monitor.py -v -x`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add polybot/trading/monitor.py tests/test_monitor.py
git commit -m "feat: integrate Binance K-line fetching into monitor loop"
```

---

### Task 6: Simplify run.py — remove Gamma backfill

**Files:**
- Modify: `run.py`

- [ ] **Step 1: Remove Gamma backfill and history setup**

In `run.py`, remove the direction prediction setup block that does Gamma API backfill. Find and replace:

Old:
```python
    # Direction prediction setup
    predictor = dir_cfg.get("predictor")
    fallback_side = dir_cfg.get("fallback_side")
    history = None
    if predictor is not None:
        import time as _time
        history = WindowHistory.for_timeframe(series.timeframe)
        log.info("Backfilling %s history (%d windows)...", series.timeframe, history._buf.maxlen)
        history.backfill(
            slug_prefix=series.slug_prefix,
            slug_step=series.slug_step,
            count=history._buf.maxlen,
            current_epoch=int(_time.time()),
        )
        log.info("History backfilled: %d windows", len(history))
    if fallback_side and predictor is None:
        trade_config.side = fallback_side
```

New:
```python
    # Direction prediction setup
    predictor = dir_cfg.get("predictor")
    fallback_side = dir_cfg.get("fallback_side")
    if fallback_side and predictor is None:
        trade_config.side = fallback_side
```

Remove the `from polybot.predict.history import WindowHistory` import.

Remove `history=history` from all `monitor_window()` calls (3 locations).

- [ ] **Step 2: Verify dry-run**

Run: `python3.11 run.py --config /tmp/test_direction.yaml --dry`
Expected: Should start without Gamma backfill, fetch Binance K-lines at window start.

- [ ] **Step 3: Commit**

```bash
git add run.py
git commit -m "feat: remove Gamma backfill, use Binance K-lines for prediction"
```

---

### Task 7: Update package exports + full test suite

**Files:**
- Modify: `polybot/predict/__init__.py`

- [ ] **Step 1: Update exports**

Replace `polybot/predict/__init__.py`:

```python
"""Auto direction prediction package."""

from .history import WindowHistory, WindowRecord
from .indicators import ema, rsi, trend_direction, volume_trend
from .kline import BinanceKlineFetcher, KlineCandle
from .momentum import DirectionPredictor, MomentumPredictor

__all__ = [
    "BinanceKlineFetcher",
    "DirectionPredictor",
    "KlineCandle",
    "MomentumPredictor",
    "WindowHistory",
    "WindowRecord",
    "ema",
    "rsi",
    "trend_direction",
    "volume_trend",
]
```

- [ ] **Step 2: Run full test suite**

Run: `python3.11 -m pytest tests/ -v --ignore=tests/test_monitor.py`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add polybot/predict/__init__.py
git commit -m "feat: update predict package exports for V2"
```
