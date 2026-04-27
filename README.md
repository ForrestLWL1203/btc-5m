# Polybot

Polymarket BTC 5-minute UP/DOWN trading bot.

Current repo policy: BTC 5-minute only, one runtime strategy only:
`paired_window`. Historical strategies, extra market/timeframe support,
conservative configs, old backtest scripts, and retired VPS wrappers have been
removed.

## Active Strategy

Config:
[paired_window_early_entry_dry.yaml](/Users/forrestliao/workspace/paired_window_early_entry_dry.yaml)

The filename contains `dry`, but live/dry behavior is controlled only by
`--dry`. Without `--dry`, the bot places real orders.

```yaml
strategy:
  type: paired_window
  theta_pct: 0.03
  theta_start_pct: 0.025
  theta_end_pct: 0.04
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

Behavior:

- Runtime market is fixed to `btc-updown-5m`. `--market` only accepts `btc` and
  `--timeframe` only accepts `5m`.
- BTC baseline is the current 5-minute window open.
- Entry band is `remaining=[255s,180s]`, i.e. 45s to 120s after window start.
- Signal threshold is dynamic: `theta_start_pct=0.025%` at 45s after open,
  linearly rising to `theta_end_pct=0.04%` at 120s after open. `theta_pct=0.03%`
  remains the fixed-threshold fallback if dynamic fields are absent.
- Signal also requires same-direction persistence `persistence_sec` ago and
  current move at least `min_move_ratio * past_move`.
- First valid direction is locked for the window.
- Hard max entry cap is `0.75`; there are no dynamic strength caps.
- Execution uses target-leg WS order-book depth, not theoretical `1 - up_price`.
- Level 1 ask is diagnostic only; fillability starts from level 2.
- First FAK hint scans from ask level 2 up to level 9 by default, or up to
  level 11 when top ask is `<0.60`; if cumulative depth covers the order
  earlier, it uses that earlier level.
- Strong signals only increase amount to `1.5` at `signal_strength >= 2.0`; they
  do not bypass the 45s timing gate.
- No TP, reversal, re-entry, or lower entry-price floor.
- Stop-loss support exists but is disabled by default.
- When live stop-loss is enabled, SELL size is read from the actual CLOB token
  balance before exit; estimated runtime shares are only a fallback if balance
  lookup fails.
- Removed legacy stop-loss multiplier/config compatibility path; stop-loss uses
  fixed trigger fields only.

## Core Files

- [run.py](/Users/forrestliao/workspace/run.py) — local dry/live runner
- [polybot/strategies/paired_window.py](/Users/forrestliao/workspace/polybot/strategies/paired_window.py) — BTC signal and direction lock
- [polybot/trading/monitor.py](/Users/forrestliao/workspace/polybot/trading/monitor.py) — window lifecycle, book-depth gate, FAK retry, logging, risk
- [polybot/trading/trading.py](/Users/forrestliao/workspace/polybot/trading/trading.py) — Polymarket CLOB order execution
- [polybot/runtime_config.py](/Users/forrestliao/workspace/polybot/runtime_config.py) — preset/config startup assembly
- [polybot/runtime_inputs.py](/Users/forrestliao/workspace/polybot/runtime_inputs.py) — CLI/UI input schema and validation
- [tools/vpsctl.sh](/Users/forrestliao/workspace/tools/vpsctl.sh) — bootstrap/run/status/stop/fetch/probe on VPS
- [tools/remote_start_run.sh](/Users/forrestliao/workspace/tools/remote_start_run.sh) — remote unattended run wrapper installed by `vpsctl`
- [tools/collect_data.py](/Users/forrestliao/workspace/tools/collect_data.py) — BTC + Polymarket collector
- [tools/probe_post_order_latency.py](/Users/forrestliao/workspace/tools/probe_post_order_latency.py) — intentional-fail `/order` latency probe

## Local Commands

Run dry:

```bash
python3.11 run.py --preset enhanced --dry --rounds 3
```

Run live:

```bash
python3.11 run.py --preset enhanced --rounds 3
```

Override common runtime fields:

```bash
python3.11 run.py --preset enhanced --dry --rounds 6 --amount 1.5 --max-entry-price 0.70
```

Run tests:

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

Current expected test suite size: 123 tests.

Collect data:

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/collect_data.py \
  --market btc-updown-5m --windows 96 --no-snap --slim --poly-min-interval-ms 100
```

Probe `/order` latency with an intentionally unfillable price:

```bash
PYTHONPATH=/Users/forrestliao/workspace python3.11 tools/probe_post_order_latency.py \
  --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

## Optional Stop Loss

Stop loss is off unless `params.stop_loss.enabled=true`.

Trigger:

```text
if entry_avg_price < disable_below_entry_price:
    stop-loss disabled
else:
    stop_price = max(min_sell_price, trigger_price)
