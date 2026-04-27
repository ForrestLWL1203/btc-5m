# Polybot - Current Paired Window Strategy

This repo currently runs one active strategy: `paired_window`.

It trades Polymarket BTC 5-minute UP/DOWN markets by:

1. anchoring BTC to the current 5-minute window open,
2. waiting for a persistent move away from that open,
3. locking the first valid direction for the window,
4. buying only when target-leg WS book depth from level 2 onward has enough
   notional inside the active cap,
5. submitting a FAK BUY with a cap-aware price hint,
6. holding to `window.end_epoch`.

## Runtime Configs

Runtime startup assembly now lives in:
- [polybot/runtime_config.py](/Users/forrestliao/workspace/polybot/runtime_config.py)
- [polybot/runtime_inputs.py](/Users/forrestliao/workspace/polybot/runtime_inputs.py)

`runtime_inputs.py` is the shared registry for:
- frontend-safe parameter schema
- advanced engineering-only fields
- backend validation and normalization
- config-path mapping for preset/config overrides

### Conservative Live

Current conservative YAML:
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

Execution is max-only by default: enough level-2+ WS book depth must exist
inside `max_entry_price=0.65`.

### Enhanced Test/Live

Enhanced YAML:
[paired_window_early_entry_dry.yaml](/Users/forrestliao/workspace/paired_window_early_entry_dry.yaml)

The filename contains `dry`, but live/dry behavior is controlled by `--dry`.
Without `--dry`, this config places real orders.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
  persistence_sec: 10
  entry_start_remaining_sec: 255
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
  entry_ask_level: 6
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

- base cap: `0.68`
- entries start at `remaining=255s`, i.e. 45s into the 5-minute window
- no early-entry bypass; strong signals do not enter before 45s
- entry pricing uses cap-limited WS order-book depth from level 2 onward
- first BUY hint uses at least ask level 6 (`best ask +5`), then clamps to cap
- strength `>= 2.0x`: cap `0.72`
- strength `>= 3.5x`: cap `0.75`
- strength `>= 2.0x`: amount `1.5`
- normal full-cap guard: if a normal-confidence entry is priced at the active
  base cap, skip it when strength `< 1.05x` or remaining time `< 210s`
- runtime has no lower entry-price floor; low target asks are allowed

## Core Logic

Strategy:
[polybot/strategies/paired_window.py](/Users/forrestliao/workspace/polybot/strategies/paired_window.py)

Execution:
[polybot/trading/monitor.py](/Users/forrestliao/workspace/polybot/trading/monitor.py)
[polybot/trading/trading.py](/Users/forrestliao/workspace/polybot/trading/trading.py)

### BTC Signal

- Use BTC price at `window_start_epoch` as the baseline.
- If the WS deque does not cover the window open, seed it from Binance 1m
  klines REST.
- Entry window: remaining time in `[255s, 180s]`.
- Strong signals do not bypass this timing gate.
- Require:
  - `abs(move_pct) >= theta_pct`
  - same-direction move already existed `persistence_sec` ago
  - current move >= `min_move_ratio * past_move`

### Direction Lock

- The first valid direction in a window is locked.
- The bot can keep waiting for price to enter the band.
- It will not flip to the opposite side inside the same window.

### Entry Gating

- Signal reference remains the UP-leg price stream.
- Final execution permission uses the target leg's fresh WS order book depth,
  not the `best_bid_ask` top quote.
- UP trades use `up_best_ask`.
- DOWN trades use `down_best_ask`.
- Final gating never uses theoretical `1 - up_price`.
- Level 1 ask is diagnostic only. Fillability starts from level 2 because the
  top ask often disappears before the FAK reaches Polymarket.
- The bot sums `price * size` only for ask levels `price <= active_cap`. It
  sends a BUY only when that cap-limited notional covers the configured amount.

### Buy Execution

- Order type: FAK.
- Permission requires enough level-2+ WS book depth inside
  `target_max_entry_price`.
- There is no runtime `min_entry_price`; execution is max-only.
- Enhanced config applies a normal full-cap guard before `BUY_SIGNAL`.
- First price hint is at least configured `entry_ask_level`; enhanced uses
  level 6 (`best ask +5`), plus a small tick buffer, clamped to cap.
- FAK retry is capped at 3 attempts total.
- Retry refreshes target-leg WS book depth with the same level-1 skip.
- Retry aborts if refreshed cap-limited depth is stale or insufficient.
- Retry hint is recalculated from fresh depth, then clamped to cap.
- A 400 `no orders found to match with FAK order` can happen if the order book
  moves before `/order`; this is handled by refresh-and-abort logic.

