# CLAUDE.md - Current Runtime State

Maintained live-capable runtime strategy: `paired_window`. Experimental
dry-run runtime strategy: `crowd_m1`. One active market: BTC 5-minute.

Historical strategy configs/scripts/tests, non-BTC/non-5m series support, and
legacy stop-loss multiplier compatibility have been removed. Treat
`paired_window_early_entry_dry.yaml` as the maintained live-capable runtime
config. `crowd_m1_dry.yaml` is for the explicitly requested M1 dry-run
experiment. Filenames containing `dry` do not control dry/live mode; `--dry`
does.

## Strategy

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

Rules:

- Runtime market is fixed to `btc-updown-5m`; `--market=btc` and
  `--timeframe=5m` are the only accepted runtime values.
- BTC baseline is current window open.
- Entry band is 45s to 120s after window start.
- Dynamic theta is active: 0.03% at 45s after open, linearly rising to 0.048%
  at 120s. `theta_pct=0.036%` is fallback only.
- Need persistence, same direction, and non-fading move.
- Direction locks once per window.
- Hard cap is `0.75`; no strength cap tiers and no early-entry bypass.
- WS order-book depth drives execution; level 1 is skipped for fillability.
- Initial FAK hint scans from level 2 up to level 9 by default, or up to level
  11 if top ask `<0.60`; if cumulative depth covers the order earlier, it uses
  that earlier level.
- `signal_strength >= 2.0` increases amount to `1.5` only.
- Optional stop-loss exists but is disabled by default.
- Stop-loss multiplier is removed; stop-loss uses fixed trigger fields only.
- Live stop-loss sells the actual CLOB token balance, not the estimated runtime
  share count; estimated shares are only a fallback if balance lookup fails.
- Hold to `window.end_epoch` unless optional stop-loss is enabled and fills.
- No TP, reversal, or re-entry.

## Strategy D Candidate

Saved config: `paired_window_strategy_d.yaml`.

Strategy D is a paired-window candidate from the 102-window merged dataset
backtest. It is not the default runtime config; use `--config` explicitly.

```bash
python3.11 run.py --config paired_window_strategy_d.yaml --dry --rounds 3
python3.11 run.py --config paired_window_strategy_d.yaml --rounds 3
```

Reference result on
`data/collect_btc-updown-5m_merged_20260427T183011_20260428T005206_102w.jsonl`:

- 34 trades, 24W/10L, win rate 70.59%.
- Settlement PnL `+5.7399`; mark PnL `+5.5485`.
- Stop-losses 9; false stop-losses 0.
- CSV: `analysis/backtest_collect_102w方案D_stop_end30_trades.csv`.

Key parameters:

```yaml
strategy:
  theta_start_pct: 0.035
  theta_end_pct: 0.055
  min_move_ratio: 1.0

params:
  stop_loss:
    enabled: true
    trigger_price: 0.38
    start_remaining_sec: 120
    end_remaining_sec: 30
```

## Experimental crowd_m1

```yaml
strategy:
  type: crowd_m1
  entry_elapsed_sec: 180
  entry_timeout_sec: 5
  min_ask_gap: 0.0
  min_leading_ask: 0.62
  max_entry_price: 0.75
  btc_direction_confirm: false
  btc_price_feed_source: polymarket_rtds
  btc_reverse_filter:
    enabled: true
    lookback_sec: 20
    # Unit is percent: 0.02 means 0.02%, not 2%.
    min_reverse_move_pct: 0.02

params:
  amount: 1.0
  entry_ask_level: 10
  max_slippage_from_best_ask: 0.04
  max_entries_per_window: 1
  stop_loss:
    enabled: true
    trigger_price: 0.40
    start_remaining_sec: 60
    end_remaining_sec: 45
    sell_bid_level: 10
    retry_count: 3
    min_sell_price: 0.20
```

Rules:

- At 180s after open, buy the higher-best-ask Polymarket side only if the
  leading ask is at least 0.62; gap requirement is disabled with
  `min_ask_gap=0.0`.
- Do not require BTC direction from window open to match the selected side.
- Use a BTC recent-reverse soft filter: skip UP if BTC fell at least 0.02% over
  the last 20s, and skip DOWN if BTC rose at least 0.02% over the last 20s.
- `btc_reverse_filter.min_reverse_move_pct` is in percent units: `0.02` means
  `0.02%`, not `2%`.
- The reverse filter reads BTC history from Polymarket RTDS `crypto_prices`
  (`btcusdt`) by default; Binance remains as a fallback source option while RTDS
  stability is validated.
- Reverse-filter checks log `BTC_REVERSE_FILTER_CHECK` once per
  `(history_ready, triggered)` state per window. Polymarket RTDS ignores
  malformed/non-finite values, preserves inner batch item symbols, and appends
  ordered ticks on the hot path.
- Use existing target-leg order-book depth gating; do not use backtest-only L5
  price proxies for live execution.
- Reject candidates whose leading ask is above `max_entry_price=0.75` before
  entering the depth/FAK pipeline.
