# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated Polymarket trading bot for BTC 5-minute Up/Down markets, implemented as a modular Python 3.11+ package. Uses WebSocket for real-time price updates (millisecond latency) and FOK market orders with retry.

## How to Run

```bash
# Install dependencies (Python 3.11+ required)
python3.11 -m pip install -r requirements.txt

# Live trading
python3.11 btc5m_trade.py

# Dry-run (logs actions but does not place orders)
python3.11 btc5m_trade.py --dry
```

## Project Structure

```
btc5m/                      # Trading package
├── __init__.py
├── config.py               # All thresholds and constants (single source of truth)
├── auth.py                 # Loads CLI config, builds CLOBClient
├── client.py               # CLOB singleton, REST price helpers
├── market.py               # MarketWindow dataclass + slug discovery
├── stream.py               # WebSocket real-time price stream (PriceStream)
├── trading.py              # buy_up() / sell_up() with FOK + GTC fallback
├── monitor.py              # Async monitoring loop (WS event-driven)
└── notify.py               # macOS notification wrapper

btc5m_trade.py              # Async entry point (asyncio.run)
requirements.txt            # Python dependencies
.env.example                # Environment variable template
```

## Architecture

- **`auth.py`**: Reads `~/.config/polymarket/config.json` for private key / signature type. Initializes `ClobClient` with `signature_type=1` (proxy/Magic wallet).
- **`client.py`**: Lazy `ClobClient` singleton. Provides REST fallback: `get_midpoint()`, `get_tick_size()`, `round_to_tick()`.
- **`market.py`**: Slug number = Unix epoch of window start. Fetches from Gamma API (direct slug lookup for current window + batch for future windows).
- **`stream.py`**: `PriceStream` class — WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribes to token IDs, emits `PriceUpdate` via callback. Handles `PING` every 10s.
- **`trading.py`**: FOK market orders with **10× retry at 100ms** (1 second total). Falls back to GTC limit at midpoint if FOK fails.
- **`monitor.py`**: Async event-driven loop. `PriceStream` callbacks immediately trigger buy / stop-loss / take-profit. `exit_triggered` flag prevents re-triggering.
- **`config.py`**: All thresholds in one place.

## Key Parameters (in `btc5m/config.py`)

| Variable | Default | Description |
|---|---|---|
| `BUY_AMOUNT` | 5.0 | Dollars per trade |
| `BUY_THRESHOLD_LOW/HIGH` | 0.45 / 0.55 | Only buy when Up price is in this range |
| `STOP_LOSS` | 0.30 | Sell if Up drops below this |
| `TAKE_PROFIT` | 0.80 | Sell if Up rises above this |
| `FOK_RETRY_COUNT` | 10 | FOK attempts before fallback |
| `FOK_RETRY_INTERVAL` | 0.1 | Seconds between FOK retries |
| `BASE_SLUG_NUM` | 1776182700 | Anchor for slug calculation (update on series reset) |

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
- `polymarket` CLI at `~/.config/polymarket/config.json`
