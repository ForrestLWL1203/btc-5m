"""MonitorState — mutable state shared between callbacks and the monitoring loop.

Extracted from monitor.py to avoid circular imports between strategies and monitor.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class MonitorState:
    """Mutable state shared between callbacks and the main loop."""

    bought: bool = False
    holding_size: float = 0.0  # shares held
    entry_price: float = 0.0
    exit_triggered: bool = False
    entry_count: int = 0  # total successful entries this window
    entry_timestamps: list[float] = field(default_factory=list)  # confirmed entry times (epoch seconds)
    latest_midpoint: Optional[float] = None
    realized_pnl: float = 0.0  # cumulative dry-run realized PnL for this window
    buy_blocked_window_cap: bool = False  # blocked because max_entries_per_window was reached
    target_side: Optional[str] = None  # optional strategy-specific side override ("up"/"down")
    target_entry_price: Optional[float] = None  # strategy-computed token price to use for fills/logging
    trade_lock: asyncio.Lock = None  # prevents concurrent buy/sell from WS callbacks
    started: bool = False  # set True when window officially starts — prevents pre-start trades

    def __post_init__(self):
        self.trade_lock = asyncio.Lock()
