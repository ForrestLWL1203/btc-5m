# AGENTS.md - Implementation Guidance

## Project Status (2026-04-24)

**Polymarket BTC 5-Minute Binary Options Trading Bot**

Status: ✅ **LIVE CAPABLE**

- Single active runtime strategy: `paired_window`
- Reference dataset: 96-window / 8-hour capture
- Current local tests: `96 passed`
- Risk management integrated
- Optional strong-signal cap support exists in code, but is not enabled in the
  active YAML

## Core Runtime Components

**Strategy & Execution**

- `polybot/strategies/paired_window.py` — BTC window-open signal, persistence
  check, direction lock, optional strong-signal cap
- `polybot/trading/monitor.py` — window lifecycle, target-leg best-ask gating,
  buy path, retry path, risk pauses, shutdown handling
- `polybot/trading/trading.py` — FAK order execution via Polymarket CLOB
- `polybot/core/state.py` — `MonitorState` with per-window and daily risk state
- `polybot/config_loader.py` — YAML loading + auto `min_entry_price`
- `polybot/market/binance.py` — BTC WS feed + REST kline fallback

**Analysis & Tooling**

- `analysis/analyze_paired_strategy.py` — primary backtest tool
- `tools/collect_data.py` — BTC + Polymarket data collection
- `tools/probe_post_order_latency.py` — probe `/order` latency with intentionally
  unfillable orders

## Current Active Config

Active YAML:
`paired_window_cap61_5r_live.yaml`

```yaml
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

`min_entry_price` defaults to:

```text
round(max_entry_price * 0.88, 2)
```

So current live band is `[0.57, 0.65]`.

## Current Runtime Behavior

1. Anchor BTC at the current window open
2. In the entry band `[60s, 180s]` into the window:
   - require BTC move from open >= `theta_pct`
   - require same direction to have existed `persistence_sec` ago
   - require current move >= `min_move_ratio * past_move`
3. Lock the first valid direction for that window
4. Keep checking the chosen target leg until its fresh `best_ask` is inside the
   entry band
5. Send a FAK BUY with `target_best_ask + 1 tick`
6. Hold to `window.end_epoch`
7. Let resolution / auto-redeem settle the outcome

## Optional Strong-Signal Cap

Code supports:

```yaml
strategy:
  strong_signal_threshold: 1.5
  strong_signal_max_entry_price: 0.67
```

Behavior:

- compute `signal_strength = abs(move_pct) / theta_pct`
- if strength crosses the threshold
- temporarily raise `state.target_max_entry_price`

If the fields are absent, the strategy stays on the fixed-cap path.

## Risk Management

- UTC+8 daily reset
- 5 consecutive losses -> pause 2 windows
- after 30+ trades, if win rate < 50% -> pause 5 windows

## Important Implementation Details

### Execution gating

- Signal reference uses the UP-leg stream
- Final execution gating always uses the target leg's fresh `best_ask`
- Do not use theoretical `1 - up_price` for execution permission

### FAK orders

- Runtime uses FAK
- Retry refreshes target-leg best ask from WS
- Retry aborts if refreshed ask is stale or outside band
- `MATCHED` + `success=true` is treated as filled even if some fill fields are
  omitted

### Logging

Current runtime logs include:

- `BUY_SIGNAL.best_ask_age_ms`
- `BUY_PREP.best_ask_age_ms`
- `FAK_FILLED.create_market_order_ms`
- `FAK_FILLED.post_order_ms`
- `FAK_FILLED.attempt_ms`
- `FAK_FILLED.total_ms`

### Window chaining

Final planned round no longer prefetches the next window. This avoids shutdown
hangs caused by an unnecessary extra market-discovery call.

## What Not To Restore Without Backtest

- TP / SL / re-entry logic
- theoretical `1 - up_price` execution gating
- rolling momentum baseline in place of window-open baseline
- any strong-signal cap defaults in docs unless they are actually enabled in
  the active YAML

## Typical Workflow

**Collect data**

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/collect_data.py \
  --market btc-updown-5m --windows 96 --no-snap --slim --poly-min-interval-ms 100
```

**Backtest**

```bash
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TS>.jsonl \
  --theta 0.03 --persistence 10 --lo 120 --hi 240 \
  --max-entry-price 0.65 --min-entry-price 0.57 --delays 0,1,2
```

**Dry-run**

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml --dry --rounds 12
```

**Live**

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml
```

**Probe `/order` latency**

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/probe_post_order_latency.py \
  --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

**Run tests**

```bash
pytest -q
```

## Guidance For Future Agent Sessions

**DO**

- Treat `paired_window` as the only active runtime strategy
- Use `analysis/analyze_paired_strategy.py` for parameter work
- Keep docs aligned with the actual active YAML and code
- Validate parameter changes on the 96-window dataset before live testing
- Distinguish `signal_price`, `target_best_ask`, and `price_hint`

**DON'T**

- Assume removed doc text is still true without checking code
- Reintroduce TP/SL/re-entry without fresh backtest evidence
- Describe strong-signal cap as active unless it is really enabled in YAML
- Change exit timing away from `window.end_epoch`
