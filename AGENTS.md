# AGENTS.md - Implementation Guidance

## Project Status

Polymarket BTC 5-minute UP/DOWN bot. Status: live-capable.

Current rule: BTC 5-minute only. `paired_window` is the maintained live-capable
runtime strategy. `crowd_m1` is an explicitly requested experimental dry-run
runtime strategy for VPS testing. Do not restore historical strategies,
ETH/multi-timeframe support, conservative configs, TP, reversal, re-entry,
dynamic strength caps, early-entry bypass, stop-loss multiplier compatibility,
or theoretical `1 - up_price` execution gating unless the user explicitly asks
and fresh tests are added.

## Current Strategy

Active config: `paired_window_early_entry_dry.yaml`.

The filename contains `dry`, but live/dry is controlled only by `--dry`.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.036
  theta_start_pct: 0.03
  theta_end_pct: 0.048
  persistence_sec: 10
  entry_start_remaining_sec: 255
  entry_end_remaining_sec: 180
  max_entry_price: 0.75
  min_move_ratio: 0.7

params:
  amount: 1.0
  entry_ask_level: 9
  low_price_threshold: 0.60
  low_price_entry_ask_level: 11
  amount_tiers:
    - threshold: 2.0
      amount: 1.5
  max_entries_per_window: 1
  stop_loss:
    enabled: false
    trigger_price: 0.38
    disable_below_entry_price: 0.45
    start_remaining_sec: 120
    end_remaining_sec: 15
    sell_bid_level: 10
    retry_count: 3
    min_sell_price: 0.20
```

Runtime behavior:

- Runtime market is fixed to `btc-updown-5m`; `--market` only accepts `btc` and
  `--timeframe` only accepts `5m`.
- BTC baseline is the current 5-minute window open.
- Entry band is `remaining=[255s,180s]`, i.e. 45s to 120s after open.
- Dynamic theta is active: `0.03%` at 45s after open, linearly rising to
  `0.048%` at 120s after open. `theta_pct=0.036%` is fallback only if dynamic
  fields are absent.
- Require same-direction persistence `persistence_sec` ago and current move >=
  `min_move_ratio * past_move`.
- Lock the first valid direction per window.
- Hard cap is `0.75`; no dynamic cap tiers.
- Execution uses target-leg WS order-book depth.
- Level 1 ask is diagnostic only; fillability starts from level 2.
- First FAK hint scans from ask level 2 up to level 9 by default, or up to
  level 11 when top ask is `<0.60`; if cumulative depth covers the order
  earlier, it uses that earlier level.
- All hints are clamped to cap.
- `signal_strength >= 2.0` uses amount `1.5`; timing does not change.
- Optional stop-loss exists but is disabled by default.
- Stop-loss multiplier is removed; use fixed trigger fields only.
- Hold to `window.end_epoch`; no exit logic before resolution.

Stop-loss behavior when enabled:

- Entries below `disable_below_entry_price=0.45` do not use stop-loss.
- Trigger price: `max(min_sell_price, trigger_price=0.38)`.
- Only active while `start_remaining_sec >= remaining >= end_remaining_sec`.
- Uses held-leg bid book, skips level 1, and defaults to scanning up to bid
  level 10.
- SELL hint uses the first bid level where cumulative depth can cover the
  actual sell size, not the deepest scanned level.
- Live runs sync actual CLOB token balance about 8 seconds after BUY fill, then
  check balance again before stop-loss SELL.
- Live SELL size comes from the actual CLOB token balance before exit; estimated
  runtime shares are only a fallback if balance lookup fails.
- SELL FAK retry count defaults to 3.
- On fill, record realized PnL and exit that window.

## Strategy D Candidate

Strategy D is saved as `paired_window_strategy_d.yaml`. It is the paired-window
candidate selected from the 102-window merged dataset backtest, not the default
runtime config.

Use it explicitly:

```bash
python3.11 run.py --config paired_window_strategy_d.yaml --dry --rounds 3
python3.11 run.py --config paired_window_strategy_d.yaml --rounds 3
```

Backtest reference on
`data/collect_btc-updown-5m_merged_20260427T183011_20260428T005206_102w.jsonl`:

- 34 trades, 24W/10L, win rate 70.59%.
- Settlement PnL `+5.7399`; mark PnL `+5.5485`.
- Stop-losses 9; false stop-losses 0.
- CSV: `analysis/backtest_collect_102w方案D_stop_end30_trades.csv`.

Key differences from `paired_window_early_entry_dry.yaml`:

- `theta_start_pct=0.035`, `theta_end_pct=0.055`.
- `min_move_ratio=1.0`.
- Stop-loss enabled with trigger `0.38`, active while remaining `[120s,30s]`.

## Experimental Strategy: crowd_m1

Experimental config: `crowd_m1_dry.yaml`.

Runtime behavior:

- At `entry_elapsed_sec=180`, compare current UP and DOWN best asks; use
  `entry_timeout_sec=5` to avoid late attach entries.
- Buy the higher-best-ask side only if its leading ask is at least
  `min_leading_ask=0.62`; `min_ask_gap=0.0` disables a gap requirement.
- Do not require BTC direction confirmation; this is a pure crowd-following variant.
- Enable the BTC recent-reverse soft filter: skip UP entries if BTC dropped at
  least `0.02%` over the last 20s, and skip DOWN entries if BTC rose at least
  `0.02%` over the last 20s.
- Use existing target-leg depth-gated execution; do not replace live execution
  with backtest-only L5 price proxies.
- Cap final selected entry/hint at `max_entry_price=0.75`.
- Reject candidates whose leading ask is above `max_entry_price=0.75` before
  entering the depth/FAK pipeline.
- Use dynamic entry depth by leading ask: `<=0.64` uses L5, `<=0.68` uses L4,
  `<=0.72` uses L2, and `<=0.75` uses L1.
- Reject entries whose selected ask is more than `0.04` above target-leg best
  ask.
- Entry is event-driven: UP or DOWN Polymarket WS updates refresh the cached
  two-leg snapshot and can trigger entry immediately inside the 5s entry
  window; the 1s snapshot loop remains only as a fallback.
- Entry requires both UP and DOWN best-ask caches to be fresh; stale cross-leg
  books are skipped before direction selection.
- Entry logs include UP/DOWN best-ask cache age for book freshness validation.
- Crowd entry `signal_price` is the leading ask, and `active_theta_pct` remains
  empty because BTC theta is not used.
- Stop-loss is enabled with trigger `0.35`, only while remaining time is
  `[65s,45s]`.
- After BUY fill, held-token WS updates are ignored until 5s before the
  stop-loss window; prewarm logs held-leg bid-book age, and active-window
  updates can trigger stop-loss immediately.
- Hold to `window.end_epoch` unless stop-loss fills.

## Core Files

- `run.py` — local runner
- `polybot/strategies/paired_window.py` — BTC signal and direction lock
- `polybot/trading/monitor.py` — window lifecycle, signal-to-execution wiring, logs, risk
- `polybot/trading/fak_quotes.py` — reusable entry/stop-loss order-book quote selection
- `polybot/trading/fak_execution.py` — reusable FAK buy/stop-loss sell gateway
- `polybot/trading/trading.py` — Polymarket CLOB order execution
- `polybot/core/state.py` — shared monitor/risk state
- `polybot/config_loader.py` — YAML loader and object builders
- `polybot/runtime_config.py` — `--preset` / `--config` assembly
- `polybot/runtime_inputs.py` — CLI/UI input schema and validation
- `tools/vpsctl.sh` — bootstrap/run/status/stop/fetch/collect/probe for VPS
- `tools/remote_start_run.sh` — remote unattended wrapper installed by `vpsctl`
- `tools/collect_data.py` — collector
- `tools/probe_post_order_latency.py` — intentional-fail `/order` latency probe

## Commands

Local dry:

```bash
python3.11 run.py --preset enhanced --dry --rounds 3
python3.11 run.py --preset crowd_m1 --dry --rounds 3
```

Local live:

```bash
python3.11 run.py --preset enhanced --rounds 3
```

Tests:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

Expected suite size after crowd_m1 cleanup fixes: 165 tests.

VPS bootstrap:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile <vps_name> --account-profile <account_name>
```

