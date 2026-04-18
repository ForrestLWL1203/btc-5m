"""Tests for polybot.predict.history — ring buffer, recording, backfill."""

import pytest
from polybot.predict.history import WindowRecord, WindowHistory


def _record(start: int, up_open=0.50, up_close=0.55, down_open=0.50, down_close=0.45,
            up_vol=1.0, down_vol=1.0, resolved=None) -> WindowRecord:
    return WindowRecord(
        window_start=start,
        up_price_open=up_open,
        up_price_close=up_close,
        down_price_open=down_open,
        down_price_close=down_close,
        up_volume=up_vol,
        down_volume=down_vol,
        resolved_side=resolved,
    )


class TestWindowRecord:
    def test_create_record(self):
        r = _record(1000, resolved="up")
        assert r.window_start == 1000
        assert r.resolved_side == "up"


class TestWindowHistoryRingBuffer:
    def test_empty_history(self):
        h = WindowHistory(capacity=5)
        assert len(h) == 0
        assert h.latest() is None

    def test_record_and_retrieve(self):
        h = WindowHistory(capacity=5)
        h.record(_record(1000))
        assert len(h) == 1
        assert h.latest().window_start == 1000

    def test_capacity_overflow_overwrites_oldest(self):
        h = WindowHistory(capacity=3)
        h.record(_record(1000))
        h.record(_record(1300))
        h.record(_record(1600))
        assert len(h) == 3
        # Add 4th — should evict window 1000
        h.record(_record(1900))
        assert len(h) == 3
        assert h.records[0].window_start == 1300
        assert h.latest().window_start == 1900

    def test_latest_returns_most_recent(self):
        h = WindowHistory(capacity=5)
        h.record(_record(1000))
        h.record(_record(1300))
        assert h.latest().window_start == 1300

    def test_records_returns_ordered_list(self):
        h = WindowHistory(capacity=5)
        h.record(_record(1000))
        h.record(_record(1300))
        h.record(_record(1600))
        starts = [r.window_start for r in h.records]
        assert starts == [1000, 1300, 1600]

    def test_last_n_returns_most_recent_n(self):
        h = WindowHistory(capacity=5)
        for s in range(1000, 2500, 300):
            h.record(_record(s))
        result = h.last_n(3)
        assert len(result) == 3
        assert [r.window_start for r in result] == [1600, 1900, 2200]

    def test_last_n_more_than_available(self):
        h = WindowHistory(capacity=5)
        h.record(_record(1000))
        h.record(_record(1300))
        result = h.last_n(10)
        assert len(result) == 2