### Exit

- No TP / SL / re-entry.
- Hold until exact `window.end_epoch`.
- Let market resolution / auto-redeem determine the final result.

## Risk Management

Shared runtime state:
[polybot/core/state.py](/Users/forrestliao/workspace/polybot/core/state.py)

- Daily reset uses UTC+8.
- 5 consecutive losses -> pause 2 windows.
- After 30+ trades, if win rate < 50% -> pause 5 windows.
- Enhanced config: consecutive realized losses `>= 3.0` -> pause 2 windows.
- Enhanced config: daily realized PnL `<= -5.0` -> pause 5 windows.

## Execution Notes

- WS best-ask freshness is tracked separately from trade updates.
- WS market-channel `book` snapshots and `price_change` deltas maintain local
  ask depth, so entry pricing uses deeper ask levels without REST `/book`.
- `BUY_SIGNAL` and `BUY_PREP` log `best_ask_age_ms`.
- `BUY_SIGNAL` / `BUY_PREP` also log `signal_strength`,
  `past_signal_strength`, `remaining_sec`, `amount`, `best_ask_level_1`,
  `target_entry_ask`, `price_hint`, `depth_levels_used`, `depth_notional`,
  `depth_skipped_levels`, and a short `book_ask_preview`.
- FAK execution logs include:
  - `create_market_order_ms`
  - `post_order_ms`
  - `attempt_ms`
  - `total_ms`
- Same timing fields are logged on `FAK_ATTEMPT_FAILED`.
- `BUY_FILLED.price` may be target best ask; true fill is
  `FAK_FILLED.avg_price`.
- Final planned round does not prefetch the next window.
- Per-window state is reset before WS token switch, preventing pre-open trades
  on reused WS callbacks.

## Recent Runtime Snapshot

Small live/dry samples:

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
  amount tier triggered live: 1 strong-signal fill at `1.5x` size won, and
  1 strong-signal attempt missed after retry refresh moved above cap.

These are smoke-test observations, not backtest evidence.

## Backtest Snapshot

Primary analysis tool:
[analysis/analyze_paired_strategy.py](/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py)

Reference dataset:
`data/collect_btc-updown-5m_1776874474.jsonl`

Fixed-cap reference shape:

- `theta=0.03`
- `persistence=10`
- entry window `[60s, 180s]` into the 5-minute window
- max-only cap `0.65`

On the 8-hour / 96-window dataset, this remains the main local reference set.

## Key Files

- [run.py](/Users/forrestliao/workspace/run.py)
- [paired_window_cap61_5r_live.yaml](/Users/forrestliao/workspace/paired_window_cap61_5r_live.yaml)
- [paired_window_early_entry_dry.yaml](/Users/forrestliao/workspace/paired_window_early_entry_dry.yaml)
- [polybot/strategies/paired_window.py](/Users/forrestliao/workspace/polybot/strategies/paired_window.py)
- [polybot/trading/monitor.py](/Users/forrestliao/workspace/polybot/trading/monitor.py)
- [polybot/trading/trading.py](/Users/forrestliao/workspace/polybot/trading/trading.py)
- [polybot/config_loader.py](/Users/forrestliao/workspace/polybot/config_loader.py)
- [polybot/runtime_config.py](/Users/forrestliao/workspace/polybot/runtime_config.py)
- [polybot/runtime_inputs.py](/Users/forrestliao/workspace/polybot/runtime_inputs.py)
- [polybot/core/state.py](/Users/forrestliao/workspace/polybot/core/state.py)
- [analysis/analyze_paired_strategy.py](/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py)
- [tools/collect_data.py](/Users/forrestliao/workspace/tools/collect_data.py)
- [tools/probe_post_order_latency.py](/Users/forrestliao/workspace/tools/probe_post_order_latency.py)

## Commands

Preset-based startup:

```bash
python3.11 run.py --preset enhanced --dry --rounds 6
python3.11 run.py --preset enhanced --amount 1.5 --max-entry-price 0.69 --rounds 24
```

`run.py` now requires exactly one of `--preset` or `--config`.

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

`tools/vpsctl.sh` is the preferred path for dynamic VPS changes because it can
take a new host/user plus a local VPS profile and perform:

- remote environment bootstrap
- repo clone / pull
- venv + dependency install
- Polymarket config sync
- unattended remote run launch
- post-run log fetch

Friend quick start:

