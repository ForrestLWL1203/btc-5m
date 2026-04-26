"""Strategy protocol — abstract interface for buy + direction decisions.

A Strategy encapsulates both "which side to buy" and "when to buy" logic.
Common execution parameters (amount, per-window cap, rounds) live in TradeConfig.

To add a new strategy:
  1. Create polybot/strategies/your_strategy.py
  2. Implement get_side() and should_buy()
  3. Register it in polybot/config_loader.py STRATEGY_REGISTRY
"""

from abc import ABC, abstractmethod
from typing import Optional

from polybot.core.state import MonitorState


class Strategy(ABC):
    """Abstract trading strategy — decides direction and whether to buy."""

    @abstractmethod
    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        """Return 'up', 'down', or None (skip window). Called once per window."""
        ...

    @abstractmethod
    def should_buy(self, price: float, state: MonitorState) -> bool:
        """Return True if the strategy would enter a position at this price."""
        ...
