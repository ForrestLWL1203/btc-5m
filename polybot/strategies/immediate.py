"""ImmediateStrategy — buy immediately at window open price.

The simplest strategy: always buy at whatever price is available.
All trading parameters (TP/SL, amount, re-entry) are in TradeConfig.
"""

from .base import Strategy
from polybot.core.state import MonitorState


class ImmediateStrategy(Strategy):
    """Buy at any price — no range check, no filtering."""

    def should_buy(self, price: float, state: MonitorState) -> bool:
        """Always return True — buy at whatever price is available."""
        return True
