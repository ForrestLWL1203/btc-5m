# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated Polymarket trading bot for BTC/ETH Up/Down markets across multiple timeframes (5m/15m/1h/4h/1d). Strategy-driven architecture with pluggable strategies, YAML config, and real-time WebSocket pricing.

## How to Run

```bash
# Install dependencies (Python 3.11+ required)
python3.11 -m pip install -r requirements.txt

# CLI — fixed side + dry-run (recommended for testing)
python3.11 run.py --market btc-updown-5m --side up --amount 1 --tp-pct 0.30 --sl-pct 0.30 --dry

# CLI — momentum auto-prediction
python3.11 run.py --market btc-updown-5m --strategy momentum --amount 1 --tp-price 0.80 --sl-pct 0.50 --rounds 1

# Live trading (remove --dry)
python3.11 run.py --market btc-updown-5m --side up --amount 1 --tp-pct 0.30 --sl-pct 0.30 --rounds 1

# YAML config
python3.11 run.py --config strategy.yaml --dry

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
│   ├── client.py           # CLOB singleton, REST helpers, order param prefetch
│   ├── config.py           # Fallback constants (legacy defaults)
│   ├── log_formatter.py    # Structured JSON + console logging
│   └── state.py            # MonitorState — trading state for the monitoring loop
├── market/                 # Market data layer
│   ├── __init__.py
│   ├── market.py           # MarketWindow dataclass + slug discovery
│   ├── series.py           # MarketSeries — market identity (asset, timeframe, slug params)
│   └── stream.py           # WebSocket real-time price stream (PriceStream)
├── predict/                # Auto direction prediction
│   ├── __init__.py         # Package exports
│   ├── history.py          # WindowHistory ring buffer + Gamma API backfill
│   ├── indicators.py       # 7 technical indicators (EMA, RSI, MACD, Bollinger, ROC, etc.)
│   ├── kline.py            # KlineCandle dataclass + BinanceKlineFetcher
│   └── momentum.py         # MomentumPredictor V3 — 7-signal weighted voting
├── strategies/             # Pluggable trading strategies
│   ├── __init__.py
│   ├── base.py             # Strategy ABC (get_side + should_buy)
│   ├── immediate.py        # FixedSideStrategy — fixed direction, buy at first price
│   └── momentum.py         # MomentumStrategy — wraps MomentumPredictor
└── trading/                # Order execution + monitoring
    ├── __init__.py
    ├── monitor.py          # Async monitoring loop (WS event-driven)
    └── trading.py          # buy_token() / sell_token() with FOK + GTD fallback

run.py                      # Entry point (--config YAML / --market / CLI args / interactive)
strategy.yaml.example       # Example config file
docs/polymarket_api.md      # Polymarket API reference
requirements.txt            # Python dependencies
```

## Architecture

