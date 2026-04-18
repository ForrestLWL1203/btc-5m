"""WindowHistory — ring buffer for cross-window price data with Gamma API backfill."""

import json
import logging
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import List, Optional

import requests

log = logging.getLogger(__name__)

HISTORY_CAPACITY = {
    "5m": 100,
    "15m": 30,
    "1h": 24,
    "4h": 6,
    "1d": 7,
}


@dataclass
class WindowRecord:
    """Price data for a single trading window."""

    window_start: int
    up_price_open: float = 0.0
    up_price_close: float = 0.0
    down_price_open: float = 0.0
    down_price_close: float = 0.0
    up_volume: float = 0.0
    down_volume: float = 0.0
    resolved_side: Optional[str] = None


_GAMMA_API = "https://gamma-api.polymarket.com/markets"


def _fetch_market_for_backfill(slug: str) -> Optional[dict]:
    """Fetch a single market by slug for backfill. Returns raw dict or None."""
    try:
        resp = requests.get(_GAMMA_API, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception:
        return None


def _parse_backfill_record(m: dict, slug: str) -> Optional[WindowRecord]:
    """Parse a Gamma API market dict into a WindowRecord."""
    try:
        prices_raw = m.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw)
        if len(prices) < 2:
            return None

        up_close = float(prices[0])
        down_close = float(prices[1])

        # Extract epoch from slug: "btc-updown-5m-1713300000"
        parts = slug.rsplit("-", 1)
        window_start = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

        resolved = None
        if up_close > down_close:
            resolved = "up"
        elif down_close > up_close:
            resolved = "down"

        return WindowRecord(
            window_start=window_start,
            up_price_open=0.0,
            up_price_close=up_close,
            down_price_open=0.0,
            down_price_close=down_close,
            up_volume=float(m.get("volume", 0)),
            down_volume=0.0,
            resolved_side=resolved,
        )
    except (ValueError, TypeError, IndexError):
        return None


class WindowHistory:
    """Ring buffer of WindowRecords, ordered oldest→newest."""

    def __init__(self, capacity: int):
        self._buf: deque[WindowRecord] = deque(maxlen=capacity)

    @classmethod
    def for_timeframe(cls, timeframe: str) -> "WindowHistory":
        cap = HISTORY_CAPACITY.get(timeframe, 100)
        return cls(capacity=cap)

    def record(self, rec: WindowRecord) -> None:
        self._buf.append(rec)

    def latest(self) -> Optional[WindowRecord]:
        return self._buf[-1] if self._buf else None

    def last_n(self, n: int) -> List[WindowRecord]:
        """Return the most recent N records, ordered oldest→newest."""
        if n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    @property
    def records(self) -> List[WindowRecord]:
        return list(self._buf)

    def __len__(self) -> int:
        return len(self._buf)

    def backfill(self, slug_prefix: str, slug_step: int, count: int, current_epoch: int) -> None:
        """Fetch past N windows from Gamma API and populate history."""
        from polybot.predict.history import _fetch_market_for_backfill, _parse_backfill_record

        slugs = []
        for i in range(1, count + 1):
            epoch = current_epoch - i * slug_step
            slugs.append(f"{slug_prefix}-{epoch}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_fetch_market_for_backfill, s): s for s in slugs}
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    m = future.result()
                    if m is None:
                        continue
                    rec = _parse_backfill_record(m, slug)
                    if rec is not None:
                        self.record(rec)
                except Exception:
                    continue
