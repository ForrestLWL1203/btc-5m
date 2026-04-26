# AGENTS.md - Implementation Guidance

## Project Status (2026-04-26)

**Polymarket BTC 5-Minute Binary Options Trading Bot**

Status: ✅ **LIVE CAPABLE**

- Single active runtime strategy: `paired_window`
- Reference dataset: 96-window / 8-hour capture
- Current local tests: `128 passed`
- Risk management integrated
- Conservative live YAML remains available
- Latest enhanced YAML uses early entry + max-only strength-tier caps

## Core Runtime Components

**Strategy & Execution**

- `polybot/strategies/paired_window.py` — BTC window-open signal,
  persistence check, direction lock, optional early entry, strength-tier caps
- `polybot/trading/monitor.py` — window lifecycle, target-leg best-ask gating,
  buy path, FAK price hints, retry path, risk pauses, shutdown handling
- `polybot/trading/trading.py` — FAK order execution via Polymarket CLOB
- `polybot/core/state.py` — `MonitorState` with per-window and daily risk state
- `polybot/config_loader.py` — YAML loading
- `polybot/runtime_config.py` — preset/config startup assembly for CLI and future UI/API
- `polybot/runtime_inputs.py` — shared runtime parameter schema, validation, and config-path mapping
- `polybot/market/binance.py` — BTC WS feed + REST kline fallback

**Analysis & Tooling**

- `analysis/analyze_paired_strategy.py` — primary backtest tool
- `tools/collect_data.py` — BTC + Polymarket data collection
- `tools/probe_post_order_latency.py` — probe `/order` latency with
  intentionally unfillable orders

## Configs

### Conservative Live Config

Active live YAML:
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

Execution is max-only by default: `target_best_ask <= 0.65`.

### Latest Enhanced Test/Live Config

Enhanced YAML:
`paired_window_early_entry_dry.yaml`

Despite the filename, it becomes live when run without `--dry`.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
  persistence_sec: 10
  entry_start_remaining_sec: 240
  early_entry_start_remaining_sec: 270
  early_entry_strength_threshold: 2.0
  early_entry_past_strength_threshold: 1.0
  entry_end_remaining_sec: 180
  max_entry_price: 0.68
  min_move_ratio: 0.7
  strength_caps:
    - threshold: 2.0
      max_entry_price: 0.72
    - threshold: 3.5
      max_entry_price: 0.75

params:
  amount: 1.0
  entry_ask_level: 1
  ask_level_tiers:
    - threshold: 2.0
      level: 2
    - threshold: 3.5
      level: 4
  amount_tiers:
    - threshold: 2.0
      amount: 1.5
  normal_full_cap_guard:
    enabled: true
    min_signal_strength: 1.05
    min_remaining_sec: 210
  max_entries_per_window: 1

risk:
  consecutive_loss_amount: 3.0
  daily_loss_amount: 5.0
  consecutive_loss_pause_windows: 2
  daily_loss_pause_windows: 5