- **`core/auth.py`**: Reads `~/.config/polymarket/config.json` for private key / signature type. Initializes `ClobClient` with `signature_type=1` (proxy/Magic wallet).
- **`core/client.py`**: Lazy `ClobClient` singleton. REST helpers: `get_midpoint()`, `get_tick_size()`, `get_token_balance(safe)`, `round_to_tick()`. Order param prefetch: `prefetch_order_params()` + `get_order_options()` to skip SDK internal API calls during order placement.
- **`core/config.py`**: Legacy constants — still used as defaults when no Strategy/Series is provided.
- **`core/log_formatter.py`**: `ConsoleFormatter` (human-readable with `[EVENT_TYPE]` prefix) and `JsonFormatter` (JSONL for frontend).
- **`core/state.py`**: `MonitorState` dataclass — tracks buy/hold state, entry price, original entry price (preserved across re-entries), SL/TP counts, deferred signals, trade lock.
- **`market/market.py`**: Slug number = Unix epoch of window start. Fetches from Gamma API. Accepts `MarketSeries` for multi-market support.
- **`market/series.py`**: `MarketSeries` frozen dataclass — defines a market series (asset, timeframe, slug params, window buffer). `KNOWN_SERIES` registry for known BTC/ETH markets.
- **`market/stream.py`**: `PriceStream` class — WebSocket connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Subscribes to token IDs, emits `PriceUpdate` via callback. Handles `PING` every 10s.
- **`trading/trading.py`**: FOK market orders with **10× retry at 100ms** (1 second total). Falls back to GTD limit at midpoint if FOK fails. Uses `PartialCreateOrderOptions` to skip SDK overhead.
- **`trading/monitor.py`**: Async event-driven loop. `PriceStream` callbacks immediately trigger buy / stop-loss / take-profit. Uses optimistic/pessimistic price aggregation (TP=max of midpoint/trade/ask, SL=min of midpoint/trade/bid). SL time gate: 5m window blocks SL until halfway, 15m until last 5min, 4h until last 1h. Re-entry price gates: SL re-entry requires price ≥ 95% of original entry (trend recovery), TP re-entry requires price ≤ entry + half gain (pullback). Window-end sell uses `trade_lock` and sets state flags to prevent double-sell. Deferred signal mechanism for race conditions. Sell with balance-refreshing retry (`_sell_with_retry`) and residual cleanup (`_cleanup_residual`, dust threshold 0.005 shares). Prefetches order params during WS pre-connect. Side resolved via `strategy.get_side(candles)` once per window.
- **`strategies/base.py`**: `Strategy` ABC with `get_side(candles) -> Optional[str]` and `should_buy(price, state) -> bool`. Direction + buy logic unified.
- **`strategies/immediate.py`**: `FixedSideStrategy(side)` — returns fixed direction, buys immediately. `ImmediateStrategy` alias for backward compat.
- **`strategies/momentum.py`**: `MomentumStrategy` — wraps `MomentumPredictor`, returns predicted direction from k-line data.
- **`trade_config.py`**: `TradeConfig` dataclass — common trading params (amount, tp_pct/sl_pct OR tp_price/sl_price, max_*_reentry, rounds). Supports both percentage and absolute price TP/SL. Absolute takes priority when both set. Progressive SL tightening on re-entry (10% per SL, floor at 5% of entry). Re-entry price criteria: SL re-entry requires price ≥ original_entry × 0.95, TP re-entry requires price ≤ original_entry × (1 + tp_pct × 0.5). Contains `check_exit()` for TP/SL logic.
- **`predict/indicators.py`**: 7 technical indicator functions — `ema`, `rsi`, `trend_direction`, `volume_trend`, `macd`, `bollinger_pctb`, `price_roc`. Pure functions, neutral returns on insufficient data.
- **`predict/momentum.py`**: `MomentumPredictor` V3 — 7-signal weighted voting (trend 20%, EMA 15%, RSI 10%, volume 5%, MACD 20%, Bollinger %B 15%, ROC 15%). Timeframe-adaptive parameters. Returns "up"/"down"/None per window.
- **`config_loader.py`**: YAML config loading, `STRATEGY_REGISTRY` (immediate/momentum), `build_series()`, `build_strategy()`, `build_trade_config()` factory functions.

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
| `rounds` | None | Number of complete windows to run (None = infinite) |

## WebSocket Protocol

- **URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- **Subscribe**: `{"assets_ids": [...], "operation": "subscribe", "custom_feature_enabled": true}`
- **Unsubscribe**: `{"assets_ids": [...], "operation": "unsubscribe"}`
- **Heartbeat**: Send `PING` every 10 seconds
- **Key events**: `best_bid_ask`, `price_change`, `last_trade_price`, `tick_size_change`, `new_market`, `market_resolved`

## API Reference

See [docs/polymarket_api.md](docs/polymarket_api.md) for complete Polymarket API documentation (CLOB, Gamma, Data APIs, order types, fill tracking, WebSocket, fees).

## Dependencies

- **Python 3.11+** — required by py-clob-client
- `py-clob-client >= 0.34.6` — Polymarket CLOB SDK
- `websockets >= 12.0` — WebSocket client
- `python-dotenv` — environment variable support
- `requests` — Gamma API for market discovery
- `pyyaml >= 6.0` — YAML config file support
- `polymarket` CLI at `~/.config/polymarket/config.json`
