"""Strategy protocol for the active paired-window strategy."""

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
