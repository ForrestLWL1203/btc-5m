"""Strategy protocol — abstract interface for buy decisions.

A Strategy encapsulates only the "when to buy" logic.
All other trading parameters (amount, TP/SL, re-entry, rounds) live in TradeConfig.

To add a new strategy:
  1. Create polybot/strategies/your_strategy.py
  2. Implement the Strategy ABC (just should_buy)
  3. Register it in polybot/config_loader.py STRATEGY_REGISTRY
"""

from abc import ABC, abstractmethod

from polybot.core.state import MonitorState


class Strategy(ABC):
    """Abstract trading strategy — only decides whether to buy."""

    @abstractmethod
    def should_buy(self, price: float, state: MonitorState) -> bool:
        """Return True if the strategy would enter a position at this price."""
        ...