- Entry scans the target-leg order book up to `entry_ask_level=10`, skipping
  level 1 for fillability and stopping at the first level whose cumulative
  depth covers the order amount.
- Selected entry ask must stay within 0.04 of target-leg best ask and at or
  below `max_entry_price=0.75`.
- Entry is event-driven: UP or DOWN Polymarket WS updates refresh the cached
  two-leg snapshot and can trigger entry immediately inside the 5s entry window;
  the 1s snapshot loop is only a fallback.
- Entry requires both UP and DOWN best-ask caches to be fresh; stale cross-leg
  books are skipped before direction selection.
- Entry logs include UP/DOWN best-ask cache age for book freshness validation.
- Crowd entry `signal_price` is the leading ask, and `active_theta_pct` remains
  empty because BTC theta is not used.
- Dry-run BUY/SELL simulates FAK latency and a tick buffer; dry BUY cap failure
  after latency locks the window and clears target entry state.
- Hold to `window.end_epoch` unless the narrow stop-loss window fills.

Stop-loss when enabled:

- Entries below 0.45 do not use stop-loss.
- Trigger price is `max(min_sell_price, trigger_price=0.40)`.
- Active only while `start_remaining_sec >= remaining >= end_remaining_sec`.
- Uses held-leg bid book, skips level 1, and defaults to scanning up to bid
  level 10.
- SELL hint uses the first bid level where cumulative depth can cover the
  actual sell size, not the deepest scanned level.
- After BUY fill, held-token WS updates are ignored until 5s before the
  stop-loss window; prewarm logs held-leg bid-book age, and active-window
  updates can trigger stop-loss immediately.
- Live runs sync actual CLOB token balance about 8 seconds after BUY fill, then
  check balance again before stop-loss SELL.
- SELL FAK retries up to 3 times.
- A filled stop records realized PnL and exits the window.

## Core Files

- `run.py`
- `polybot/strategies/paired_window.py`
- `polybot/trading/monitor.py`
- `polybot/trading/fak_quotes.py`
- `polybot/trading/fak_execution.py`
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
python3.11 run.py --preset crowd_m1 --dry --rounds 3
python3.11 run.py --preset enhanced --rounds 3
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy pytest -q
```

Expected suite size after RTDS/reverse-filter bugfixes: 185 tests.

VPS:

```bash
bash tools/vpsctl.sh bootstrap --vps-profile <vps_name> --account-profile <account_name>
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 6 --label test6
bash tools/vpsctl.sh run --vps-profile <vps_name> --preset enhanced --rounds 5 --label live5_stoploss -- --stop-loss-enabled
bash tools/vpsctl.sh status --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh stop --vps-profile <vps_name> --run-id latest
bash tools/vpsctl.sh fetch --vps-profile <vps_name> --run-id latest
```

Always use `--vps-profile` for remote run/status/stop/fetch so password is
loaded from `~/.polybot/vps/<name>.env`.

`run` defaults to live. Use `--dry` for remote dry. Extra `run.py` args go after
`--`; use this for `--stop-loss-enabled`.

Bootstrap installs/updates `/opt/polybot/current`, `/opt/polybot/venv`, account
config, and helper commands `polybot-update`, `polybot-run`, `polybot-probe`,
`polybot-remote-start`.

## Profiles

Default paths are `~/.polybot/vps/<name>.env` and
`~/.polybot/accounts/<name>.json`. Create with:

```bash
mkdir -p ~/.polybot/vps ~/.polybot/accounts
chmod 700 ~/.polybot ~/.polybot/vps ~/.polybot/accounts
```

Profiles can also be passed by full path. VPS profile:

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

Never commit profiles, `log/`, `data/`, or `remote_runs/`.

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

- `signal_price`: UP-leg signal reference for `paired_window`; leading ask for
  `crowd_m1`.
- `best_ask_level_1`: target-leg top ask, diagnostic only.
- `target_entry_ask`: selected depth level price.
- `price_hint`: clamped FAK hint.
- `FAK_FILLED.avg_price`: actual average fill.
- `TRADE_RESOLVED` uses binary settlement only when the held-leg mark is fresh.
  Stale cached marks are logged as `result=MARK_STALE` with
  `mark_price_age_sec` / `mark_price_fresh` and use mark-to-mid PnL.
- Persistent run logs are JSONL only. Normal business records below `WARNING`
  go to `log/runs/<run_id>/<market>_trade.jsonl`; abnormal records at
  `WARNING` and above go to `log/runs/<run_id>/<market>_error.jsonl`.
  Human-readable normal output goes to stdout/remote `stdout.log`; abnormal
  output goes to stderr/remote `stderr.log`; do not create `*_trade.log`.

## Guardrails

- Do not restore conservative config, analysis experiments, early-entry bypass,
  strength caps, normal full-cap guard, reversal, TP, re-entry, ETH/multi-
  timeframe support, or stop-loss multiplier compatibility.
- Do not use theoretical `1 - up_price` for final execution permission.
- Do not commit logs, `data/`, `remote_runs/`, profiles, or secrets.
- Keep docs and tests aligned with the current single strategy.
