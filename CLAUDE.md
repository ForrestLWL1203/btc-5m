# CLAUDE.md - Current Runtime State

## Repo State (2026-04-24)

This repository has one active runtime strategy: `paired_window`.

The codebase is live-capable and currently centered on a simple window-open BTC
momentum signal with strict price-band execution. An optional strong-signal cap
path exists in code, but it is not enabled in the active YAML.

## Core Runtime Components

- `run.py` — dry-run/live runner
- `polybot/strategies/paired_window.py` — BTC direction signal, direction lock,
  and optional strong-signal cap selection
- `polybot/trading/monitor.py` — window lifecycle, target-leg best-ask gating,
  risk management, and retry handling
- `polybot/trading/trading.py` — FAK order creation / posting / fill handling
- `polybot/core/state.py` — shared monitor state and daily risk counters
- `polybot/config_loader.py` — YAML loading and auto `min_entry_price`
- `polybot/market/binance.py` — BTC WS feed + REST klines fallback
- `analysis/analyze_paired_strategy.py` — primary backtest tool
- `tools/collect_data.py` — BTC + Polymarket collector
- `tools/probe_post_order_latency.py` — `/order` latency probe helper

## Current Runtime Behavior

1. Anchor BTC to the current 5-minute window open.
   - If the WS deque does not contain the open, seed it with Binance 1m kline
     REST data.
2. Only consider entries while remaining time is in `[240s, 120s]`.
3. Require BTC move from the open to satisfy:
   - `abs(move_pct) >= theta_pct`
   - same direction existed `persistence_sec` ago
   - current move >= `min_move_ratio * past_move`
4. Lock the first valid direction for the window.
5. Continue checking only that target side until its fresh `best_ask` is inside
   `[min_entry_price, target_max_entry_price]`.
6. Submit a FAK BUY using `target_best_ask + 1 tick` as the price hint.
7. Hold to `window.end_epoch`.
8. Let resolution / auto-redeem determine the final result.

## Active Config

Current active YAML:
`paired_window_cap61_5r_live.yaml`

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

`min_entry_price` is auto-calculated as:

```text
round(max_entry_price * 0.88, 2)
```

So the active runtime band is `[0.57, 0.65]`.

## Optional Strong-Signal Cap

Code supports these optional fields:

```yaml
strategy:
  strong_signal_threshold: 1.5
  strong_signal_max_entry_price: 0.67
```

Runtime behavior:

- compute `signal_strength = abs(move_pct) / theta_pct`
- if `signal_strength >= strong_signal_threshold`
- temporarily raise `state.target_max_entry_price`

If omitted, runtime stays on the fixed `max_entry_price` path.

## Signal / Price Semantics

- `signal_price`: reference price passed through the signal path
- `target_best_ask`: real executable ask on the target leg
- `price_hint`: `target_best_ask + 1 tick`, sent to Polymarket

Important:

- signal formation still keys off the UP-leg reference stream
- execution gating always uses the target leg's live `best_ask`
- never use theoretical `1 - up_price` for final order permission

## Risk Management

- UTC+8 daily reset
- 5 consecutive losses -> pause 2 windows
- after 30+ trades, if win rate < 50% -> pause 5 windows

## Execution Details

### FAK behavior

- Runtime uses FAK, not FOK
- `MATCHED` + `success=true` is treated as filled even if some size fields are
  omitted
- Retry refreshes a fresh target-leg best ask and aborts if stale or outside
  the band

### Quote freshness

- WS best ask freshness is tracked independently from trade prints
- stale best ask data should not be revived by newer non-book events

### Latency logging

Recent runtime logging includes:

- `BUY_SIGNAL.best_ask_age_ms`
- `BUY_PREP.best_ask_age_ms`
- `FAK_FILLED.create_market_order_ms`
- `FAK_FILLED.post_order_ms`
- `FAK_FILLED.attempt_ms`
- `FAK_FILLED.total_ms`
- same timing fields on `FAK_ATTEMPT_FAILED`

### Final-round shutdown

The final planned round no longer prefetches the next window. This avoids
extra market-discovery requests during shutdown.

## Backtest Reference

Reference dataset:
`data/collect_btc-updown-5m_1776874474.jsonl`

Current main shape:

- `theta=0.03`
- `persistence=10`
- entry band `[60s, 180s]` into window
- price band `[0.57, 0.65]`

This 96-window / 8-hour dataset remains the main local calibration set.

## Commands

Dry-run:

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml --dry --rounds 12
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

Collect 8-hour data:

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

Current status: `96 passed`.

## Working Guidance

- Treat `paired_window` as the only active runtime strategy
- Keep strategy, monitor, config loader, analysis script, and tests aligned
- Validate on the 96-window dataset before live parameter changes
- Do not change exit timing away from `window.end_epoch`
- Do not reintroduce TP/SL/re-entry unless explicitly requested and backtested
- If enabling strong-signal cap, update:
  - YAML
  - strategy docs
  - analysis assumptions
  - tests
