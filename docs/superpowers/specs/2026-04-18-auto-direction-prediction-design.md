# Auto Direction Prediction — Design Spec

## Goal

Automatically predict Up/Down direction for Polymarket BTC/ETH trading windows.
Maximize win rate. Replace manual `--side up/down` with algorithmic prediction.

## Approach: Momentum Signal on Polymarket Token Prices (Phase 1)

Use Up/Down token prices already available via existing WS subscription.
Cross-window trend analysis with weighted voting.

## Architecture

### New Files

```
polybot/predict/
├── __init__.py
├── history.py       # WindowHistory ring buffer + Gamma API backfill
└── momentum.py      # MomentumPredictor (V1)
```

### Modified Files

| File | Change |
|------|--------|
| `monitor.py` | Call predictor at window start → set `trade_config.side`; call `history.record()` at window end |
| `config_loader.py` | Add `direction` config block, register predictor |
| `trade_config.py` | No change needed (`side` already mutable) |

### Data Flow

```
Startup → Gamma API backfill WindowHistory (concurrent, ~1s for 5m)
         ↓
New window → MomentumPredictor.predict(history, window) → "up"/"down"/None
         ↓
trade_config.side = result → existing buy/TP/SL logic unchanged
         ↓
Window end → history.record(open, close, volume, resolved_side)
         ↓
Next window → predictor.predict() called again
```

## Module 1: WindowHistory (`predict/history.py`)

### Data Structure

```python
@dataclass
class WindowRecord:
    window_start: int          # epoch
    up_price_open: float
    up_price_close: float
    down_price_open: float
    down_price_close: float
    up_volume: float
    down_volume: float
    resolved_side: str         # "up"/"down"/None
```

### Ring Buffer

- Capacity per timeframe:

| Timeframe | Capacity | Coverage |
|-----------|----------|----------|
| 5m | 100 | 8.3h |
| 15m | 30 | 7.5h |
| 4h | 6 | 24h |

- `HISTORY_CAPACITY` dict maps timeframe → capacity
- When full, oldest record overwritten by newest
- In-memory only. Reset on restart.

### Backfill on Startup

- Compute N slugs (epoch decrementing by `slug_step`)
- Query Gamma API `GET /markets?slug=<slug>` per window
- Concurrent execution (20-way) via `asyncio.gather` or ThreadPoolExecutor
- Target: <1s for all timeframes
- Extract from response: token prices (clobTokenIds), volume, resolved outcome
- Skip markets with missing/incomplete data

### Runtime Recording

- Open prices: captured from WS cache at window start
- Close prices: captured at window end (sell time)
- Volume: accumulated from WS `last_trade_price` events during window
- Resolved side: from Gamma API after resolution, or deferred

## Module 2: DirectionPredictor (`predict/momentum.py`)

### Interface

```python
class DirectionPredictor(ABC):
    def predict(self, history: WindowHistory, current_window: MarketWindow) -> Optional[str]:
        """Return 'up', 'down', or None (skip window)"""
        ...
```

### V1: MomentumPredictor

Three weighted signals:

| Signal | Weight | Logic |
|--------|--------|-------|
| Price momentum | 50% | Last 3 windows Up token close price trend → rising = up, falling = down |
| Up/Down price offset | 30% | Up token price deviation from 0.50 → >0.50 = up, <0.50 = down |
| Streak reversal | 20% | 3+ consecutive same-direction results → bet opposite direction (mean reversion) |

### Voting

- Weighted score > 0 → "up"
- Weighted score < 0 → "down"
- Score = 0 and history < 5 windows → None (skip)

### Timeframe Scaling

Parameters adjust by `slug_step`:

```python
class MomentumPredictor(DirectionPredictor):
    def __init__(self, series: MarketSeries):
        self.lookback = 3
        self.streak_threshold = 3 if series.slug_step <= 900 else 2
        self.min_history = 5
```

Works for all markets: btc-updown-5m, btc-updown-15m, btc-updown-4h, eth-updown-*.

## Module 3: Integration

### monitor.py Changes

At `monitor_window()` entry:
1. If predictor configured: call `predictor.predict(history, window)`
2. Set `trade_config.side = result`
3. If result is None and no `fallback_side` → skip window

At window end (sell/expire):
1. Call `history.record()` with window data

### YAML Config

```yaml
market:
  asset: btc
  timeframe: 5m

direction:
  type: momentum          # "momentum" | "fixed"
  fallback_side: up       # used when history insufficient or signal unclear

params:
  amount: 5.0
  tp_pct: 0.50
  sl_pct: 0.30
```

- `type: fixed` + `fallback_side` → behaves like current manual mode
- `type: momentum` → auto prediction

### Skip Window Logic

- `predict()` returns None + `fallback_side` configured → use fallback
- `predict()` returns None + no fallback → skip window, wait for next
- Log all prediction decisions for post-analysis

## Evolution Path (Phase 2+)

- **Phase 2**: Add Binance BTC K-line trend as external confirmation signal
- **Phase 3**: ML model (logistic regression) trained on accumulated history
- **Phase 4**: Adaptive weights — dynamically adjust signal weights based on recent accuracy

## Performance

- Backfill: 20-way concurrent Gamma API calls, <1s for 5m (100 windows)
- Prediction: pure in-memory computation, <1ms
- No additional WS connections needed
- No additional API calls during trading loop