```

Execution constraints:

- Only active while holding and `start_remaining_sec >= remaining >= end_remaining_sec`.
- Default range: `120s >= remaining >= 15s`.
- Default trigger is around `0.38`; entries below `0.45` do not use stop-loss.
- Uses held-leg bid book, not ask book.
- Level 1 bid is skipped; sell depth scans up to `sell_bid_level=10` by default.
- Live runs sync actual CLOB token balance about 8 seconds after BUY fill, and
  check balance again before stop-loss SELL.
- SELL FAK price hint is placed below the selected bid level and retried up to
  `retry_count=3`.
- If stop-loss fills, the bot records realized PnL and exits the window.

## VPS Profiles

Remote usage is driven by local profile files. Profiles do not need to live
beside `vpsctl.sh`. Default locations:

- VPS profile: `~/.polybot/vps/<name>.env`
- Account profile: `~/.polybot/accounts/<name>.json`

Create directories manually if needed:

```bash
mkdir -p ~/.polybot/vps ~/.polybot/accounts
chmod 700 ~/.polybot ~/.polybot/vps ~/.polybot/accounts
```

VPS profile example, e.g. `~/.polybot/vps/sweden.env`:

```bash
HOST=70.34.207.45
USER_NAME=root
PASSWORD='your-vps-password'
REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git
BRANCH=main
```

Instead of `PASSWORD`, you may use `PASSWORD_ENV_VAR=MY_VPS_PASSWORD` and set
that environment variable before running `vpsctl.sh`.

Account profile example, e.g. `~/.polybot/accounts/main.json`:

```json
{
  "private_key": "0x...",
  "proxy_address": "0x...",
  "chain_id": 137,
  "signature_type": 1
}
```

Required: `private_key`, `proxy_address`. Defaults: `chain_id=137`,
`signature_type=1`.

Do not commit either profile. They contain server credentials and Polymarket
account secrets.

## VPS Commands

Bootstrap a new VPS or refresh dependencies and account config:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile sweden --account-profile main
```

What bootstrap does: installs git/python/venv dependencies, clones or updates
`REPO_URL` into `/opt/polybot/current`, installs requirements, copies the
account profile to `/opt/polybot/shared/polymarket_config.json` and
`/root/.config/polymarket/config.json`, and installs remote helpers:
`polybot-update`, `polybot-run`, `polybot-probe`, `polybot-remote-start`.

Start a remote run:

```bash
bash tools/vpsctl.sh run --vps-profile sweden --preset enhanced --rounds 6 --label test6
```

Live is the default. Add `--dry` before `--label` for a remote dry run. Extra
`run.py` args are passed after `--`; for example enable stop-loss:

```bash
bash tools/vpsctl.sh run --vps-profile sweden --preset enhanced --rounds 5 \
  --label live5_stoploss -- --stop-loss-enabled
```

Check status:

```bash
bash tools/vpsctl.sh status --vps-profile sweden --run-id latest
```

Stop a run:

```bash
bash tools/vpsctl.sh stop --vps-profile sweden --run-id latest
```

Fetch logs:

```bash
bash tools/vpsctl.sh fetch --vps-profile sweden --run-id latest
```

Runs persist one structured analysis log: `<market>_trade.jsonl`. Human-readable
logs are stdout/stderr only; remote runs capture them in `stdout.log`. Fetched
logs are copied to `remote_runs/<host_ip_with_underscores>/<RUN_ID>/`.

Probe latency remotely:

```bash
bash tools/vpsctl.sh probe --vps-profile sweden --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

Use `--vps-profile` for `run/status/stop/fetch`; the password is read from the
profile so commands do not fail and retry interactively.

Avoid raw `ssh`/`scp` unless you also load the VPS profile password. In Codex,
prefer `bash tools/vpsctl.sh ... --vps-profile <name-or-path>` for all remote
actions.

## Logging

Important events:

- `SIGNAL`: BTC direction signal created.
- `SIGNAL_EVAL`: target-leg book depth evaluated.
- `ENTRY_DEPTH_SKIP`: first insufficient-depth skip per window; later repeats
  are aggregated into `SUMMARY`.
- `BUY_SIGNAL`: FAK attempt is about to be prepared.
- `BUY_PREP`: order creation/posting starts.
- `FAK_FILLED` / `FAK_ATTEMPT_FAILED`: order result and latency breakdown.
- `TRADE_RESOLVED`: window-end outcome.
- `SUMMARY`: compact per-window summary, including depth-skip aggregates.

Key price fields:

- `signal_price`: UP-leg signal reference price.
- `best_ask_level_1`: target-leg top ask, diagnostic only.
- `target_entry_ask`: selected depth level price.
- `price_hint`: FAK price hint, clamped to cap.
- `FAK_FILLED.avg_price`: actual average fill price.
