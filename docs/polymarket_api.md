# Polymarket API Reference

Studied 2026-05-01 from https://docs.polymarket.com

## Three API Systems

| API | Base URL | Purpose |
|---|---|---|
| **CLOB** | `https://clob.polymarket.com` | Trading: orders, orderbook, positions |
| **Gamma** | `https://gamma-api.polymarket.com` | Markets/events metadata, slug discovery |
| **Data** | `https://data-api.polymarket.com` | Positions, activity history, leaderboard |

## Authentication (CLOB)

- **L1**: Derive API key/secret from private key via EIP-712 `derive_api_key()` / `create_or_derive_api_key()`
- **L2**: HMAC-SHA256 signature on every trading request (handled by `py-clob-client-v2`)
- Signature types: EOA(0), POLY_PROXY(1), GNOSIS_SAFE(2)
- polybot uses type=1 (Magic Link proxy wallet)

## Orders — CLOB API

### POST /order — Place order

- Request body: `tokenID, price, size, side, type, expiration` + optional `swap_fee`/`swap_market`
- **Response**: ONLY `orderID, status, success, errorMsg` — **NO sizeFilled/avgPrice**
- Statuses: `matched` (filled), `live` (GTC/GTD resting), `delayed`, `unmatched`
- **Critical**: Must query fills separately to get execution details

### GET /order/{orderID} — Order status

- Returns full order object including `size_matched` field
- Use this to check fill status after placing orders

### DELETE /order/{orderID} — Cancel single order

### DELETE /orders — Cancel all open orders (body: `{market: conditionID}`)

## Order Types

| Type | Behavior | Notes |
|---|---|---|
| **FAK** | Fill-and-Kill, take available liquidity then cancel remainder | Current polybot runtime order type |
| **FOK** | Fill-or-Kill, entire fill or nothing | Not used by current runtime |
| **GTC** | Good-Til-Cancelled | Requires heartbeat within 10s or ALL orders cancelled |
| **GTD** | Good-Til-Date, auto-expires at timestamp | No heartbeat needed, preferred for limit fallback |
| **Post-Only** | Guaranteed maker placement | Zero fees + rebates, rejected if would take |

## Fill Tracking

POST /order response lacks fill data. Options:

1. **GET /trades** — Query Trade objects: `{size, price, side, market, asset, timestamp}`
2. **GET /order/{orderID}** — Check `size_matched` on the order
3. **GET /balance-allowance** — Query current token balance (most reliable for sell sizing)

## Balance & Allowance

### GET /balance-allowance

- Params: `asset_type=CONDITIONAL, token_id=xxx`
- Response: `{balance: "1724000", allowance: "..."}` — balance is 6-decimal integer
- **Convert**: `float(balance) / 1_000_000` = actual shares
- **No "sell all" API exists** — must query balance then sell exact amount

## CLOB V2 Order Signing

- Runtime dependency is `py-clob-client-v2==1.0.0`.
- V2 signed orders include `timestamp`, `metadata`, and `builder`.
- V1 signed-order fields `nonce`, `feeRateBps`, and signed `taker` are not used.
- CLOB production remains `https://clob.polymarket.com`; V2 is selected by the SDK/order signature.
- Runtime FAK calls build `MarketOrderArgs(..., order_type=OrderType.FAK)` and then `post_order(..., OrderType.FAK)`.

## MarketOrderArgs (`py-clob-client-v2`)

- **BUY**: `amount` = dollars to spend (NOT shares). Shares = amount / price
- **SELL**: `amount` = shares to sell directly
- This asymmetry is critical for correct order sizing

## Positions — Data API

### GET /positions — Current positions

- Params: `user=address, sizeThreshold=0.5, limit=100`
- Returns: `{market, conditionId, asset, size, avgPrice, pnl, curPrice}`

## Market Discovery — Gamma API

### GET /events — Event metadata

- Filter by `slug` (e.g., `btc-updown-5m-1713300000`)
- Slug number = Unix epoch of window start time

### GET /markets — Market details

- Returns `conditionId, tokens[{tokenID, outcome}]`
- `tokens[0]` = Up/Yes, `tokens[1]` = Down/No

## WebSocket — Real-Time

**URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`

Subscribe: `{"assets_ids": [...], "operation": "subscribe"}`
Unsubscribe: `{"assets_ids": [...], "operation": "unsubscribe"}`

Events:

- `best_bid_ask` — best bid/ask → midpoint = (bid+ask)/2
- `last_trade_price` — actual trade execution price
- `book` — full book snapshot used to seed local depth
- `price_change` — incremental book update
- `tick_size_change` — tick size update

Heartbeat: send `PING` (or `{}` empty JSON) every 10s

## Tick Sizes

Tick size determines minimum price increment. Varies by price range:

- Low prices: 0.01 (1¢)
- High prices: 0.001 (0.1¢)
- Always use `get_tick_size(tokenID)` then `round_to_tick()`

## Fees

- Fees are determined by the protocol / CLOB market info at match time.
- The bot does not set `feeRateBps` on orders.
- Maker: **zero fees, earns rebates** where applicable.

## Geoblock

- Taiwan: close-only (can sell existing positions, cannot open new)
- Hong Kong: allowed
- US: blocked

## Current API Strategy for polybot

1. **Signal**: BTC window-open move decides direction; Polymarket UP stream is
   only a signal reference.
2. **Entry permission**: Use target-leg WS order-book depth from ask level 1
   and require enough cap-limited notional.
3. **Buy**: FAK market order with a book-depth price hint clamped to the hard
   cap.
4. **Retry**: Refresh WS book depth before retry; abort if stale or insufficient.
5. **Optional stop loss**: When enabled, use held-leg bid-book depth and SELL
   FAK inside the configured late-window time band.
6. **Default exit**: Hold to window end and let resolution / auto-redeem settle.
7. **Fill accounting**: Prefer `FAK_FILLED.avg_price` from execution result;
   balance queries remain useful for reconciliation.
