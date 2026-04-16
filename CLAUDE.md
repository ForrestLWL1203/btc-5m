# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated Polymarket trading bot for BTC/ETH Up/Down markets across multiple timeframes (5m/15m/1h/4h/1d). Strategy-driven architecture with pluggable strategies, YAML config, and real-time WebSocket pricing.

## How to Run

```bash
# Install dependencies (Python 3.11+ required)
python3.11 -m pip install -r requirements.txt

# YAML config + dry-run (recommended)
python3.11 run.py --config strategy.yaml --dry

# CLI args
python3.11 run.py --side up --amount 5 --dry

# Interactive mode
python3.11 run.py
```

## Project Structure

```
polybot/                    # Trading package
├── __init__.py
├── config_loader.py        # YAML config loading + strategy registry
├── trade_config.py         # TradeConfig dataclass — common params + check_exit()
├── core/                   # Core infrastructure
│   ├── __init__.py
│   ├── auth.py             # Loads CLI config, builds CLOBClient
│   ├── client.py           # CLOB singleton, REST price helpers
│   ├── config.py           # Fallback constants (legacy defaults)
│   ├── log_formatter.py    # Structured JSON + console logging
│   └── state.py            # MonitorState — trading state for the monitoring loop
├── market/                 # Market data layer
│   ├── __init__.py
│   ├── market.py           # MarketWindow dataclass + slug discovery
│   ├── series.py           # MarketSeries — market identity (asset, timeframe, slug params)
│   └── stream.py           # WebSocket real-time price stream (PriceStream)
├── strategies/             # Pluggable buy strategies
│   ├── __init__.py
│   ├── base.py             # Strategy ABC (should_buy only)
│   └── immediate.py        # ImmediateStrategy — buy at first price
└── trading/                # Order execution + monitoring
    ├── __init__.py
    ├── monitor.py          # Async monitoring loop (WS event-driven)
    └── trading.py          # buy_token() / sell_token() with FOK + GTC fallback

run.py                      # Entry point (--config YAML / CLI args / interactive)
strategy.yaml.example       # Example config file
requirements.txt            # Python dependencies
```

## Architecture

- **`core/auth.py`**: Reads `~/.config/polymarket/config.json` for private key / signature type. Initializes `ClobClient` with `signature_type=1` (proxy/Magic wallet).
- **`core/client.py`**: Lazy `ClobClient` singleton. Provides REST fallback: `get_midpoint()`, `get_tick_size()`, `round_to_tick()`.
- **`core/config.py`**: Legacy constants — still used as defaults when no Strategy/Series is provided.
- **`core/log_formatter.py`**: `ConsoleFormatter` (human-readable with `[EVENT_TYPE]` prefix) and `JsonFormatter` (JSONL for frontend).
- **`core/state.py`**: `MonitorState` dataclass — tracks buy/hold state, entry price, SL/TP counts, deferred signals, trade lock.
- **`market/market.py`**: Slug number = Unix epoch of window start. Fetches from Gamma API. Accepts `MarketSeries` for multi-market support.
- **`market/series.py`**: `MarketSeries` frozen dataclass — defines a market series (asset, timeframe, slug params, window buffer). `KNOWN_SERIES` registry for known BTC/ETH markets.
- **`market/stream.py`**: `PriceStream` class — WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribes to token IDs, emits `PriceUpdate` via callback. Handles `PING` every 10s.
- **`trading/trading.py`**: FOK market orders with **10× retry at 100ms** (1 second total). Falls back to GTC limit at midpoint if FOK fails.
- **`trading/monitor.py`**: Async event-driven loop. `PriceStream` callbacks immediately trigger buy / stop-loss / take-profit. Uses optimistic/pessimistic price aggregation (TP=max of midpoint/trade/ask, SL=min of midpoint/trade/bid). Deferred signal mechanism for race conditions.
- **`strategies/base.py`**: `Strategy` ABC with single method `should_buy(price, state) -> bool`. Buy logic only.
- **`strategies/immediate.py`**: `ImmediateStrategy` — `should_buy()` always returns `True`. Buys immediately at first price.
- **`trade_config.py`**: `TradeConfig` dataclass — common trading params (side, amount, tp_pct, sl_pct, max_*_reentry, rounds) shared across all strategies. Contains `check_exit()` for TP/SL logic.
- **`config_loader.py`**: YAML config loading, `STRATEGY_REGISTRY`, `build_series()`, `build_strategy()`, and `build_trade_config()` factory functions.

## Key TradeConfig Parameters

| Parameter | Default | Description |
|---|---|---|
| `side` | up | Which token to buy (up/down) |
| `amount` | 5.0 | USD per trade |
| `tp_pct` | 0.50 | +50% from entry price → sell at entry * 1.50 |
| `sl_pct` | 0.30 | -30% from entry price → sell at entry * 0.70 |
| `max_sl_reentry` | 0 | Max re-buys after stop-loss (0 = disabled) |
| `max_tp_reentry` | 0 | Max re-buys after take-profit (0 = disabled) |
| `rounds` | None | Number of complete windows to run (None = infinite) |

## WebSocket Protocol

- **URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Subscribe**: `{"assets_ids": [...], "operation": "subscribe", "custom_feature_enabled": true}`
- **Unsubscribe**: `{"assets_ids": [...], "operation": "unsubscribe"}`
- **Heartbeat**: Send `PING` every 10 seconds
- **Key events**: `best_bid_ask`, `price_change`, `last_trade_price`, `tick_size_change`, `new_market`, `market_resolved`

## Dependencies

- **Python 3.11+** — required by py-clob-client
- `py-clob-client >= 0.34.6` — Polymarket CLOB SDK
- `websockets >= 12.0` — WebSocket client
- `python-dotenv` — environment variable support
- `requests` — Gamma API for market discovery
- `pyyaml >= 6.0` — YAML config file support
- `polymarket` CLI at `~/.config/polymarket/config.json`
