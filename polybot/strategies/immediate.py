"""FixedSideStrategy — buy immediately at window open price with a fixed side.

The simplest strategy: always buy at whatever price is available, using a
user-specified side (up/down). All trading parameters in TradeConfig.
"""

from typing import Optional

from .base import Strategy
from polybot.core.state import MonitorState


class FixedSideStrategy(Strategy):
    """Buy at any price with a fixed side — no range check, no filtering."""

    def __init__(self, side: str = "up"):
        self.side = side

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        return self.side

    def should_buy(self, price: float, state: MonitorState) -> bool:
        return True


# Backward compat
ImmediateStrategy = FixedSideStrategy
