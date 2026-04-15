# Polymarket Official Documentation Study Notes

> Source: https://docs.polymarket.com/
> Date: 2026-04-15
> Status: All accessible pages read; some pages returned 403/404 (noted at bottom)

---

## 1. Core Concepts

### 1.1 Markets & Events
- **Event**: A real-world occurrence with multiple possible outcomes
- **Market**: A specific outcome within an event — each market has a Yes/No token pair
- **Slug**: URL-friendly identifier for events (e.g., `btc-updown-5m-{N}`)
- **Condition ID**: Unique onchain identifier for a market's condition
- Markets are resolved to Yes or No by UMA Optimistic Oracle

### 1.2 Prices & Orderbook
- Prices range from 0.00 to 1.00 (representing probability in cents)
- **Tick sizes**: 0.1, 0.01, 0.001, 0.0001 — orders MUST conform to the market's tick size or they are rejected
- **Orderbook**: Standard bid/ask structure
- **Midpoint price** = (best_bid + best_ask) / 2
- Actual execution happens at bid or ask price, not midpoint

### 1.3 Positions & Tokens
- Tokens are ERC1155 (Gnosis CTF standard) on Polygon
- Each market has a **Yes token** and **No token**
- Buying Yes at 60¢ means you believe the outcome has >60% probability
- If resolved Yes: Yes token = $1.00, No token = $0.00
- If resolved No: No token = $1.00, Yes token = $0.00

### 1.4 Order Lifecycle
1. User signs order offchain (EIP-712 signature)
2. Order submitted to CLOB operator
3. Operator matches orders (offchain)
4. Matched orders settled onchain via Exchange contract
5. Statuses: `LIVE` → `MATCHED` → `MINED` → `CONFIRMED`
   - Or: `RETRYING` → `FAILED` (if onchain settlement fails)
- Orders can also be cancelled by user before matching

### 1.5 Resolution
- Uses **UMA Optimistic Oracle**
- Proposer posts $750 bond
- 2-hour challenge window
- If no challenge: proposal accepted
- If challenged: UMA DVM (Data Verification Mechanism) resolves via token holder vote
- Resolution source specified per market (e.g., Chainlink BTC/USD for BTC markets)

---

## 2. Trading

### 2.1 Order Types
| Type | Code | Description |
|---|---|---|
| Good-Til-Cancelled | GTC | Limit order, stays open until filled or cancelled |
| Good-Til-Date | GTD | Limit order with auto-expiry timestamp — no manual cancel needed |
| Fill-Or-Kill | FOK | Must fill entire amount immediately, or cancel entirely |
| Fill-And-Kill | FAK | Partial fill allowed, unfilled portion cancelled |
| Post-Only | — | Guaranteed maker placement, zero taker fees + earns rebates |

**Key details**:
- FOK is the most aggressive — instant full fill or nothing
- FAK is more flexible — allows partial fills
- Post-Only ensures you're always a maker (placed on orderbook, never crosses existing orders)
- GTD is ideal for time-bounded strategies — auto-cancels at specified timestamp
- Batch orders: up to 15 orders per single API request

### 2.2 Creating Orders
- All trading orders require **L2 authentication** (HMAC-SHA256)
- L1 auth (EIP-712 signature from private key) used only for deriving API credentials
- Signature types: `EOA(0)`, `POLY_PROXY(1)` (Magic Link), `GNOSIS_SAFE(2)` (MetaMask)
- Order payload includes: token_id, price, size, side, type, tick_size
- Price must be rounded to market's tick size

### 2.3 Fee Structure
- **Formula**: `fee = C × feeRate × p × (1 - p)`
  - `C` = collateral amount (in dollars)
  - `feeRate` = category-specific taker rate (crypto = 0.072)
  - `p` = execution price (0 to 1)
- Fee peaks at p = 0.50 (50¢ probability): `fee = C × 0.072 × 0.25`
- **Maker never charged fees** — earns rebates instead
  - Crypto category rebate: 20% of taker fee
