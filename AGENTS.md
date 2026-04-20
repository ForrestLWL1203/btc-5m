# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

Automated Polymarket trading bot for BTC/ETH Up/Down markets across multiple timeframes (5m/15m/4h). Uses a latency arbitrage strategy that exploits BTC price lead (~0.75s) over Polymarket token prices. Real-time Binance WebSocket feed + linear regression model for edge computation. Direction determined per-tick by edge sign (positive=up, negative=down).

## How to Run

```bash
# Install dependencies (Python 3.11+ required)
python3.11 -m pip install -r requirements.txt

# Dry-run (recommended)
python3.11 run.py --config latency_arb.yaml --dry

# CLI mode with custom params
python3.11 run.py --market btc-updown-5m --amount 1 --tp-price 0.80 --sl-pct 0.05 --dry

# Live trading (remove --dry)
python3.11 run.py --config latency_arb.yaml
```

## Data Collection & Analysis

```bash
# Collect paired BTC + Polymarket tick data
python3.11 tools/collect_data.py --market btc-updown-5m --windows 10

# Analyze collected data (latency, reaction model, edge opportunities)
python3.11 analysis/analyze_data.py data/collect_btc-updown-5m_*.jsonl

# Edge quality bucket analysis (edge × flow × velocity)
python3.11 analysis/analyze_edge_quality.py data/collect_btc-updown-5m_*.jsonl

# Edge decay analysis (optimal hold time, half-life)
python3.11 analysis/analyze_edge_decay.py data/collect_btc-updown-5m_*.jsonl

# Parameter scan (edge / cooldown / re-entry caps / phased caps)
python3.11 analysis/analyze_param_scan.py data/collect_btc-updown-5m_*.jsonl
```

## Project Structure

```
polybot/                    # Trading package
├── __init__.py
├── config_loader.py        # YAML config loading + strategy registry (latency_arb only)
├── trade_config.py         # TradeConfig dataclass — common params + check_exit()
├── core/                   # Core infrastructure
│   ├── __init__.py
│   ├── auth.py             # Loads CLI config, builds CLOBClient
│   ├── client.py           # CLOB singleton, REST helpers, order param prefetch
│   ├── config.py           # Fallback constants
│   ├── log_formatter.py    # Structured JSON + console logging
│   └── state.py            # MonitorState — trading state + target_side override + entry timestamps
├── market/                 # Market data layer
│   ├── __init__.py
│   ├── binance.py           # BinanceTradeFeed — real-time BTC WS + feature extraction
│   ├── market.py           # MarketWindow dataclass + exact slug discovery + future-window chaining
│   ├── series.py           # MarketSeries — market identity (asset, timeframe, slug params)
│   └── stream.py           # WebSocket real-time price stream (PriceStream)
├── strategies/             # Trading strategies
│   ├── __init__.py
│   ├── base.py             # Strategy ABC (get_side + should_buy)
│   └── latency_arb.py      # LatencyArbStrategy — BTC lead signal, phased multi-signal entry
└── trading/                # Order execution + monitoring
    ├── __init__.py
    ├── monitor.py          # Async monitoring loop (WS event-driven + edge exit hooks + dry-run PnL)
    └── trading.py          # buy_token() / sell_token() with FOK + GTD fallback

run.py                      # Entry point (--config YAML or CLI args)
tools/collect_data.py       # Dual-WS data collector (Binance + Polymarket ticks)
analysis/common.py          # Shared analysis utilities and data structures
analysis/analyze_data.py    # Data analysis (latency, reaction model, edge opportunities)
analysis/analyze_edge_quality.py # Edge bucket analysis (edge × flow × velocity)
analysis/analyze_edge_decay.py   # Edge decay analysis (optimal hold time, half-life)
analysis/analyze_param_scan.py   # Parameter scan for cooldown / edge / trade caps
latency_arb.yaml            # Main latency arb strategy config
latency_arb_fast.yaml       # Faster variant
latency_arb_probe.yaml      # Experimental probe config
docs/polymarket_api.md      # Polymarket API reference
requirements.txt            # Python dependencies
```

## Architecture

