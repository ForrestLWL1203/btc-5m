"""TradeConfig — common trading parameters shared across all strategies.

Every strategy needs the same TP/SL, amount, re-entry limits, and round control.
These are universal, not strategy-specific. Strategy only contains buy decision logic.
"""

from dataclasses import dataclass
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
    effective_sl_pct: float = 0.0  # actual SL pct used (after tightening)


@dataclass
class TradeConfig:
    """Common trading parameters — strategy-agnostic.

    TP/SL can be percentage-based (tp_pct/sl_pct) or absolute price (tp_price/sl_price).
    If both specified for same direction, absolute price takes priority.
    """

    amount: float = 5.0
    # Percentage-based TP/SL
    tp_pct: Optional[float] = 0.50
    sl_pct: Optional[float] = 0.30
    # Absolute price TP/SL
    tp_price: Optional[float] = None
    sl_price: Optional[float] = None
    # Re-entry and rounds
    max_sl_reentry: int = 0
    max_tp_reentry: int = 0
    max_edge_reentry: int = 0
    max_entries_per_window: Optional[int] = None
    rounds: Optional[int] = None  # None = infinite

    SL_TIGHTENING_STEP = 0.10  # each SL re-entry tightens by 10% absolute
    SL_FLOOR = 0.05            # minimum SL pct (never tighter than 5%)

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

        # TP: absolute price takes priority over percentage
        if self.tp_price is not None:
            tp_threshold = self.tp_price
        elif self.tp_pct is not None:
            tp_threshold = entry * (1.0 + self.tp_pct)
        else:
            return None

        # SL: absolute price (with tightening) or percentage (with tightening)
        if self.sl_price is not None:
            gap = entry - self.sl_price
            min_gap = entry * self.SL_FLOOR
            effective_gap = max(min_gap, gap * (1.0 - self.SL_TIGHTENING_STEP * state.stop_loss_count))
            sl_threshold = entry - effective_gap
            effective_sl_pct = effective_gap / entry
        elif self.sl_pct is not None:
            effective_sl_pct = max(
                self.SL_FLOOR,
                self.sl_pct - self.SL_TIGHTENING_STEP * state.stop_loss_count,
            )
            sl_threshold = entry * (1.0 - effective_sl_pct)
        else:
            return None

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
                effective_sl_pct=effective_sl_pct,
            )

        return None