- Fee deducted from order's collateral at time of placement

### 2.4 Heartbeat (Critical)
- Must send a **heartbeat within 10 seconds** (5s safety buffer recommended)
- Failure to send heartbeat: **ALL open orders auto-cancelled**
- Heartbeat is sent via the API (not WebSocket) — it's a dedicated heartbeat endpoint

---

## 3. CTF Tokens (Conditional Token Framework)

### 3.1 Overview
- Based on Gnosis Conditional Token Framework
- ERC1155 tokens on Polygon
- Two contract types:
  - **CTF Exchange**: Standard markets (binary Yes/No)
  - **Neg Risk CTF Exchange**: Multi-outcome events (3+ outcomes)

### 3.2 Core Operations
- **Split**: Convert $1 USDC → 1 Yes token + 1 No token
- **Merge**: Convert 1 Yes token + 1 No token → $1 USDC
- **Redeem**: After resolution, exchange winning tokens → $1 each
- **Conversion (Neg Risk only)**: Exchange No token for Yes tokens in other outcomes

### 3.3 Token ID Computation
1. Compute **condition ID** from market parameters
2. Compute **collection ID** from condition ID + index set
3. Compute **position ID** = hash(collection ID, collateral token address)
- These are deterministic — same inputs always produce same token IDs

---

## 4. WebSocket Protocol

### 4.1 Overview
- **URL**: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- No authentication required for Market channel
- Authentication required for User channel
- Channels: Market, User, Sports, RTDS

### 4.2 Market Channel — Connection & Subscription

**Connect**: Open WebSocket to the URL above

**Subscribe** (initial):
```json
{
  "type": "market",
  "assets_ids": ["token_id_1", "token_id_2"],
  "operation": "subscribe"
}
```

**Dynamic subscribe/unsubscribe**:
```json
{
  "assets_ids": ["token_id"],
  "operation": "subscribe"  // or "unsubscribe"
}
```

**Ping (keepalive)**:
```json
{}
```
> **IMPORTANT**: The official API reference specifies `{}` (empty JSON object) as the ping message, NOT the string `"PING"`. This differs from some community examples.

### 4.3 Market Channel — Event Types

| Event | Description | Custom Feature? |
|---|---|---|
| `book` | Full orderbook snapshot (bids + asks arrays) | No |
| `price_change` | Incremental trade update | No |
| `tick_size_change` | Tick size updated for market | No |
| `last_trade_price` | Last executed trade price | No |
| `best_bid_ask` | Best bid/ask/spread update | Yes |
| `new_market` | New market created | Yes |
| `market_resolved` | Market resolved | Yes |

Events marked "Yes" for custom feature require `"custom_feature_enabled": true` in the subscribe message.

### 4.4 Event Payload Details

**`best_bid_ask`**:
```json
{
  "event_type": "best_bid_ask",
  "best_bid": "0.50",
  "best_ask": "0.55",
  "spread": "0.05",
  "asset_id": "token_id",
  "market": "market_id",
  "timestamp": "2024-01-01T00:00:00Z"
}
```

**`price_change`**:
```json
{
  "event_type": "price_change",
  "price_changes": [
    {
      "asset_id": "token_id",
      "price": "0.52",
      "size": "100.0",
      "side": "BUY",
      "hash": "...",
      "best_bid": "0.50",
      "best_ask": "0.55"
    }
  ]
}
```
> Note: `price_change` is an array — a single event can contain multiple price changes.

**`last_trade_price`**:
```json
{
  "event_type": "last_trade_price",
  "asset_id": "token_id",
  "price": "0.53",
  "size": "50.0",
  "fee_rate_bps": 72,
  "side": "BUY",
  "timestamp": "2024-01-01T00:00:00Z",
  "transaction_hash": "0x..."
}
```

**`book`**:
```json
{
  "event_type": "book",
  "asset_id": "token_id",
  "market": "market_id",
  "bids": [{"price": "0.50", "size": "100"}],
  "asks": [{"price": "0.55", "size": "200"}],
  "timestamp": "2024-01-01T00:00:00Z"
}
```