- **`core/auth.py`**: Reads `~/.config/polymarket/config.json` for private key / signature type. Initializes `ClobClient` with `signature_type=1` (proxy/Magic wallet).
- **`core/client.py`**: Lazy `ClobClient` singleton. REST helpers: `get_midpoint()`, `get_tick_size()`, `get_token_balance(safe)`, `round_to_tick()`. Balance truncation uses proportional safety margin (`min(tick, raw * 0.01)`). Order param prefetch: `prefetch_order_params()` + `get_order_options()` to skip SDK internal API calls during order placement.
- **`core/config.py`**: Legacy constants — still used as defaults when no Strategy/Series is provided.
- **`core/log_formatter.py`**: `ConsoleFormatter` (human-readable with `[EVENT_TYPE]` prefix) and `JsonFormatter` (JSONL for frontend).
- **`core/state.py`**: `MonitorState` dataclass — tracks buy/hold state, entry price, original entry price (preserved across re-entries), SL/TP/edge-exit counts, deferred signals, trade lock, `target_side` direction override for latency arb, entry timestamps for phased caps, and dry-run realized PnL.
- **`market/binance.py`**: `BinanceTradeFeed` — real-time Binance trade WebSocket with rolling price history and flow windows. `compute_features()` returns `BtcFeatures` (ret_2s, ret_5s, velocity, abs_vel, flow_imbalance, data_age_ms) using efficient indexed lookups, pruning, and cached snapshots tuned for high-frequency use.
- **`market/market.py`**: Slug number = Unix epoch of window start. Fetches from Gamma API, only accepts exact slug matches, and can scan forward to the next future-not-yet-active window so the monitor does not chain back into an expired window.
- **`market/series.py`**: `MarketSeries` frozen dataclass — defines a market series (asset, timeframe, slug params, window buffer). `KNOWN_SERIES` registry for known BTC/ETH markets (6 series: btc/eth × 5m/15m/4h).
- **`market/stream.py`**: `PriceStream` class — WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribes to token IDs, emits `PriceUpdate` via callback. Auto-reconnect with exponential backoff. `switch_tokens()` for WS reuse across windows.
- **`trading/trading.py`**: FOK market orders with **10× retry at 100ms** (1 second total). Falls back to GTD limit at midpoint if FOK fails. Uses `PartialCreateOrderOptions` to skip SDK overhead.
- **`trading/monitor.py`**: Async event-driven loop (908 lines). Key behaviors:
  - `PriceStream` callbacks immediately trigger buy / stop-loss / take-profit
  - `strategy` parameter required (no default fallback)
  - `state.target_side` override for latency arb dynamic direction
  - Edge exit hook (`check_edge_exit`) for fast-sell when edge reverses or decays
  - SL time gate: 5m→2m30s, 15m→5min, 4h→1h before SL allowed
  - Deferred signal mechanism: when `trade_lock` held, signals stored and replayed after release
  - Post-buy deferred signal discard only for stale contexts; deferred replay works for DOWN-side paths too
  - Re-entry price gates bypassed (direction can flip between trades)
  - Window-ending sell uses `trade_lock` + state flags to prevent double-sell
  - `_sell_with_retry()` with balance-refreshing (3 attempts, 0.3s between)
  - `_cleanup_residual()` for dust (threshold: 0.005 shares)
  - `strategy.set_window_start()` notification for entry time gate
  - `_handle_opening_price` now respects `strategy.should_buy()` instead of unconditional opening buys
  - Calls `strategy.on_buy_confirmed()` so `max_hold_sec` starts from real fills
  - Optimistic/pessimistic price aggregation (TP=max of midpoint/trade/ask, SL=min of midpoint/trade/bid)
  - Dry-run emits approximate per-trade/per-window PnL and skips live `cancel-all` / sell cleanup
  - Fresh-start attach tolerance allows taking over windows that began within the last 60 seconds
- **`strategies/base.py`**: `Strategy` ABC with `get_side() -> Optional[str]` and `should_buy(price, state) -> bool`.
- **`strategies/latency_arb.py`**: `LatencyArbStrategy` — latency arbitrage exploiting BTC price lead over Polymarket. Multi-signal entry: edge threshold, noise filter, freshness, price band, persistence, cooldown, minimum re-entry gap, edge re-arm, and phased caps across the window (`phase_one_sec`, `max_entries_phase_one`, `phase_two_sec`, `max_entries_phase_two`, `disable_after_sec`). Exit on edge reversal/decay or max hold time (2s), with `edge_decay_grace_ms` suppressing only immediate decay exits after buy. Feature caching refreshes `data_age_ms` even without a new Binance tick. Diagnostic log throttling (max every 5s). Calibrated from paired tick data via `analysis/analyze_edge_quality.py`.
- **`trade_config.py`**: `TradeConfig` dataclass — common trading params (amount, tp_pct/sl_pct OR tp_price/sl_price, max_*_reentry, max_entries_per_window, rounds). Supports both percentage and absolute price TP/SL. Absolute takes priority when both set. In the current latency-arb configs, TP/SL act as backstops while edge exit + max hold provide the primary exit logic.
- **`config_loader.py`**: YAML config loading, `STRATEGY_REGISTRY` (latency_arb only), `build_series()`, `build_strategy()`, `build_trade_config()` factory functions.

