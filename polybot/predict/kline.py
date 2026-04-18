"""Binance K-line data fetcher for BTC/ETH price history."""

import logging
from dataclasses import dataclass
from typing import List

import requests

from polybot.market.series import MarketSeries

log = logging.getLogger(__name__)

_BINANCE_API = "https://api.binance.com/api/v3/klines"

# (interval, limit) per window timeframe
_TIMEFRAME_MAP = {
    "5m": ("1m", 60),
    "15m": ("5m", 48),
    "4h": ("1h", 24),
}


@dataclass
class KlineCandle:
    """Single OHLCV candle from Binance."""

    open_time: int   # epoch ms
    open: float
    high: float
    low: float
    close: float
    volume: float


class BinanceKlineFetcher:
    """Fetches K-line data from Binance for a given market series."""

    def __init__(self, series: MarketSeries):
        self.symbol = "BTCUSDT" if series.asset == "btc" else "ETHUSDT"
        interval, limit = _TIMEFRAME_MAP.get(series.timeframe, ("1m", 60))
        self.interval = interval
        self.limit = limit

    def fetch(self) -> List[KlineCandle]:
        """Fetch K-line candles from Binance. Returns empty list on failure."""
        try:
            resp = requests.get(
                _BINANCE_API,
                params={
                    "symbol": self.symbol,
                    "interval": self.interval,
                    "limit": self.limit,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []
            return [
                KlineCandle(
                    open_time=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                for row in data
            ]
        except Exception as e:
            log.warning("Binance K-line fetch failed: %s", e)
            return []