### 4.5 Price Derivation
- **Midpoint** = (best_bid + best_ask) / 2
- For `best_bid_ask` events: use directly from `best_bid` / `best_ask` fields
- For `price_change` events: also includes `best_bid` / `best_ask` — can derive midpoint
- `last_trade_price`: actual trade execution price — may differ from midpoint
- **Important**: Midpoint may lag behind last trade price during rapid moves

---

## 5. Matching Engine Restarts

- **Schedule**: Weekly, Tuesdays at 7:00 AM ET
- **Duration**: ~90 seconds downtime
- **API behavior**: Returns HTTP 425 (Too Early) during restart
- **Recommended retry strategy**: Exponential backoff, starting 1-2 seconds, max 30 seconds
- **Notifications**: Announced via Telegram (Polymarket Trading APIs) and Discord (#trading-apis)
- **Impact**: All order operations unavailable during restart; WebSocket may disconnect

---

## 6. Negative Risk Markets

- Used for multi-outcome events (3+ outcomes)
- Different exchange contract: **Neg Risk CTF Exchange** (vs standard CTF Exchange)
- Must pass `negRisk: true` flag when creating orders
- Additional "conversion" operation available
- BTC Up/Down markets are binary (2 outcomes) — may NOT need negRisk flag
- Full documentation page inaccessible (403 error)

---

## 7. Key Discrepancies & Notes for Code Review

### 7.1 WebSocket Ping Format
- **Official docs**: `{}` (empty JSON object)
- **CLAUDE.md / bot code**: Sends `"PING"` string
- **Action**: Verify which format actually works; `{}` is the documented spec

### 7.2 Heartbeat vs Ping
- **API Heartbeat** (order-critical): Must be sent within 10 seconds via API endpoint. Failure cancels ALL open orders.
- **WebSocket Ping** (connection keepalive): Keeps WS connection alive; different from API heartbeat.
- **Action**: Check if bot implements API heartbeat for GTC orders; WebSocket ping alone is insufficient for maintaining GTC orders.

### 7.3 price_change Event Structure
- Official docs show `price_change` as an **array** of price changes in a single event
- Bot code should iterate over the array, not assume a single object
- **Action**: Verify handling in `stream.py`

### 7.4 last_trade_price for Stop-Loss/Take-Profit
- Known issue: midpoint lags behind actual trade price
- Using `last_trade_price` events directly may be more accurate for SL/TP decisions
- **Action**: Review if bot uses or should use `last_trade_price` for trigger logic

### 7.5 Custom Feature Flag
- `best_bid_ask` event requires `"custom_feature_enabled": true` in subscribe message
- Bot's subscribe message should include this flag
- **Action**: Verify subscribe message format in `stream.py`

---

## 8. Pages Not Fetched (403/404 Errors)

| URL Attempted | Error |
|---|---|
| `/trading/negative-risk-markets` | 403 Forbidden |
| `/trading/negative-risk` | 404 Not Found |
| `/trading/gasless-transactions` | 404 Not Found |
| `/trading/client-reference` | 403 Forbidden |
| `/trading/sor` (Smart Order Routing) | Not attempted |
| `/trading/rewards` | Not attempted |
| `/api-reference/*` (REST endpoints) | Not attempted |

These pages may contain additional important details. Consider retrying with alternative access methods or checking if URLs have changed.

---

## 9. Summary of API Endpoints Used by Bot

| Purpose | Method | Library |
|---|---|---|
| Market discovery | REST (Gamma API) | `requests` |
| Get midpoint | REST | `py-clob-client` |
| Get tick size | REST | `py-clob-client` |
| Create FOK order | REST | `py-clob-client` |
| Create GTC order | REST | `py-clob-client` |
| Cancel order | REST | `py-clob-client` |
| Real-time prices | WebSocket | `websockets` |
| Auth (L1) | REST | `py-clob-client` |
| Auth (L2) | REST | `py-clob-client` |