## Key TradeConfig Parameters

| Parameter | Default | Description |
|---|---|---|
| `amount` | 5.0 | USD per trade |
| `tp_pct` | 0.50 | +50% from entry price → sell at entry * 1.50 |
| `sl_pct` | 0.30 | -30% from entry price → sell at entry * 0.70 |
| `tp_price` | None | Absolute price TP (overrides tp_pct if both set) |
| `sl_price` | None | Absolute price SL (overrides sl_pct if both set) |
| `max_sl_reentry` | 0 | Max re-buys after stop-loss (0 = disabled) |
| `max_tp_reentry` | 0 | Max re-buys after take-profit (0 = disabled) |
| `max_edge_reentry` | 0 | Max re-buys after edge exit (0 = disabled) |
| `max_entries_per_window` | None | Hard cap on entries per window (None = unlimited) |
| `rounds` | None | Number of complete windows to run (None = infinite) |

## Latency Arbitrage Strategy

Exploits the ~0.75s reaction delay between BTC moves on Binance and Polymarket token price updates.

**Reaction function**: Linear regression with return/velocity features predicting UP token delta. Runtime entry logic also uses flow imbalance, freshness, and persistence gates. Calibrated from `analysis/analyze_data.py` on paired tick data.

**Edge quality findings** (from 10-window analysis):
- Edge < 0.02: noise, negative PnL after fees
- Edge ≥ 0.02: positive net PnL (+0.031/trade)
- Best hold time: 2.0s
- Optimal entry: edge > 0.02 + persistence 200ms + strong flow alignment

**Entry filters**: edge threshold, noise filter, freshness, entry price band (`0.25 <= price <= 0.70` in the main config), entry time gate (first 4 min), persistence, cooldown, minimum re-entry gap, edge re-arm, and phased caps.

**Exit triggers**: edge reversed, edge decayed below fraction, TP/SL backstops, max hold time (2s), window-end forced sell. `edge_decay_grace_ms=300` suppresses only immediate decay-only exits after entry.

**Multiple trades per window**: controlled explicitly with `max_edge_reentry` and `max_entries_per_window`, plus phased caps that distribute trades across the first 3 minutes of the window.

**Direction**: Per-tick by edge sign. Positive edge → buy UP, negative edge → buy DOWN. Not a per-window prediction.

## Config Files

- `latency_arb.yaml` — main config (`edge_threshold=0.02`, `max_data_age_ms=800`, `min_entry_price=0.25`, `max_entry_price=0.70`, `max_hold_sec=2.0`, `edge_decay_grace_ms=300`, `persistence_ms=200`, `cooldown_sec=1.0`, phased caps `2 / 3 / disable after 180s`, `tp_price=0.80`, `sl_pct=0.05`, `max_edge_reentry=3`, `max_entries_per_window=4`)
- `latency_arb_fast.yaml` — same core logic with `cooldown_sec=0.5`
- `latency_arb_probe.yaml` — experimental (`max_data_age_ms=1000`, `persistence_ms=120`, `rounds=1`)

## WebSocket Protocol

- **Polymarket URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Subscribe**: `{"assets_ids": [...], "operation": "subscribe", "custom_feature_enabled": true}`
- **Unsubscribe**: `{"assets_ids": [...], "operation": "unsubscribe"}`
- **Heartbeat**: Send `PING` every 10 seconds
- **Key events**: `best_bid_ask`, `price_change`, `last_trade_price`, `tick_size_change`, `new_market`, `market_resolved`
- **Binance URL**: `wss://stream.binance.com:9443/ws/btcusdt@trade` (real-time trade feed)

## API Reference

See [docs/polymarket_api.md](docs/polymarket_api.md) for complete Polymarket API documentation (CLOB, Gamma, Data APIs, order types, fill tracking, WebSocket, fees).

## Dependencies

- **Python 3.11+** — required by py-clob-client
- `py-clob-client >= 0.34.6` — Polymarket CLOB SDK
- `websockets >= 12.0` — WebSocket client (Binance + Polymarket)
- `python-dotenv` — environment variable support
- `requests` — Gamma API for market discovery
- `pyyaml >= 6.0` — YAML config file support
- `polymarket` CLI at `~/.config/polymarket/config.json`
