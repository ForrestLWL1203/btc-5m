"""TradeConfig — common trading parameters shared across all strategies.

Every strategy needs the same TP/SL, amount, side, re-entry limits, and round control.
These are universal, not strategy-specific. Strategy only contains buy decision logic.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from polybot.core.state import MonitorState


class ExitReason(Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"


@dataclass
class ExitSignal:
    """Returned by TradeConfig.check_exit when an exit is triggered."""

    reason: ExitReason
    threshold: float     # the absolute price threshold that was crossed
    can_reenter: bool    # whether re-entry is allowed after this exit


@dataclass
class TradeConfig:
    """Common trading parameters — strategy-agnostic."""

    side: str = "up"
    amount: float = 5.0
    tp_pct: float = 0.50
    sl_pct: float = 0.30
    max_sl_reentry: int = 0
    max_tp_reentry: int = 0
    rounds: Optional[int] = None  # None = infinite

    def check_exit(
        self,
        tp_price: float,
        sl_price: float,
        state: MonitorState,
    ) -> Optional[ExitSignal]:
        """Check TP/SL thresholds relative to entry price."""
        entry = state.entry_price
        if entry <= 0:
            return None

        tp_threshold = entry * (1.0 + self.tp_pct)
        sl_threshold = entry * (1.0 - self.sl_pct)

        if tp_price > tp_threshold:
            count = state.tp_count + 1
            can_reenter = count <= self.max_tp_reentry
            return ExitSignal(
                reason=ExitReason.TAKE_PROFIT,
                threshold=tp_threshold,
                can_reenter=can_reenter,
            )

        if sl_price < sl_threshold:
            count = state.stop_loss_count + 1
            can_reenter = count <= self.max_sl_reentry
            return ExitSignal(
                reason=ExitReason.STOP_LOSS,
                threshold=sl_threshold,
                can_reenter=can_reenter,
            )

        return None
