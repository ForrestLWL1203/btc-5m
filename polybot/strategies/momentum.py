"""MomentumStrategy — auto-predict direction using momentum signals.

Uses BinanceKlineFetcher + MomentumPredictor to decide up/down each window.
Buy decision is always True (like ImmediateStrategy).
"""

from typing import Optional

from .base import Strategy
from polybot.core.state import MonitorState
from polybot.market.series import MarketSeries
from polybot.predict.momentum import MomentumPredictor


class MomentumStrategy(Strategy):
    """Auto-predict trading direction using momentum signals."""

    def __init__(self, series: MarketSeries):
        self._predictor = MomentumPredictor(series)

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        if candles is None:
            return None
        return self._predictor.predict(candles)

    def should_buy(self, price: float, state: MonitorState) -> bool:
        return True
