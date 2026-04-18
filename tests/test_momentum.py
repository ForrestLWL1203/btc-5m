"""Tests for polybot.predict.momentum — weighted voting signals."""

import pytest
from polybot.predict.history import WindowHistory, WindowRecord
from polybot.predict.momentum import MomentumPredictor, DirectionPredictor
from polybot.market.series import MarketSeries


def _btc_5m() -> MarketSeries:
    return MarketSeries.from_known("btc-updown-5m")


def _record(start: int, up_close: float, down_close: float, resolved=None) -> WindowRecord:
    return WindowRecord(
        window_start=start,
        up_price_open=up_close - 0.02,
        up_price_close=up_close,
        down_price_open=down_close - 0.02,
        down_price_close=down_close,
        up_volume=1.0,
        down_volume=1.0,
        resolved_side=resolved,
    )


class TestMomentumPredictor:
    def test_predict_up_on_rising_up_token(self):
        """Rising Up token close prices -> predict 'up'."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        h.record(_record(1000, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1300, up_close=0.55, down_close=0.45, resolved="up"))
        h.record(_record(1600, up_close=0.60, down_close=0.40, resolved="up"))
        h.record(_record(1900, up_close=0.65, down_close=0.35, resolved="up"))
        h.record(_record(2200, up_close=0.70, down_close=0.30, resolved="up"))
        result = p.predict(h)
        assert result == "up"

    def test_predict_down_on_falling_up_token(self):
        """Falling Up token close prices -> predict 'down'."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        h.record(_record(1000, up_close=0.60, down_close=0.40, resolved="down"))
        h.record(_record(1300, up_close=0.55, down_close=0.45, resolved="down"))
        h.record(_record(1600, up_close=0.50, down_close=0.50, resolved="down"))
        h.record(_record(1900, up_close=0.45, down_close=0.55, resolved="down"))
        h.record(_record(2200, up_close=0.40, down_close=0.60, resolved="down"))
        result = p.predict(h)
        assert result == "down"

    def test_predict_none_when_insufficient_history(self):
        """Fewer than min_history records -> return None."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        h.record(_record(1000, up_close=0.55, down_close=0.45))
        result = p.predict(h)
        assert result is None

    def test_predict_streak_reversal(self):
        """3+ consecutive 'up' results with flat prices -> streak reversal pushes 'down'."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        h.record(_record(1000, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1300, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1600, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1900, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(2200, up_close=0.50, down_close=0.50, resolved="up"))
        result = p.predict(h)
        # With flat prices (no momentum, no offset) and 5 consecutive ups,
        # streak reversal pushes score negative -> "down"
        assert result == "down"

    def test_empty_history_returns_none(self):
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        assert p.predict(h) is None

    def test_is_direction_predictor_subclass(self):
        assert issubclass(MomentumPredictor, DirectionPredictor)