1. Create a VPS profile at `~/.polybot/vps/<name>.env`.
2. Create an account profile at `~/.polybot/accounts/<name>.json`.
3. Bootstrap the server once:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile <vps_name> --account-profile <account_name>
```

4. Start a run:

```bash
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 12
```

5. Stop a remote run if needed:

```bash
bash tools/vpsctl.sh stop --vps-profile <vps_name> --run-id latest
```

6. After the run finishes, fetch logs:

```bash
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id latest
```

Manual setup notes:

- `~/.polybot/` is only the default profile location. It may not exist yet.
- Create the default directories manually if needed:

```bash
mkdir -p ~/.polybot/vps ~/.polybot/accounts
```

- Then create:
  - `~/.polybot/vps/<name>.env`
  - `~/.polybot/accounts/<name>.json`
- If you do not want to use the default directory, pass direct file paths:

```bash
bash tools/vpsctl.sh bootstrap \
  --vps-profile /path/to/my_vps.env \
  --account-profile /path/to/my_account.json
```

If the user has an AI agent, the agent can create these directories and files
from the examples below. If the user does not have an agent, the examples and
step-by-step commands here are intended to be sufficient for manual setup.

Profile-driven usage for other users:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile sweden --account-profile alice
bash tools/vpsctl.sh run --vps-profile sweden --preset enhanced --rounds 12
bash tools/vpsctl.sh fetch --vps-profile sweden --run-id latest
```

`run` now returns an immediate startup health check from the VPS, including
`RUN_ID`, `PID`, `STATUS`, and the initial `stdout` tail, so obvious startup
failures are visible immediately instead of only after a later log fetch.

VPS profile format:

- location by name: `~/.polybot/vps/<name>.env`
- or pass a direct file path to `--vps-profile`
- shell-style `KEY=value` file

Required / supported keys:

- `HOST=70.34.207.45`
- `USER_NAME=root`
- one of:
  - `PASSWORD=your_vps_password`
  - `PASSWORD_ENV_VAR=MY_VPS_PASSWORD`
- optional:
  - `REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git`
  - `BRANCH=main`

Example `~/.polybot/vps/sweden.env`:

```bash
HOST=70.34.207.45
USER_NAME=root
PASSWORD=your_vps_password
REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git
BRANCH=main
```

Reference example file:
[docs/examples/vps_profile.example.env](/Users/forrestliao/workspace/docs/examples/vps_profile.example.env)

Account profile format:

- location by name: `~/.polybot/accounts/<name>.json`
- or pass a direct file path to `--account-profile`
- JSON file with the same structure as Polymarket CLI config

Minimum required keys:

- `private_key`
- `proxy_address`

Recommended interpretation:

- `private_key`: the exported Polymarket signer private key
- `proxy_address`: the wallet address shown in the Polymarket web UI

Optional keys with defaults:

- `chain_id`
  - default: `137`
- `signature_type`
  - default: `proxy`
- `funder`
  - optional fallback alias for `proxy_address`

Example `~/.polybot/accounts/alice.json`:

```json
{
  "private_key": "0xYOUR_PRIVATE_KEY",
  "proxy_address": "0xYOUR_PROXY_ADDRESS"
}
```

Reference example file:
[docs/examples/account_profile.example.json](/Users/forrestliao/workspace/docs/examples/account_profile.example.json)

Notes:

- `bootstrap` uploads the chosen account profile to the VPS as the active
  Polymarket config.
- Use `vpsctl.sh` with `--vps-profile` for `run`, `status`, `stop`, and
  `fetch`; the VPS password is loaded from the local profile.
- `vpsctl.sh` no longer takes password as a command-line parameter; put the
  password source in the VPS profile.
- In normal proxy-wallet usage, users usually only need to provide their
  `private_key` and the proxy wallet address they see on Polymarket.
- If `chain_id` is omitted, runtime defaults to `137`.
- If `signature_type` is omitted, runtime defaults to `proxy`.
- If `--account-profile` is omitted, `vpsctl.sh` falls back to the local
  `~/.config/polymarket/config.json`.
- Do not store other users' account profiles inside the repo.

Run tests:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

Current local status: `135 passed`.

## Notes For Future Changes

- Keep strategy, monitor, config loader, analysis script, and tests aligned.
- Runtime execution is max-only. Do not reintroduce a lower price floor unless
  explicitly requested.
- Validate parameter changes on the 96-window dataset before live testing.
- If entry logic changes, update:
  - strategy
  - monitor
  - config loader
  - analysis script
  - tests
- Do not reintroduce TP/SL/re-entry unless explicitly requested and backtested.
