"""Configuration constants — mirrors btc5m_trade.sh top-level parameters."""

# Trading direction: "up" or "down"
BUY_SIDE: str = "up"  # which side to buy ("up" or "down")

# Trading parameters
BUY_AMOUNT: float = 5.0  # dollars per trade
BUY_THRESHOLD_LOW: float = 0.45  # minimum price to place buy order
BUY_THRESHOLD_HIGH: float = 0.55  # maximum price to place buy order
STOP_LOSS: float = 0.30  # sell if Up price drops below this (for UP side) / DOWN price drops below this (for DOWN side)
TAKE_PROFIT: float = 0.80  # sell if price rises above this

# Market slug discovery
SLUG_STEP: int = 300  # slug numbers increment by 300 seconds (5 minutes)
# CLOB API
CLOB_HOST: str = "https://clob.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet
SIGNATURE_TYPE: int = 1  # 1 = proxy/Magic wallet (matches polymarket CLI config)

# Polling / retry
API_HEARTBEAT_INTERVAL: float = 8.0  # seconds between API heartbeat calls (must be < 10s)
WS_RECONNECT_DELAY: float = 1.0  # initial WS reconnect backoff (seconds)
WS_RECONNECT_MAX_DELAY: float = 30.0  # max WS reconnect backoff (seconds)
WS_RECONNECT_MAX_RETRIES: int = 10  # max consecutive WS reconnect attempts
FOK_RETRY_COUNT: int = 10  # retry FOK orders this many times
FOK_RETRY_INTERVAL: float = 0.1  # seconds between retries (~10x per second)
FALLBACK_GTC: bool = True  # fallback to GTC limit order if FOK fails

# Window timing
WINDOW_END_BUFFER: int = 5  # treat window as ending this many seconds early to avoid boundary issues

# Signal freshness
TRADE_PRICE_TTL: float = 3.0  # seconds before last_trade_price is considered stale for SL/TP

# Re-entry limits per window (0 = most conservative, only one buy)
# STOP_LOSS: allow up to N re-buys after stop-loss exits
# TAKE_PROFIT: allow up to N re-buys after take-profit exits
MAX_STOP_LOSS_REENTRY: int = 0   # 0 = no re-entry after stop-loss
MAX_TP_REENTRY: int = 0          # 0 = no re-entry after take-profit

# Slug name
SERIES_SLUG_PREFIX: str = "btc-updown-5m"

# How many historical slug candidates to check
SLUG_CANDIDATE_STEP: int = SLUG_STEP