VPS run/status/stop/fetch:

```bash
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 6 --label test6
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 5 --label live5_stoploss -- --stop-loss-enabled
bash tools/vpsctl.sh status --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh stop --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id latest
```

VPS collect:

```bash
bash tools/vpsctl.sh collect --vps-profile <vps_name> --windows 96 --label collect96
bash tools/vpsctl.sh status --vps-profile <vps_name> --run-id <collect_run_id>
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id <collect_run_id>
```

`collect` defaults to `--slim --no-snap --poly-min-interval-ms 100`. Pass
collector arguments after `--` to override the default argument set.

Always use `--vps-profile` for VPS commands so host/user/password are loaded
from profile. Do not run raw `ssh`/`scp` for stop/status unless profile password
is also loaded.

`run` defaults to live mode. Add `--dry` for remote dry-run. Extra `run.py`
arguments go after `--`; this is how `--stop-loss-enabled` is passed.

Bootstrap installs/updates `/opt/polybot/current`, `/opt/polybot/venv`, the
Polymarket account config, and helper commands `polybot-update`, `polybot-run`,
`polybot-probe`, `polybot-remote-start`.

## Profiles

Default paths:

- VPS profile: `~/.polybot/vps/<name>.env`
- Account profile: `~/.polybot/accounts/<name>.json`

Create dirs with `mkdir -p ~/.polybot/vps ~/.polybot/accounts` and `chmod 700`
them. Profiles may also be passed by full path, e.g.
`--vps-profile /tmp/polybot_vps_sweden.env`.

VPS profile fields:

```bash
HOST=70.34.207.45
USER_NAME=root
PASSWORD='your-vps-password'
REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git
BRANCH=main
```

Account profile required fields:

```json
{
  "private_key": "0x...",
  "proxy_address": "0x...",
  "chain_id": 137,
  "signature_type": 1
}
```

`private_key` and `proxy_address` are required. `chain_id=137` and
`signature_type=1` are defaults.

Never commit profiles. `remote_runs/`, `log/`, and `data/` are analysis/runtime
artifacts and should stay out of git.

## Logging Notes

Each runtime execution writes one structured analysis log under
`log/runs/<run_id>/<market>_trade.jsonl`.
Human-readable logs are stdout/stderr only and are captured in remote
`stdout.log`; do not reintroduce persistent `*_trade.log` files.

Important fields:

- `signal_price`: UP-leg signal reference for `paired_window`; leading ask for
  `crowd_m1`.
- `best_ask_level_1`: target-leg top ask, diagnostic only.
- `target_entry_ask`: selected depth level.
- `price_hint`: FAK hint sent to order builder.
- `FAK_FILLED.avg_price`: actual average fill.
- `ENTRY_DEPTH_SKIP` logs first insufficient-depth skip only; repeats aggregate
  into `SUMMARY`.

## Agent Rules

- Keep docs, config, strategy, monitor, runtime schema, and tests aligned.
- Do not add new runtime strategy branches for experiments; use a separate
  branch or ask first.
- Do not re-add non-BTC/non-5m market series or legacy runtime fields unless
  requested with tests.
- Do not commit logs, `data/`, remote run folders, local profiles, or secrets.
- Do not change exit timing away from `window.end_epoch`.
