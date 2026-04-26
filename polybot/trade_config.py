"""TradeConfig — runtime parameters shared by the active paired-window strategy."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeConfig:
    """Execution controls for the current runtime strategy."""

    amount: float = 5.0
    entry_ask_level: int = 1
    ask_level_tiers: list[tuple[float, int]] = field(default_factory=list)
    max_entries_per_window: Optional[int] = None
    rounds: Optional[int] = None  # None = infinite
    amount_tiers: list[tuple[float, float]] = field(default_factory=list)
    consecutive_loss_amount_limit: Optional[float] = None
    daily_loss_amount_limit: Optional[float] = None
    consecutive_loss_pause_windows: int = 2
    daily_loss_pause_windows: int = 5
    normal_full_cap_guard_enabled: bool = False
    normal_full_cap_min_signal_strength: Optional[float] = None
    normal_full_cap_min_remaining_sec: Optional[float] = None
    normal_full_cap_price_tolerance: float = 1e-9

    def amount_for_signal_strength(self, signal_strength: Optional[float]) -> float:
        """Return configured stake size for signal strength."""
        if signal_strength is None:
            return self.amount
        selected = self.amount
        for threshold, tier_amount in self.amount_tiers:
            if signal_strength >= threshold:
                selected = max(selected, tier_amount)
        return selected

    def ask_level_for_signal_strength(self, signal_strength: Optional[float]) -> int:
        """Return configured ask-book level for signal strength."""
        selected = max(1, int(self.entry_ask_level))
        if signal_strength is None:
            return selected
        for threshold, level in self.ask_level_tiers:
            if signal_strength >= threshold:
                selected = max(selected, int(level))
        return selected

    def normal_full_cap_guard_reason(
        self,
        *,
        confidence: Optional[str],
        best_ask: Optional[float],
        max_entry_price: Optional[float],
        signal_strength: Optional[float],
        remaining_sec: Optional[float],
    ) -> Optional[str]:
        """Return why a normal full-cap entry should be skipped."""
        if not self.normal_full_cap_guard_enabled:
            return None
        if confidence != "normal":
            return None
        if best_ask is None or max_entry_price is None:
            return None
        if best_ask < max_entry_price - self.normal_full_cap_price_tolerance:
            return None
        if (
            self.normal_full_cap_min_signal_strength is not None
            and signal_strength is not None
            and signal_strength < self.normal_full_cap_min_signal_strength
        ):
            return "signal_strength_below_min"
        if (
            self.normal_full_cap_min_remaining_sec is not None
            and remaining_sec is not None
            and remaining_sec < self.normal_full_cap_min_remaining_sec
        ):
            return "remaining_sec_below_min"
        return None
