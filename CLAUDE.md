# CLAUDE.md - Current Runtime State

One active runtime strategy: `paired_window`.

Historical strategy configs/scripts/tests have been removed. Treat
`paired_window_early_entry_dry.yaml` as the only maintained runtime config. The
filename contains `dry`, but `--dry` controls dry/live mode.

## Strategy

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
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
    multiplier: 1.2
    start_remaining_sec: 120
    end_remaining_sec: 15
    sell_bid_level: 9
    retry_count: 3
    min_sell_price: 0.20
```

Rules:

- BTC baseline is current window open.
- Entry band is 45s to 120s after window start.
- Need theta, persistence, same direction, and non-fading move.
- Direction locks once per window.
- Hard cap is `0.75`; no strength cap tiers and no early-entry bypass.
- WS order-book depth drives execution; level 1 is skipped for fillability.
- Initial FAK hint uses level 9, or level 11 if top ask `<0.60`.
- `signal_strength >= 2.0` increases amount to `1.5` only.
- Optional stop-loss exists but is disabled by default.
- Hold to `window.end_epoch` unless optional stop-loss is enabled and fills.
- No TP, reversal, or re-entry.

Stop-loss when enabled:

- Trigger price is `max(min_sell_price, (1 - entry_avg_price) * multiplier)`.
- Active only while `start_remaining_sec >= remaining >= end_remaining_sec`.
- Uses held-leg bid book, skips level 1, and defaults to bid level 9.
- SELL FAK retries up to 3 times.
- A filled stop records realized PnL and exits the window.

## Core Files

- `run.py`
- `polybot/strategies/paired_window.py`
- `polybot/trading/monitor.py`
- `polybot/trading/trading.py`
- `polybot/core/state.py`
- `polybot/config_loader.py`
- `polybot/runtime_config.py`
- `polybot/runtime_inputs.py`
- `tools/vpsctl.sh`
- `tools/remote_start_run.sh`
- `tools/collect_data.py`
- `tools/probe_post_order_latency.py`

## Commands

```bash
python3.11 run.py --preset enhanced --dry --rounds 3
python3.11 run.py --preset enhanced --rounds 3
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

VPS:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile <vps_name> --account-profile <account_name>
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 6 --label test6
bash tools/vpsctl.sh status --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh stop --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id latest
```

Always use `--vps-profile` for remote run/status/stop/fetch so password is
loaded from `~/.polybot/vps/<name>.env`.

## Profiles

VPS profile:

```bash
HOST=70.34.207.45
USER_NAME=root
PASSWORD='your-vps-password'
REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git
BRANCH=main
```

Account profile:

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

## Logging

- `SIGNAL`: BTC signal.
- `SIGNAL_EVAL`: selected target-leg book depth.
- `ENTRY_DEPTH_SKIP`: first insufficient-depth skip per window.
- `BUY_SIGNAL`: order path starts.
- `BUY_PREP`: FAK order about to be built/posted.
- `FAK_FILLED` / `FAK_ATTEMPT_FAILED`: result and latency breakdown.
- `TRADE_RESOLVED`: window-end outcome.
- `SUMMARY`: per-window aggregate.

Price fields:

- `signal_price`: UP-leg signal reference.
- `best_ask_level_1`: target-leg top ask, diagnostic only.
- `target_entry_ask`: selected depth level price.
- `price_hint`: clamped FAK hint.
- `FAK_FILLED.avg_price`: actual average fill.

## Guardrails

- Do not restore conservative config, analysis experiments, early-entry bypass,
  strength caps, normal full-cap guard, reversal, TP, or re-entry.
- Do not use theoretical `1 - up_price` for final execution permission.
- Do not commit logs, `data/`, `remote_runs/`, profiles, or secrets.
- Keep docs and tests aligned with the current single strategy.
