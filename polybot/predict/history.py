"""WindowHistory — ring buffer for cross-window price data with Gamma API backfill."""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

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
