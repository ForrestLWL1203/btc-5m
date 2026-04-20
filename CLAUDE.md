# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated Polymarket trading bot for BTC/ETH Up/Down markets across multiple timeframes (5m/15m/4h). The current production path is a latency arbitrage strategy that exploits BTC price lead (~0.75s) over Polymarket token prices. It uses a real-time Binance trade feed, a linear reaction model, dynamic UP/DOWN direction selection, and a short-hold state machine with edge-based exits.

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
│   └── state.py            # MonitorState — trading state, target_side, entry timestamps, window stats
├── market/                 # Market data layer
│   ├── __init__.py
│   ├── binance.py          # BinanceTradeFeed — real-time BTC WS + feature extraction
│   ├── market.py           # MarketWindow dataclass + exact-slug discovery + future-window chaining
│   ├── series.py           # MarketSeries — market identity (asset, timeframe, slug params)
│   └── stream.py           # WebSocket real-time price stream (PriceStream)
├── strategies/             # Trading strategies
│   ├── __init__.py
│   ├── base.py             # Strategy ABC (get_side + should_buy)
│   └── latency_arb.py      # LatencyArbStrategy — BTC lead signal, gated multi-signal entry
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
latency_arb.yaml            # Main latency-arb strategy config
latency_arb_fast.yaml       # Faster variant (shorter cooldown)
latency_arb_probe.yaml      # Experimental / probing config
docs/polymarket_api.md      # Polymarket API reference
requirements.txt            # Python dependencies
```

## Architecture

- **`core/auth.py`**: Reads `~/.config/polymarket/config.json` for private key / signature type. Initializes `ClobClient` with `signature_type=1` (proxy/Magic wallet).
- **`core/client.py`**: Lazy `ClobClient` singleton. REST helpers: `get_midpoint()`, `get_tick_size()`, `get_token_balance(safe)`, `round_to_tick()`. Order param prefetch: `prefetch_order_params()` + `get_order_options()` to skip SDK internal API calls during order placement.
- **`core/log_formatter.py`**: `ConsoleFormatter` (human-readable with `[EVENT_TYPE]` prefix) and `JsonFormatter` (JSONL for frontend).
- **`core/state.py`**: `MonitorState` dataclass — tracks buy/hold state, entry price, original entry price (preserved across re-entries), SL/TP/edge-exit counts, deferred signals, trade lock, `target_side` direction override for latency arb, entry timestamps used for phased caps, and dry-run realized PnL per window.
- **`market/binance.py`**: `BinanceTradeFeed` — real-time Binance trade WebSocket with rolling price history, flow tracking, and cached feature computation. `compute_features()` returns `BtcFeatures` (`ret_2s`, `ret_5s`, `velocity`, `abs_vel`, `flow_imbalance`, `data_age_ms`) with efficient lookups and pruning tuned for high-frequency use.
- **`market/market.py`**: Slug number = Unix epoch of window start. Fetches from Gamma API, only accepts exact slug matches, and `find_window_after()` can now return the next future-not-yet-active window so monitor chaining does not loop back into the same expired window.
- **`market/series.py`**: `MarketSeries` frozen dataclass — defines a market series (asset, timeframe, slug params, window buffer). `KNOWN_SERIES` registry for known BTC/ETH markets.
- **`market/stream.py`**: `PriceStream` class — WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribes to token IDs, emits `PriceUpdate` via callback. Handles `PING` every 10s.
- **`trading/trading.py`**: FOK market orders with **10× retry at 100ms** (1 second total). Falls back to GTD limit at midpoint if FOK fails. Uses `PartialCreateOrderOptions` to skip SDK overhead.
- **`trading/monitor.py`**: Async event-driven loop. `PriceStream` callbacks immediately trigger buy / stop-loss / take-profit. Supports `state.target_side` override for latency arb dynamic direction, DOWN-side monitoring, deferred signal replay after lock release, dry-run approximate per-trade/per-window PnL, and a 60-second startup attach tolerance for already-open windows. Buy confirmation calls `strategy.on_buy_confirmed()` so max-hold timing is real. Dry-run exit paths and interrupt cleanup skip live cancel/sell actions.
- **`strategies/base.py`**: `Strategy` ABC with `get_side() -> Optional[str]` and `should_buy(price, state) -> bool`.
- **`strategies/latency_arb.py`**: `LatencyArbStrategy` — latency arbitrage exploiting BTC price lead over Polymarket. Multi-signal entry: edge threshold, persistence, freshness, price band (`min_entry_price` / `max_entry_price`), cooldown, minimum re-entry gap, edge re-arm threshold, and phased entry caps (`phase_one_sec`, `max_entries_phase_one`, `phase_two_sec`, `max_entries_phase_two`, `disable_after_sec`). Exit on edge reversal/decay or max hold time, with `edge_decay_grace_ms` suppressing only immediate post-entry decay exits. Direction is determined per tick by edge sign (positive=up, negative=down). Includes throttled diagnostic logging for blocked entries.
- **`trade_config.py`**: `TradeConfig` dataclass — common trading params (`amount`, `tp_pct/sl_pct` OR `tp_price/sl_price`, `max_*_reentry`, `max_entries_per_window`, `rounds`). Supports both percentage and absolute price TP/SL. Absolute takes priority when both set. Contains `check_exit()` for TP/SL logic.
- **`config_loader.py`**: YAML config loading, `STRATEGY_REGISTRY` (latency_arb), `build_series()`, `build_strategy()`, `build_trade_config()` factory functions.

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

**Reaction function**: Linear regression with BTC return/velocity features predicting UP token delta. Runtime features also include flow imbalance and data age gating. Calibrated from `analysis/analyze_data.py` on paired tick data.

**Edge quality findings** (from the current 10-window analysis baseline):
- Edge < 0.02: noise, negative PnL after fees
- Edge ≥ 0.02: positive net PnL (+0.031/trade)
- Best hold time: 2.0s
- Optimal entry: edge > 0.02 + persistence 200ms + strong flow alignment

**Entry filters**: edge threshold, noise filter, freshness (`max_data_age_ms`), price band (`min_entry_price <= price <= max_entry_price`), entry time gate (first 4 min), persistence, cooldown, minimum re-entry gap, edge re-arm, and phased trade distribution across the window.

**Exit triggers**: edge reversed, edge decayed below fraction, TP/SL, max hold time (2s), window-end forced sell. `edge_decay_grace_ms` prevents immediate decay-only exits during the first few hundred milliseconds after entry, but true edge reversals still exit immediately.

**Multiple trades per window**: controlled explicitly with `max_edge_reentry` and `max_entries_per_window`. The strategy now also uses `min_reentry_gap_sec`, `edge_rearm_threshold`, and phased caps so entries are spread across the window rather than clustered into a few seconds.

**Direction**: Per-tick by edge sign. Positive edge → buy UP, negative edge → buy DOWN. Not a per-window prediction.

**Config files**:
- `latency_arb.yaml` — main config, currently the safer default: `min_entry_price=0.25`, `max_data_age_ms=800`, `persistence_ms=200`, `cooldown_sec=1.0`, `edge_decay_grace_ms=300`, phase caps `2 / 3 / disable after 180s`, `tp_price=0.80`, `sl_pct=0.05`, `max_edge_reentry=3`, `max_entries_per_window=4`
- `latency_arb_fast.yaml` — faster variant with `cooldown_sec=0.5`
- `latency_arb_probe.yaml` — experimental config with `max_data_age_ms=1000` and `persistence_ms=120`

**Dry-run behavior**: Dry-run now skips live cancel-all / sell actions, logs approximate per-trade PnL, and emits per-window summaries with cumulative dry-run realized PnL.

## Data Collection

`tools/collect_data.py` collects paired BTC trade ticks (Binance WS) + Polymarket UP/DOWN prices (PriceStream WS) into JSONL. Event-driven snapshots are triggered by BTC price changes, Poly price changes, or heartbeat (200ms). `analysis/common.py` provides the shared loader, lookup, dedup, and regression helpers used by the analysis scripts.

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
