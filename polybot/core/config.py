"""Configuration constants for market discovery and execution plumbing."""

# Market slug discovery
SLUG_STEP: int = 300  # slug numbers increment by 300 seconds (5 minutes)
# CLOB API
CLOB_HOST: str = "https://clob.polymarket.com"
CHAIN_ID: int = 137  # Polygon mainnet
SIGNATURE_TYPE: int = 1  # 1 = proxy/Magic wallet (matches polymarket CLI config)

# Polling / retry
WS_RECONNECT_DELAY: float = 1.0  # initial WS reconnect backoff (seconds)
WS_RECONNECT_MAX_DELAY: float = 30.0  # max WS reconnect backoff (seconds)
WS_RECONNECT_MAX_RETRIES: int = 10  # max consecutive WS reconnect attempts
FAK_RETRY_COUNT: int = 3  # FAK retry attempts
FAK_RETRY_INTERVAL: float = 0.1  # seconds between retries (~10x per second)
PRICE_HINT_BUFFER_TICKS: float = 1.0  # add one tick above best ask for BUY hints
FAK_RETRY_PRICE_HINT_BUFFER_TICKS: float = 2.0  # slightly wider retry hint after FAK no-depth
FAK_RETRY_MAX_BEST_ASK_AGE_SEC: float = 1.0  # require fresh WS ask before retrying FAK

# Window timing
WINDOW_END_BUFFER: int = 5  # treat window as ending this many seconds early to avoid boundary issues

# Slug name
SERIES_SLUG_PREFIX: str = "btc-updown-5m"

# How many historical slug candidates to check
SLUG_CANDIDATE_STEP: int = SLUG_STEP
