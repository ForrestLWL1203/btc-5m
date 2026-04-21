"""Configuration constants for market discovery and execution plumbing."""

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

# Slug name
SERIES_SLUG_PREFIX: str = "btc-updown-5m"

# How many historical slug candidates to check
SLUG_CANDIDATE_STEP: int = SLUG_STEP
