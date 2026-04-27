# AGENTS.md - Implementation Guidance

## Project Status

Polymarket BTC 5-minute UP/DOWN bot. Status: live-capable.

Current rule: `paired_window` is the only active runtime strategy. Do not
restore historical strategies, conservative configs, TP, reversal, re-entry,
dynamic strength caps, early-entry bypass, or theoretical `1 - up_price`
execution gating unless the user explicitly asks and fresh tests are added.

## Current Strategy

Active config: `paired_window_early_entry_dry.yaml`.

The filename contains `dry`, but live/dry is controlled only by `--dry`.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
  persistence_sec: 10
  entry_start_remaining_sec: 255
  entry_end_remaining_sec: 180
  max_entry_price: 0.72
  min_move_ratio: 0.7

params:
  amount: 1.0
  entry_ask_level: 7
  low_price_threshold: 0.60
  low_price_entry_ask_level: 9
  amount_tiers:
    - threshold: 2.0
      amount: 1.5
  max_entries_per_window: 1
  stop_loss:
    enabled: false
    multiplier: 1.2
    start_remaining_sec: 120
    end_remaining_sec: 15
    sell_bid_level: 9
    retry_count: 3
    min_sell_price: 0.20
```

Runtime behavior:

- BTC baseline is the current 5-minute window open.
- Entry band is `remaining=[255s,180s]`, i.e. 45s to 120s after open.
- Require `abs(move_pct) >= theta_pct`, same-direction persistence
  `persistence_sec` ago, and current move >= `min_move_ratio * past_move`.
- Lock the first valid direction per window.
- Hard cap is `0.72`; no dynamic cap tiers.
- Execution uses target-leg WS order-book depth.
- Level 1 ask is diagnostic only; fillability starts from level 2.
- First FAK hint uses ask level 7, or ask level 9 when top ask is `<0.60`.
- All hints are clamped to cap.
- `signal_strength >= 2.0` uses amount `1.5`; timing does not change.
- Optional stop-loss exists but is disabled by default.
- Hold to `window.end_epoch`; no exit logic before resolution.

Stop-loss behavior when enabled:

- Trigger price: `max(min_sell_price, (1 - entry_avg_price) * multiplier)`.
- Only active while `start_remaining_sec >= remaining >= end_remaining_sec`.
- Uses held-leg bid book, skips level 1, and defaults to bid level 9.
- SELL FAK retry count defaults to 3.
- On fill, record realized PnL and exit that window.

## Core Files

- `run.py` — local runner
- `polybot/strategies/paired_window.py` — BTC signal and direction lock
- `polybot/trading/monitor.py` — window lifecycle, depth gating, FAK retry, logs, risk
- `polybot/trading/trading.py` — Polymarket CLOB order execution
- `polybot/core/state.py` — shared monitor/risk state
- `polybot/config_loader.py` — YAML loader and object builders
- `polybot/runtime_config.py` — `--preset` / `--config` assembly
- `polybot/runtime_inputs.py` — CLI/UI input schema and validation
- `tools/vpsctl.sh` — bootstrap/run/status/stop/fetch/probe for VPS
- `tools/remote_start_run.sh` — remote unattended wrapper installed by `vpsctl`
- `tools/collect_data.py` — collector
- `tools/probe_post_order_latency.py` — intentional-fail `/order` latency probe

## Commands

Local dry:

```bash
python3.11 run.py --preset enhanced --dry --rounds 3
```

Local live:

```bash
python3.11 run.py --preset enhanced --rounds 3
```

Tests:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

VPS bootstrap:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile <vps_name> --account-profile <account_name>
```

VPS run/status/stop/fetch:

```bash
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 6 --label test6
bash tools/vpsctl.sh status --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh stop --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id latest
```

Always use `--vps-profile` for VPS commands so host/user/password are loaded
from profile. Do not run raw `ssh`/`scp` for stop/status unless profile password
is also loaded.

## Profiles

Default paths:

- VPS profile: `~/.polybot/vps/<name>.env`
- Account profile: `~/.polybot/accounts/<name>.json`

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

## Logging Notes

Important fields:

- `signal_price`: UP-leg signal reference.
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
- Do not commit logs, `data/`, remote run folders, local profiles, or secrets.
- Do not change exit timing away from `window.end_epoch`.
