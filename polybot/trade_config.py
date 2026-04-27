"""TradeConfig — runtime parameters shared by the active paired-window strategy."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeConfig:
    """Execution controls for the current runtime strategy."""

    amount: float = 5.0
    entry_ask_level: int = 1
    low_price_threshold: Optional[float] = None
    low_price_entry_ask_level: Optional[int] = None
    max_entries_per_window: Optional[int] = None
    rounds: Optional[int] = None  # None = infinite
    amount_tiers: list[tuple[float, float]] = field(default_factory=list)
    consecutive_loss_amount_limit: Optional[float] = None
    daily_loss_amount_limit: Optional[float] = None
    consecutive_loss_pause_windows: int = 2
    daily_loss_pause_windows: int = 5
    stop_loss_enabled: bool = False
    stop_loss_multiplier: float = 1.2
    stop_loss_trigger_price: float = 0.35
    stop_loss_disable_below_entry_price: float = 0.45
    stop_loss_start_remaining_sec: float = 120.0
    stop_loss_end_remaining_sec: float = 15.0
    stop_loss_sell_bid_level: int = 20
    stop_loss_retry_count: int = 3
    stop_loss_min_sell_price: float = 0.20

    def amount_for_signal_strength(self, signal_strength: Optional[float]) -> float:
        """Return configured stake size for signal strength."""
        if signal_strength is None:
            return self.amount
        selected = self.amount
        for threshold, tier_amount in self.amount_tiers:
            if signal_strength >= threshold:
                selected = max(selected, tier_amount)
        return selected

    def base_entry_ask_level(self) -> int:
        """Return the deepest ask-book level allowed for FAK hints."""
        return max(1, int(self.entry_ask_level))

    def stop_loss_bid_level(self) -> int:
        """Return the configured bid-book level for stop-loss SELL hints."""
        return max(1, int(self.stop_loss_sell_bid_level))
