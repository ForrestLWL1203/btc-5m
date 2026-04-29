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

    async def start(self) -> None:
        """Optional strategy startup hook."""
        return None

    async def stop(self) -> None:
        """Optional strategy shutdown hook."""
        return None

    def set_window_start(self, epoch: float) -> None:
        """Optional per-window initialization hook."""
        return None

    async def preload_open_btc(self, epoch: float) -> None:
        """Optional hook for seeding a BTC window-open reference."""
        return None

    def set_market_snapshot(
        self,
        *,
        up_mid: Optional[float],
        down_mid: Optional[float],
        up_best_ask: Optional[float] = None,
        down_best_ask: Optional[float] = None,
        up_best_ask_age_sec: Optional[float] = None,
        down_best_ask_age_sec: Optional[float] = None,
    ) -> None:
        """Optional hook for strategies that consume two-leg Polymarket snapshots."""
        return None

    def on_buy_confirmed(self, timestamp: float) -> None:
        """Optional hook fired after an entry fill is confirmed."""
        return None
