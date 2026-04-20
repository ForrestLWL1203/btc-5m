"""MonitorState — mutable state shared between callbacks and the monitoring loop.

Extracted from monitor.py to avoid circular imports between strategies and monitor.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from . import config
from polybot.market.stream import PriceUpdate


@dataclass
class MonitorState:
    """Mutable state shared between callbacks and the main loop."""

    bought: bool = False
    holding_size: float = 0.0  # shares held
    entry_price: float = 0.0
    original_entry_price: float = 0.0  # first buy price, preserved across re-entries
    exit_triggered: bool = False
    entry_count: int = 0  # total successful entries this window
    entry_timestamps: list[float] = field(default_factory=list)  # confirmed entry times (epoch seconds)
    tp_count: int = 0      # take-profit exits this window
    edge_exit_count: int = 0  # edge-based fast exits this window
    stop_loss_count: int = 0  # stop-loss exits this window
    latest_midpoint: Optional[float] = None
    realized_pnl: float = 0.0  # cumulative dry-run realized PnL for this window
    buy_blocked_sl: bool = False  # permanently blocked for this window due to stop-loss count exceeded
    buy_blocked_tp: bool = False  # permanently blocked for this window due to take-profit count exceeded
    buy_blocked_window_cap: bool = False  # blocked because max_entries_per_window was reached
    target_side: Optional[str] = None  # direction override from LatencyArbStrategy ("up"/"down")
    trade_lock: asyncio.Lock = None  # prevents concurrent buy/sell from WS callbacks
    started: bool = False  # set True when window officially starts — prevents pre-start trades

    # Deferred SL/TP signal storage (populated while trade_lock is held)
    _pending_signal: Optional[PriceUpdate] = None

    # Last trade price tracking for more responsive SL/TP
    _last_trade_price: Optional[float] = None
    _last_trade_time: float = 0.0  # monotonic timestamp

    def __post_init__(self):
        self.trade_lock = asyncio.Lock()

    def update_trade_price(self, price: float) -> None:
        """Store latest trade execution price with monotonic timestamp."""
        self._last_trade_price = price
        self._last_trade_time = time.monotonic()

    def get_fresh_trade_price(self, ttl: float = config.TRADE_PRICE_TTL) -> Optional[float]:
        """Return trade price if within TTL, else None."""
        if self._last_trade_price is None:
            return None
        if time.monotonic() - self._last_trade_time > ttl:
            return None
        return self._last_trade_price
