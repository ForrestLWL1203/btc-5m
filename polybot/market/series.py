"""Market series definition — slug discovery, timeframe parameters.

A MarketSeries encapsulates the identity of a trading market series
(e.g., BTC 5-minute, ETH 15-minute) and provides slug construction
and timing parameters.
"""

from dataclasses import dataclass
from typing import Optional

# Seconds per window for each supported timeframe
TIMEFRAME_SECONDS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}

# Known market series. Each entry maps a series key to its discovery parameters.
# slug_step is derived from TIMEFRAME_SECONDS automatically.
KNOWN_SERIES: dict[str, dict] = {
    "btc-updown-5m": {
        "asset": "btc",
        "timeframe": "5m",
        "slug_prefix": "btc-updown-5m",
    },
    "btc-updown-15m": {
        "asset": "btc",
        "timeframe": "15m",
        "slug_prefix": "btc-updown-15m",
    },
    "btc-updown-4h": {
        "asset": "btc",
        "timeframe": "4h",
        "slug_prefix": "btc-updown-4h",
    },
    "eth-updown-5m": {
        "asset": "eth",
        "timeframe": "5m",
        "slug_prefix": "eth-updown-5m",
    },
    "eth-updown-15m": {
        "asset": "eth",
        "timeframe": "15m",
        "slug_prefix": "eth-updown-15m",
    },
    "eth-updown-4h": {
        "asset": "eth",
        "timeframe": "4h",
        "slug_prefix": "eth-updown-4h",
    },
}


def _default_buffer(slug_step: int) -> int:
    """Window-end buffer scales with timeframe: max(5, step//60) seconds.

    5m -> 5s, 15m -> 15s, 1h -> 60s, 4h -> 240s, 1d -> 1440s.
    """
    return max(5, slug_step // 60)


@dataclass(frozen=True)
class MarketSeries:
    """Immutable definition of a market series (e.g., BTC 5-minute)."""

    asset: str                # "btc" or "eth"
    timeframe: str            # "5m", "15m", "1h", "4h", "1d"
    slug_prefix: str          # e.g., "btc-updown-5m"
    slug_step: int            # seconds per window
    window_end_buffer: int    # seconds before window end to stop trading

    @property
    def series_key(self) -> str:
        return self.slug_prefix

    def epoch_to_slug(self, n: int) -> str:
        """Build a slug from an epoch number."""
        return f"{self.slug_prefix}-{n}"

    @classmethod
    def from_known(cls, key: str) -> "MarketSeries":
        """Build from KNOWN_SERIES lookup."""
        info = KNOWN_SERIES[key]
        step = TIMEFRAME_SECONDS[info["timeframe"]]
        return cls(
            asset=info["asset"],
            timeframe=info["timeframe"],
            slug_prefix=info["slug_prefix"],
            slug_step=step,
            window_end_buffer=_default_buffer(step),
        )
