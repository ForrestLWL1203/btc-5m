# MomentumPredictor V2 — Binance K-line Design Spec

## Goal

Redesign MomentumPredictor to use Binance BTC/ETH K-line data for direction prediction.
Replace Polymarket token price signals with real crypto market technical indicators.
Support all markets: BTC/ETH × 5m/15m/4h.

## Why V2

V1 used Polymarket Up/Down token prices. Problem: Gamma API removes closed 5m windows quickly,
making historical backfill impossible. Prediction had zero data at startup.
Binance K-line API is free, no auth, and provides rich OHLCV data instantly.

## Architecture

### New Files

```
polybot/predict/
├── kline.py          # BinanceKlineFetcher — fetch + parse K-line data
└── indicators.py     # Pure functions: EMA, RSI, trend, volume
```

### Modified Files

| File | Change |
|------|--------|
| `predict/momentum.py` | Rewrite MomentumPredictor to use K-line signals |
| `trading/monitor.py` | Fetch K-lines before window start, pass to predictor |
| `run.py` | Remove Gamma API backfill, add Binance K-line fetch |

### Removed Dependencies

- Gamma API backfill in `run.py` startup (no longer needed for direction)
- `WindowHistory` dependency in predictor (kept for runtime recording only)

## Module 1: BinanceKlineFetcher (`predict/kline.py`)

### API

```
GET https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={INTERVAL}&limit={LIMIT}
```

Free, no authentication. Rate limit: 1200 requests/minute.

### Symbol Mapping

| Market asset | Binance symbol |
|---|---|
| btc | BTCUSDT |
| eth | ETHUSDT |

### K-line Interval Mapping

| Window timeframe | Binance interval | limit | Coverage |
|---|---|---|---|
| 5m | 1m | 60 | 1 hour |
| 15m | 5m | 48 | 4 hours |
| 4h | 1h | 24 | 24 hours |

### Data Structure

```python
@dataclass
class KlineCandle:
    open_time: int   # epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float
```

### Fetcher Interface

```python
class BinanceKlineFetcher:
    def __init__(self, series: MarketSeries):
        self.symbol = "BTCUSDT" if series.asset == "btc" else "ETHUSDT"
        self.interval, self.limit = self._map_timeframe(series.timeframe)

    def fetch(self) -> list[KlineCandle]:
        """Fetch K-line data from Binance. Returns empty list on failure."""
        ...
```

### Error Handling

- Network error → log warning, return empty list
- Binance 429 (rate limit) → log warning, return empty list
- Malformed response → log warning, return empty list
- Empty response → return empty list

When empty list returned, predictor falls back to `fallback_side` or skips window.

## Module 2: Technical Indicators (`predict/indicators.py`)

Pure functions. Input: `list[KlineCandle]`. Output: float values.

| Indicator | Function | Logic |
|---|---|---|
| EMA | `ema(candles, period) -> float` | Exponential moving average of close prices |
| RSI | `rsi(candles, period=14) -> float` | Relative Strength Index (0-100) |
| Trend direction | `trend_direction(candles, n) -> float` | Fraction of last N candles where close > open (0.0-1.0) |
| Volume trend | `volume_trend(candles, n) -> float` | Ratio of recent N candles volume vs prior N candles volume |

All functions return 0.0 or neutral value when insufficient data.

## Module 3: MomentumPredictor V2 (`predict/momentum.py`)

### DirectionPredictor ABC Update

```python
class DirectionPredictor(ABC):
    @abstractmethod
    def predict(self, candles: list[KlineCandle]) -> Optional[str]:
        """Return 'up', 'down', or None (skip)."""
        ...
```

Signature changes from `predict(history: WindowHistory)` to `predict(candles: list[KlineCandle])`.

### MomentumPredictor V2 Signals

| Signal | Weight | Logic |
|---|---|---|
| Short-term trend | 40% | `trend_direction(candles, n)` > 0.50 → UP score positive |
| EMA crossover | 30% | `ema(candles, short)` > `ema(candles, long)` → UP score positive |
| RSI | 20% | RSI < 40 → UP (oversold bounce), RSI > 60 → DOWN (overbought pullback) |
| Volume confirmation | 10% | `volume_trend(candles, n)` > 1.0 + close rising → UP confirmation |

### Timeframe Scaling

```python
class MomentumPredictor(DirectionPredictor):
    def __init__(self, series: MarketSeries):
        if series.slug_step <= 300:      # 5m
            self.trend_n = 12            # last 12 candles (12min)
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 15
        elif series.slug_step <= 900:    # 15m
            self.trend_n = 8
            self.ema_short = 10
            self.ema_long = 30
            self.rsi_period = 14
            self.min_candles = 30
        else:                            # 4h
            self.trend_n = 6
            self.ema_short = 8
            self.ema_long = 20
            self.rsi_period = 14
            self.min_candles = 20
```

### Voting Logic

Same as V1: weighted score > 0 → "up", < 0 → "down", = 0 → None.

## Module 4: Integration

### monitor.py Changes

At `monitor_window()` entry, before direction prediction:

```python
# Fetch K-line data for prediction
candles = []
if predictor is not None:
    from polybot.predict.kline import BinanceKlineFetcher
    fetcher = BinanceKlineFetcher(series)
    candles = fetcher.fetch()

# Direction prediction
if predictor is not None:
    direction = predictor.predict(candles)
    ...
```

### run.py Changes

- Remove Gamma API backfill on startup
- Remove `WindowHistory` initialization for direction prediction
- Keep `WindowHistory` import but don't use it for predictor (future use)
- `history` param to `monitor_window` becomes optional/unused for now

### config_loader.py

No changes. `direction.type: momentum` and `DIRECTION_REGISTRY` stay the same.

### YAML Config (unchanged)

```yaml
direction:
  type: momentum
  fallback_side: up
```

## Backward Compatibility

- CLI `--side up` and no `direction` block → unchanged behavior
- `direction.type: fixed` → unchanged behavior
- `direction.type: momentum` → now uses Binance K-lines instead of Polymarket token prices
- `WindowHistory` class retained but not used by predictor (available for future enhancements)

## Performance

- Binance API call: ~200ms single request
- Indicator calculation: <1ms for 60 candles
- No additional WS connections needed
- Can run in parallel with WS pre-connect

## Evolution Path

- **Phase 3**: ML model trained on accumulated K-line + outcome data
- **Phase 4**: Adaptive weights based on prediction accuracy tracking
- **Phase 5**: Multi-source confirmation (Binance + Polymarket token prices)