```

Enhanced behavior:

- base cap is `0.68`
- base entry pricing uses WS order-book ask level `1` (`best ask`)
- `signal_strength >= 2.0x` -> ask level `2`
- `signal_strength >= 3.5x` -> ask level `4`
- `signal_strength >= 2.0x` -> cap `0.72`
- `signal_strength >= 3.5x` -> cap `0.75`
- `signal_strength >= 2.0x` -> amount `1.5`
- normal full-cap guard: if a normal-confidence entry is priced at the active
  base cap, skip it when strength `< 1.05x` or remaining time `< 210s`
- `signal_strength >= 2.0x` and past strength `>= 1.0x` allows entry as early
  as `remaining=270s`, i.e. 30 seconds into a 5-minute window
- runtime has no lower entry-price floor; low target asks are allowed

## Runtime Behavior

1. Anchor BTC at the current window open.
2. In the normal entry band `[60s, 180s]` into the window:
   - require BTC move from open >= `theta_pct`
   - require same direction to have existed `persistence_sec` ago
   - require current move >= `min_move_ratio * past_move`
3. If early-entry fields are enabled, allow strong signals as early as 30s into
   the window.
4. Lock the first valid direction for that window.
5. Keep checking the chosen target leg until its fresh ask from the active
   strength-selected book level is at or below the active cap.
6. Send a FAK BUY.
7. Hold to `window.end_epoch`.
8. Let resolution / auto-redeem settle the outcome.

## Execution Gating

- Signal reference uses the UP-leg stream.
- Final execution gating always uses the target leg's fresh ask from the
  active strength-selected book level.
- Do not use theoretical `1 - up_price` for execution permission.
- `signal_price`, `target_best_ask`, `target_entry_ask`, and `price_hint` are distinct.

## FAK Orders

- Runtime uses FAK.
- Entry permission requires `target_entry_ask <= target_max_entry_price`.
- There is no runtime `min_entry_price`; execution is max-only.
- Enhanced config applies a normal full-cap guard before `BUY_SIGNAL`.
- First-attempt hint: active cap directly.
- Retry refreshes target-leg ask from the same active book level.
- Retry aborts if refreshed ask is stale or above cap.
- Retry hint uses refreshed ask + small buffer, then clamps to cap.
- `MATCHED` + `success=true` is treated as filled even if some fill fields are
  omitted.
- A 400 `no orders found to match with FAK order` can happen when the book moves
  before `/order`; the retry guard should prevent chasing above cap.

## Risk Management

- UTC+8 daily reset
- 5 consecutive losses -> pause 2 windows
- after 30+ trades, if win rate < 50% -> pause 5 windows
- enhanced config: consecutive realized losses `>= 3.0` -> pause 2 windows
- enhanced config: daily realized PnL `<= -5.0` -> pause 5 windows

## Logging

Current runtime logs include:

- `BUY_SIGNAL.best_ask_age_ms`
- `BUY_PREP.best_ask_age_ms`
- `BUY_SIGNAL.signal_strength`
- `BUY_SIGNAL.past_signal_strength`
- `BUY_SIGNAL.remaining_sec`
- `BUY_SIGNAL.amount`
- `BUY_SIGNAL.entry_ask_level`
- `BUY_SIGNAL.best_ask_level_1`
- `BUY_SIGNAL.target_entry_ask`
- WS `book` snapshots plus `price_change` deltas maintain local ask depth
- `FAK_FILLED.create_market_order_ms`
- `FAK_FILLED.post_order_ms`
- `FAK_FILLED.attempt_ms`
- `FAK_FILLED.total_ms`
- same timing fields on `FAK_ATTEMPT_FAILED`

Note: `BUY_FILLED.price` may reflect target best ask; real fill is
`FAK_FILLED.avg_price`.

## Window Chaining

- Per-window state resets `started=False` and clears target fields before WS
  token switch, preventing pre-open trades on reused WS.
- Prefetched next-window task is awaited before reading result.
- Final planned round does not prefetch next window. This avoids shutdown hangs
  caused by unnecessary market discovery.

## Recent Live/Dry Observations

- Fixed-cap dry 6 rounds: 4 entries, 3W/1L, 2 no-entry windows.
- Enhanced dry 5 rounds: 3 entries, 3W/0L, 2 no-entry windows.
- Enhanced live 3 monitored rounds: 2 fills, 2W/0L, 1 FAK 400 then retry abort
  because refreshed ask moved above cap.
- Enhanced live 24 rounds / 2 hours: 16 fills, 14W/2L, estimated PnL
  `+4.52 USDC`.
- Log-level replay of that 2-hour session with current caps `0.68/0.72/0.75`
  and max-only gating: estimated 21 fills, 19W/2L, estimated PnL
  `+6.52 USDC`. This is not a full orderbook replay.
- Enhanced live 108 rounds / 9 hours: 10 fills, 8W/2L, 2 retry-abort misses,
  total stake `10.5 USDC`, realized PnL about `+1.91 USDC`. The `>=2.0x`
  amount tier triggered live: 1 strong-signal `1.5x` fill won, and 1
  strong-signal attempt missed after retry refresh moved above cap.

These are small samples, not backtests.

## What Not To Restore Without Backtest

- TP / SL / re-entry logic
- theoretical `1 - up_price` execution gating
- rolling momentum baseline in place of window-open baseline
- strong-signal defaults in conservative live YAML unless intentionally enabled
  and documented

## Typical Workflow

**Preset-based startup**

```bash
python3.11 run.py --preset enhanced --dry --rounds 6
python3.11 run.py --preset enhanced --amount 1.5 --max-entry-price 0.69 --rounds 24
```

`run.py` now requires exactly one of `--preset` or `--config`.

**Collect data**

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/collect_data.py \
  --market btc-updown-5m --windows 96 --no-snap --slim --poly-min-interval-ms 100
```

**Backtest**

```bash
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TS>.jsonl \
  --theta 0.03 --persistence 10 --lo 120 --hi 240 \
  --max-entry-price 0.68 --delays 0,1,2
```

**Dry-run fixed cap**

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml --dry --rounds 12
```

**Dry-run enhanced**

```bash
python3.11 run.py --config paired_window_early_entry_dry.yaml --dry --rounds 6
```

**Live fixed cap**

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml
```

**Live enhanced**

```bash
python3.11 run.py --config paired_window_early_entry_dry.yaml --rounds 3
```

**Probe `/order` latency**

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/probe_post_order_latency.py \
  --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

**Start unattended VPS run**

```bash
bash tools/vps_start_run.sh --host 70.34.207.45 --preset enhanced --rounds 6
```

**Fetch latest VPS logs**

```bash
bash tools/vps_fetch_run.sh --host 70.34.207.45 --run-id latest
```

**Unified VPS control**

```bash
bash tools/vpsctl.sh bootstrap --host 70.34.207.45 --ask-pass
bash tools/vpsctl.sh run --host 70.34.207.45 --ask-pass --preset enhanced --rounds 6
bash tools/vpsctl.sh fetch --host 70.34.207.45 --ask-pass --run-id latest
```

Prefer `tools/vpsctl.sh` when the VPS host may change. It accepts dynamic
`host/user/password` and handles bootstrap, update, run, and log fetch.

Profiles:

- VPS profile file: `~/.polybot/vps/<name>.env`
- account profile file: `~/.polybot/accounts/<name>.json`
- bootstrap can use:
  - `bash tools/vpsctl.sh bootstrap --vps-profile sweden --account-profile alice`
- account profile minimum:
  - `private_key`
  - `proxy_address`
- optional defaults:
  - `chain_id=137`
  - `signature_type=proxy`

**Run tests**

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

## Guidance For Future Agent Sessions

**DO**

- Treat `paired_window` as the only active runtime strategy.
- Use `analysis/analyze_paired_strategy.py` for parameter work.
- Keep docs aligned with the actual YAML and code.
- Runtime execution is max-only. Do not reintroduce a lower price floor unless
  explicitly requested.
- Validate parameter changes on the 96-window dataset before live testing.
- Distinguish `signal_price`, `target_best_ask`, `price_hint`, and
  `FAK_FILLED.avg_price`.

**DON'T**

- Assume removed doc text is still true without checking code.
- Reintroduce TP/SL/re-entry without fresh backtest evidence.
- Describe enhanced caps/early entry as active in fixed-cap live YAML.
- Change exit timing away from `window.end_epoch`.
