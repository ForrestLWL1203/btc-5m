# CLAUDE.md - Current Runtime State

## Repo State (2026-04-26)

This repository has one active runtime strategy: `paired_window`.

The bot is live-capable. Current work centers on a BTC window-open momentum
signal with target-leg best-ask execution. Conservative fixed-cap live config
remains available, but the latest tested strategy path is the enhanced config
with early entry, max-only execution gating, and strength-tier caps.

Current local tests: `128 passed`.

## Core Runtime Components

- `run.py` — dry-run/live runner
- `polybot/strategies/paired_window.py` — BTC direction signal, direction lock,
  early-entry gate, strength-tier cap selection
- `polybot/trading/monitor.py` — window lifecycle, target-leg best-ask gating,
  first FAK hint, retry handling, risk management, state reset
- `polybot/trading/trading.py` — FAK order creation / posting / fill handling
- `polybot/core/state.py` — shared monitor state and daily risk counters
- `polybot/config_loader.py` — YAML loading
- `polybot/runtime_config.py` — preset/config startup assembly for CLI and future UI/API
- `polybot/runtime_inputs.py` — shared runtime parameter schema, validation, and config-path mapping
- `polybot/market/binance.py` — BTC WS feed + REST klines fallback
- `analysis/analyze_paired_strategy.py` — primary backtest tool
- `tools/collect_data.py` — BTC + Polymarket collector
- `tools/probe_post_order_latency.py` — `/order` latency probe helper

## Configs

### Conservative Live

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

### Latest Enhanced Test/Live

`paired_window_early_entry_dry.yaml`

The filename contains `dry`, but live mode is controlled only by the `--dry`
flag. Running this config without `--dry` places real orders.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
  persistence_sec: 10
  entry_start_remaining_sec: 255
  early_entry_start_remaining_sec: 285
  early_entry_strength_threshold: 1.5
  early_entry_past_strength_threshold: 0.8
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

- base cap `0.68`
- base entry pricing uses WS order-book ask level `1` (`best ask`)
- strength `>= 2.0x` -> ask level `2`
- strength `>= 3.5x` -> ask level `4`
- strength `>= 2.0x` -> cap `0.72`
- strength `>= 3.5x` -> cap `0.75`
- strength `>= 2.0x` -> amount `1.5`
- normal full-cap guard: if a normal-confidence entry is priced at the active
  base cap, skip it when strength `< 1.05x` or remaining time `< 210s`
- strength `>= 1.5x` and past strength `>= 0.8x` -> entry can start at
  `remaining=285s`, i.e. 15s after window open
- runtime has no lower entry-price floor; low target asks are allowed

## Current Runtime Behavior

1. Anchor BTC to the current 5-minute window open.
   - If the WS deque does not contain the open, seed it with Binance 1m kline
     REST data.
2. Normal entries run while remaining time is in `[255s, 180s]`.
3. Optional early entry extends start to `285s` for earlier persistent signals
   at `>=1.5x` current strength and `>=0.8x` past strength.
4. Signal requirements:
   - `abs(move_pct) >= theta_pct`
   - same direction existed `persistence_sec` ago
   - current move >= `min_move_ratio * past_move`
5. Lock the first valid direction for the window.
6. Continue checking only that target side until its fresh ask from the
   active strength-selected book level is at or below `target_max_entry_price`.
7. Submit a FAK BUY.
8. Hold to `window.end_epoch`.
9. Let resolution / auto-redeem determine the final result.

## Signal / Price Semantics

- `signal_price`: reference price passed through the signal path
- `target_best_ask`: top-of-book ask on the target leg
- `target_entry_ask`: executable ask chosen from the configured ask-book level
- `price_hint`: limit-like price passed into SDK market order creation
- `FAK_FILLED.avg_price`: actual filled average price from Polymarket response

Important:

- signal formation keys off the UP-leg reference stream
- execution permission always uses the target leg's live ask from the
  active strength-selected book level
- never use theoretical `1 - up_price` for final order permission

## FAK Behavior

- Runtime uses FAK, not FOK.
- Entry permission requires `target_entry_ask <= target_max_entry_price`.
- There is no runtime `min_entry_price`; execution is max-only.
- Enhanced config applies a normal full-cap guard before `BUY_SIGNAL`.
- First hint: active cap directly.
- Retry refreshes target-leg ask from the same active book level.
- Retry aborts if refreshed ask is stale or above cap.
- Retry hint uses refreshed ask + small buffer, then clamps to cap.
- `MATCHED` + `success=true` is treated as filled even if size fields are
  omitted.
- 400 `no orders found to match with FAK order` can happen if the book moves
  during `/order`; the retry path should not chase above cap.

## Risk Management

- UTC+8 daily reset
- 5 consecutive losses -> pause 2 windows
- after 30+ trades, if win rate < 50% -> pause 5 windows
- enhanced config: consecutive realized losses `>= 3.0` -> pause 2 windows
- enhanced config: daily realized PnL `<= -5.0` -> pause 5 windows

## State / Window Safety

- Per-window reset clears target side, target price, max cap, signal confidence,
  latest midpoint, entry check cache, and sets `state.started=False`.
- Trading is re-enabled only after window start and strategy window init.
- This prevents reused WS callbacks from trading during pre-open.
- Next-window prefetch task is awaited before reading result.
- Final planned round skips next-window prefetch to avoid shutdown hangs.

## Logging

Recent runtime logging includes:

- `BUY_SIGNAL.best_ask_age_ms`
- `BUY_PREP.best_ask_age_ms`
- `BUY_SIGNAL.signal_strength`
- `BUY_SIGNAL.past_signal_strength`
- `BUY_SIGNAL.remaining_sec`
- `BUY_SIGNAL.amount`
- `BUY_SIGNAL.entry_ask_level`
- `BUY_SIGNAL.best_ask_level_1`
- `BUY_SIGNAL.target_entry_ask`
- local WS `book` + `price_change` maintain ask depth without REST polling
- `FAK_FILLED.create_market_order_ms`
- `FAK_FILLED.post_order_ms`
- `FAK_FILLED.attempt_ms`
- `FAK_FILLED.total_ms`
- same timing fields on `FAK_ATTEMPT_FAILED`

Current caveat:

- `BUY_FILLED.price` can show target best ask, while true fill is
  `FAK_FILLED.avg_price`.

## Recent Runtime Observations

- Fixed-cap dry 6 rounds: 4 entries, 3W/1L, 2 no-entry windows.
- Enhanced dry 5 rounds: 3 entries, 3W/0L, 2 no-entry windows.
- Enhanced live 3 monitored rounds: 2 fills, 2W/0L, 1 FAK 400 then retry abort
  because refreshed ask moved above cap.
- Enhanced live 24 rounds / 2 hours: 16 fills, 14W/2L, estimated PnL
  `+4.52 USDC`.
- Log-level replay of that same 2-hour session using updated caps
  `0.68/0.72/0.75` and max-only gating: estimated 21 fills, 19W/2L,
  estimated PnL `+6.52 USDC`. This adds 5 likely winning fills, but it is
  not a full orderbook replay.
- Enhanced live 108 rounds / 9 hours: 10 fills, 8W/2L, 2 retry-abort misses,
  total stake `10.5 USDC`, realized PnL about `+1.91 USDC`. The `>=2.0x`
  amount tier triggered live: 1 strong-signal `1.5x` fill won, and 1
  strong-signal attempt missed after retry refresh moved above cap.

Small sample only. Do not treat as backtest evidence.

## Backtest Reference

Reference dataset:
`data/collect_btc-updown-5m_1776874474.jsonl`

Current main fixed-cap shape:

- `theta=0.03`
- `persistence=10`
- entry band `[60s, 180s]` into window
- max-only cap `0.65`

This 96-window / 8-hour dataset remains the main local calibration set.

## Commands

Preset-based startup:

```bash
python3.11 run.py --preset enhanced --dry --rounds 6
python3.11 run.py --preset enhanced --amount 1.5 --max-entry-price 0.69 --rounds 24
python3.11 run.py --preset uncapped-depth-test --rounds 3
```

`run.py` now requires exactly one of `--preset` or `--config`.

`uncapped-depth-test` is a live experiment: cap still gates BUY_SIGNAL, but
the FAK price hint uses the fresh order-book depth level that can cover the
configured amount, even if that hint is above cap.

Dry-run fixed cap:

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml --dry --rounds 12
```

Dry-run enhanced:

```bash
python3.11 run.py --config paired_window_early_entry_dry.yaml --dry --rounds 6
```

Live fixed cap:

```bash
python3.11 run.py --config paired_window_cap61_5r_live.yaml
```

Live enhanced:

```bash
python3.11 run.py --config paired_window_early_entry_dry.yaml --rounds 3
```

Backtest:

```bash
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TS>.jsonl \
  --theta 0.03 --persistence 10 --lo 120 --hi 240 \
  --max-entry-price 0.68 --delays 0,1,2
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

Start an unattended VPS run:

```bash
bash tools/vps_start_run.sh --host 70.34.207.45 --preset enhanced --rounds 6
```

Fetch the latest VPS run logs:

```bash
bash tools/vps_fetch_run.sh --host 70.34.207.45 --run-id latest
```

Unified VPS control tool:

```bash
bash tools/vpsctl.sh bootstrap --host 70.34.207.45
bash tools/vpsctl.sh run --host 70.34.207.45 --preset enhanced --rounds 6
bash tools/vpsctl.sh fetch --host 70.34.207.45 --run-id latest
```

Use `tools/vpsctl.sh` when the VPS may change. It handles dynamic
`host/user` input plus local VPS/account profiles for bootstrap, update, run,
and fetch.

Profile support:

- VPS profile: `~/.polybot/vps/<name>.env`
- account profile: `~/.polybot/accounts/<name>.json`
- Use `vpsctl.sh --vps-profile ...` for remote `run`, `status`, `stop`, and
  `fetch`; do not use raw `ssh` for stop/status unless you also load the
  profile password.
- example:
  - `bash tools/vpsctl.sh bootstrap --vps-profile sweden --account-profile alice`
- account profile minimum:
  - `private_key`
  - `proxy_address`
- optional defaults:
  - `chain_id=137`
  - `signature_type=proxy`

Run tests:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

Current local status: `128 passed`.

## Working Guidance

- Treat `paired_window` as the only active runtime strategy.
- Keep strategy, monitor, config loader, analysis script, and tests aligned.
- Runtime execution is max-only. Do not reintroduce a lower price floor unless
  explicitly requested.
- Validate on the 96-window dataset before live parameter changes.
- Do not change exit timing away from `window.end_epoch`.
- Do not reintroduce TP/SL/re-entry unless explicitly requested and backtested.
- Do not describe enhanced caps/early entry as active in fixed-cap live YAML.
