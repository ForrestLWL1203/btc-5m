"""DirectionPredictor ABC and MomentumPredictor V1 — weighted voting signals."""

from abc import ABC, abstractmethod
from typing import Optional

from polybot.market.series import MarketSeries
from .history import WindowHistory


class DirectionPredictor(ABC):
    """Abstract direction predictor — returns 'up', 'down', or None (skip)."""

    @abstractmethod
    def predict(self, history: WindowHistory) -> Optional[str]:
        ...


class MomentumPredictor(DirectionPredictor):
    """V1 predictor: weighted voting on 3 signals.

    Signals:
      1. Price momentum (50%) — Up token close price trend over last N windows
      2. Up/Down price offset (30%) — Up token price deviation from 0.50
      3. Streak reversal (20%) — consecutive same-direction results -> mean reversion
    """

    def __init__(self, series: MarketSeries):
        self.lookback = 3
        self.streak_threshold = 3 if series.slug_step <= 900 else 2
        self.min_history = 5

    def predict(self, history: WindowHistory) -> Optional[str]:
        if len(history) < self.min_history:
            return None

        score = 0.0

        # Signal 1: Price momentum (50%)
        momentum = self._price_momentum(history)
        score += momentum * 0.50

        # Signal 2: Up/Down price offset (30%)
        offset = self._price_offset(history)
        score += offset * 0.30

        # Signal 3: Streak reversal (20%)
        reversal = self._streak_reversal(history)
        score += reversal * 0.20

        if score > 0:
            return "up"
        elif score < 0:
            return "down"
        return None

    def _price_momentum(self, history: WindowHistory) -> float:
        """Positive = Up token prices rising. Negative = falling."""
        recent = history.last_n(self.lookback)
        if len(recent) < 2:
            return 0.0
        first = recent[0].up_price_close
        last = recent[-1].up_price_close
        if first == 0:
            return 0.0
        return (last - first) / first

    def _price_offset(self, history: WindowHistory) -> float:
        """Positive = Up token > 0.50. Negative = Up token < 0.50."""
        latest = history.latest()
        if latest is None:
            return 0.0
        return latest.up_price_close - 0.50

    def _streak_reversal(self, history: WindowHistory) -> float:
        """Positive = bet on 'up' next. Negative = bet on 'down' next.

        Consecutive same-direction results -> bet opposite (mean reversion).
        """
        records = history.records
        if len(records) < self.streak_threshold:
            return 0.0

        streak_dir = records[-1].resolved_side
        if streak_dir is None:
            return 0.0

        count = 0
        for r in reversed(records):
            if r.resolved_side == streak_dir:
                count += 1
            else:
                break

        if count < self.streak_threshold:
            return 0.0

        # Bet opposite direction
        return -0.10 if streak_dir == "up" else 0.10
