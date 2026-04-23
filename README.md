# Polybot - Current Paired Window Strategy

This repo currently runs one active strategy: `paired_window`.

It trades Polymarket BTC 5-minute UP/DOWN markets by:

1. anchoring BTC to the current 5-minute window open,
2. waiting for a persistent move away from that open,
3. locking the first valid direction for the window,
4. buying only when the target leg's fresh executable `best_ask` is inside the
   active entry band,
5. holding to `window.end_epoch`.

## Active Runtime Config

Current runtime YAML:
[paired_window_cap61_5r_live.yaml](/Users/forrestliao/workspace/paired_window_cap61_5r_live.yaml)

```yaml
market:
  asset: btc
  timeframe: 5m

strategy:
  type: paired_window
  theta_pct: 0.03
  persistence_sec: 10
  entry_start_remaining_sec: 240
  entry_end_remaining_sec: 120
  max_entry_price: 0.65
  min_move_ratio: 0.7

params:
  amount: 1.0
  max_entries_per_window: 1
```

`min_entry_price` is auto-calculated in
[polybot/config_loader.py](/Users/forrestliao/workspace/polybot/config_loader.py)
as:

```text
round(max_entry_price * 0.88, 2)
```

So the current effective entry band is:

```text
[0.57, 0.65]
```

## Core Logic

Strategy:
[polybot/strategies/paired_window.py](/Users/forrestliao/workspace/polybot/strategies/paired_window.py)

Execution:
[polybot/trading/monitor.py](/Users/forrestliao/workspace/polybot/trading/monitor.py)
[polybot/trading/trading.py](/Users/forrestliao/workspace/polybot/trading/trading.py)

### BTC signal

- Use BTC price at `window_start_epoch` as the baseline.
- If the WS deque does not cover the window open, seed it from Binance 1m
  klines REST.
- Only consider entries while remaining time is in `[240s, 120s]`.
- Require:
  - `abs(move_pct) >= theta_pct`
  - same-direction move already existed `persistence_sec` ago
  - current move >= `min_move_ratio * past_move`

### Direction lock

- The first valid direction in a window is locked.
- The bot can keep waiting for price to enter the band.
- It will not flip to the opposite side inside the same window.

### Entry gating

- Signal reference remains the UP-leg price stream.
- Final execution permission always uses the target leg's fresh live
  `best_ask`.
- UP trades use `up_best_ask`.
- DOWN trades use `down_best_ask`.
- Final gating never uses theoretical `1 - up_price`.

### Buy execution

- Order type: FAK.
- BUY price hint: `target_best_ask + 1 tick`.
- If the first FAK attempt fails, retry only after refreshing the latest WS
  target-leg `best_ask`.
- Retry is aborted if that refreshed ask is stale or outside the active band.

### Exit

- No TP / SL / re-entry.
- Hold until exact `window.end_epoch`.
- Let market resolution / auto-redeem determine the final result.

## Optional Strong-Signal Cap

The strategy now supports an optional strong-signal cap, but the active YAML
does not enable it.

Supported optional fields:

```yaml
strategy:
  strong_signal_threshold: 1.5
  strong_signal_max_entry_price: 0.67
```

Runtime behavior:

- compute `signal_strength = abs(move_pct) / theta_pct`
- if `signal_strength >= strong_signal_threshold`
- then temporarily raise `state.target_max_entry_price` to
  `strong_signal_max_entry_price`

If these fields are omitted, the strategy stays on the fixed `max_entry_price`
logic.

## Risk Management

Shared runtime state:
[polybot/core/state.py](/Users/forrestliao/workspace/polybot/core/state.py)

- Daily reset uses UTC+8.
- 5 consecutive losses -> pause 2 windows.
- After 30+ trades, if win rate < 50% -> pause 5 windows.

## Execution Notes

- WS best-ask freshness is tracked separately from trade updates.
- `BUY_SIGNAL` and `BUY_PREP` now log `best_ask_age_ms`.
- FAK execution logs now include:
  - `create_market_order_ms`
  - `post_order_ms`
  - `attempt_ms`
  - `total_ms`
- Final planned round does not prefetch the next window anymore, which avoids
  shutdown hangs caused by an extra market-discovery call.

## Current Backtest Snapshot

Primary analysis tool:
[analysis/analyze_paired_strategy.py](/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py)

Reference dataset:
`data/collect_btc-updown-5m_1776874474.jsonl`

For the current active config shape:

- `theta=0.03`
- `persistence=10`
- entry window `[60s, 180s]` into the 5-minute window
- price band `[0.57, 0.65]`

On the 8-hour / 96-window dataset, this remains the main local reference set.

## Key Files

- [run.py](/Users/forrestliao/workspace/run.py)
- [paired_window_cap61_5r_live.yaml](/Users/forrestliao/workspace/paired_window_cap61_5r_live.yaml)
- [polybot/strategies/paired_window.py](/Users/forrestliao/workspace/polybot/strategies/paired_window.py)
- [polybot/trading/monitor.py](/Users/forrestliao/workspace/polybot/trading/monitor.py)
- [polybot/trading/trading.py](/Users/forrestliao/workspace/polybot/trading/trading.py)
- [polybot/config_loader.py](/Users/forrestliao/workspace/polybot/config_loader.py)
- [polybot/core/state.py](/Users/forrestliao/workspace/polybot/core/state.py)
- [analysis/analyze_paired_strategy.py](/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py)
- [tools/collect_data.py](/Users/forrestliao/workspace/tools/collect_data.py)
- [tools/probe_post_order_latency.py](/Users/forrestliao/workspace/tools/probe_post_order_latency.py)

## Commands

Dry-run:

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml --dry
```

Live:

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml
```

Backtest:

```bash
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TS>.jsonl \
  --theta 0.03 --persistence 10 --lo 120 --hi 240 \
  --max-entry-price 0.65 --min-entry-price 0.57 --delays 0,1,2
```

Collect data:

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/collect_data.py \
  --market btc-updown-5m --windows 96 --no-snap --slim --poly-min-interval-ms 100
```

Probe `/order` latency:

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/probe_post_order_latency.py \
  --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

Run tests:

```bash
pytest -q
```

Current local status: `96 passed`.

## Notes For Future Changes

- Keep strategy, monitor, config loader, analysis script, and tests aligned.
- Validate parameter changes on the 96-window dataset before live testing.
- If entry logic changes, update:
  - strategy
  - monitor
  - config loader
  - analysis script
  - tests
- Do not reintroduce TP/SL/re-entry unless explicitly requested and backtested.
