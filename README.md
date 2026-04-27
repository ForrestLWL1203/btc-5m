# Polybot

Polymarket BTC 5-minute UP/DOWN trading bot.

Current repo policy: one runtime strategy only, `paired_window`. Historical
strategy configs, old backtest scripts, and retired VPS wrappers have been
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

Behavior:

- BTC baseline is the current 5-minute window open.
- Entry band is `remaining=[255s,180s]`, i.e. 45s to 120s after window start.
- Signal requires `abs(move_pct) >= theta_pct`, same-direction persistence
  `persistence_sec` ago, and current move at least `min_move_ratio * past_move`.
- First valid direction is locked for the window.
- Hard max entry cap is `0.75`; there are no dynamic strength caps.
- Execution uses target-leg WS order-book depth, not theoretical `1 - up_price`.
- Level 1 ask is diagnostic only; fillability starts from level 2.
- First FAK hint uses ask level 9 by default, or level 11 when top ask is `<0.60`.
- Strong signals only increase amount to `1.5` at `signal_strength >= 2.0`; they
  do not bypass the 45s timing gate.
- No TP, reversal, re-entry, or lower entry-price floor.
- Stop-loss support exists but is disabled by default.

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
stop_price = max(min_sell_price, (1 - entry_avg_price) * multiplier)
```

Execution constraints:

- Only active while holding and `start_remaining_sec >= remaining >= end_remaining_sec`.
- Default range: `120s >= remaining >= 15s`.
- Uses held-leg bid book, not ask book.
- Level 1 bid is skipped; sell depth targets `sell_bid_level=9` by default.
- SELL FAK price hint is placed below the selected bid level and retried up to
  `retry_count=3`.
- If stop-loss fills, the bot records realized PnL and exits the window.

## VPS Profiles

Profiles do not need to live beside `vpsctl.sh`. Default locations:

- VPS profile: `~/.polybot/vps/<name>.env`
- Account profile: `~/.polybot/accounts/<name>.json`

Create directories manually if needed:

```bash
mkdir -p ~/.polybot/vps ~/.polybot/accounts
```

VPS profile example:

```bash
HOST=70.34.207.45
USER_NAME=root
PASSWORD='your-vps-password'
REPO_URL=https://github.com/ForrestLWL1203/btc-5m.git
BRANCH=main
```

Instead of `PASSWORD`, you may use `PASSWORD_ENV_VAR=MY_VPS_PASSWORD` and set
that environment variable before running `vpsctl.sh`.

Account profile example:

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

## VPS Commands

Bootstrap a new VPS:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile sweden --account-profile main
```

Start a remote run:

```bash
bash tools/vpsctl.sh run --vps-profile sweden --preset enhanced --rounds 6 --label test6
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

Probe latency remotely:

```bash
bash tools/vpsctl.sh probe --vps-profile sweden --token-id <TOKEN_ID> --side buy --price 0.01 --size 1 --repeats 3
```

Use `--vps-profile` for `run/status/stop/fetch`; the password is read from the
profile so commands do not fail and retry interactively.

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
