"""Market series definition for the active BTC 5-minute market."""

from dataclasses import dataclass
ACTIVE_SERIES_KEY = "btc-updown-5m"
ACTIVE_WINDOW_SECONDS = 300
ACTIVE_WINDOW_END_BUFFER = 5

KNOWN_SERIES: dict[str, dict] = {
    ACTIVE_SERIES_KEY: {
        "asset": "btc",
        "timeframe": "5m",
        "slug_prefix": ACTIVE_SERIES_KEY,
        "slug_step": ACTIVE_WINDOW_SECONDS,
        "window_end_buffer": ACTIVE_WINDOW_END_BUFFER,
    },
}


@dataclass(frozen=True)
class MarketSeries:
    """Immutable definition of the active market series."""

    asset: str                # "btc"
    timeframe: str            # "5m"
    slug_prefix: str          # "btc-updown-5m"
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
        return cls(
            asset=info["asset"],
            timeframe=info["timeframe"],
            slug_prefix=info["slug_prefix"],
            slug_step=info["slug_step"],
            window_end_buffer=info["window_end_buffer"],
        )
