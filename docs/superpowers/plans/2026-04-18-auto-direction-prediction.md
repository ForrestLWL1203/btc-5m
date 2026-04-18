# Auto Direction Prediction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add automatic Up/Down direction prediction to the Polymarket trading bot using momentum signals from Up/Down token prices.

**Architecture:** New `polybot/predict/` package with `WindowHistory` (ring buffer + Gamma API backfill) and `MomentumPredictor` (weighted voting on 3 signals). Integrated at `monitor_window()` entry — predictor runs once per window, sets `trade_config.side`, then existing logic unchanged.

**Tech Stack:** Python 3.11+, asyncio, `requests` (Gamma API), `pytest` (tests)

---

## File Structure

### New Files
- `polybot/predict/__init__.py` — Package init, exports
- `polybot/predict/history.py` — `WindowRecord` dataclass, `WindowHistory` ring buffer with Gamma API backfill
- `polybot/predict/momentum.py` — `DirectionPredictor` ABC, `MomentumPredictor` V1
- `tests/test_history.py` — Tests for WindowHistory
- `tests/test_momentum.py` — Tests for MomentumPredictor

### Modified Files
- `polybot/config_loader.py` — Add `build_direction_config()`, `DIRECTION_REGISTRY`
- `polybot/trading/monitor.py` — Integrate predictor at window start, record history at window end
- `tests/test_config_loader.py` — Tests for direction config building

---

### Task 1: WindowRecord dataclass + WindowHistory ring buffer

**Files:**
- Create: `polybot/predict/__init__.py`
- Create: `polybot/predict/history.py`
- Create: `tests/test_history.py`

- [ ] **Step 1: Create `polybot/predict/__init__.py`**

```python
"""Auto direction prediction package."""
```

- [ ] **Step 2: Write failing tests for WindowHistory ring buffer**

Create `tests/test_history.py`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_history.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.predict'`

- [ ] **Step 4: Implement WindowRecord and WindowHistory ring buffer**

Create `polybot/predict/history.py`:

```python
"""WindowHistory — ring buffer for cross-window price data with Gamma API backfill."""

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

HISTORY_CAPACITY = {
    "5m": 100,
    "15m": 30,
    "1h": 24,
    "4h": 6,
    "1d": 7,
}


@dataclass
class WindowRecord:
    """Price data for a single trading window."""

    window_start: int
    up_price_open: float = 0.0
    up_price_close: float = 0.0
    down_price_open: float = 0.0
    down_price_close: float = 0.0
    up_volume: float = 0.0
    down_volume: float = 0.0
    resolved_side: Optional[str] = None


class WindowHistory:
    """Ring buffer of WindowRecords, ordered oldest→newest."""

    def __init__(self, capacity: int):
        self._buf: deque[WindowRecord] = deque(maxlen=capacity)

    @classmethod
    def for_timeframe(cls, timeframe: str) -> "WindowHistory":
        cap = HISTORY_CAPACITY.get(timeframe, 100)
        return cls(capacity=cap)

    def record(self, rec: WindowRecord) -> None:
        self._buf.append(rec)

    def latest(self) -> Optional[WindowRecord]:
        return self._buf[-1] if self._buf else None

    def last_n(self, n: int) -> List[WindowRecord]:
        """Return the most recent N records, ordered oldest→newest."""
        if n >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-n:]

    @property
    def records(self) -> List[WindowRecord]:
        return list(self._buf)

    def __len__(self) -> int:
        return len(self._buf)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_history.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/predict/__init__.py polybot/predict/history.py tests/test_history.py
git commit -m "feat: add WindowRecord dataclass and WindowHistory ring buffer"
```

---

### Task 2: Gamma API backfill for WindowHistory

**Files:**
- Modify: `polybot/predict/history.py`
- Modify: `tests/test_history.py`

- [ ] **Step 1: Write failing tests for backfill**

Add to `tests/test_history.py`:

```python
from unittest.mock import patch, MagicMock


class TestBackfill:
    def _mock_market_response(self, slug, up_close=0.55, down_close=0.45, resolved="up"):
        """Return a mock Gamma API market dict."""
        return {
            "slug": slug,
            "active": False,
            "closed": True,
            "clobTokenIds": '["up-tok","down-tok"]',
            "outcomePrices": f'["{up_close}","{down_close}"]',
            "endDate": "2026-04-18T12:05:00Z",
            "eventStartTime": "2026-04-18T12:00:00Z",
            "volume": "100.0",
        }

    @patch("polybot.predict.history._fetch_market_for_backfill")
    def test_backfill_fills_history(self, mock_fetch):
        mock_fetch.side_effect = lambda slug: self._mock_market_response(slug)
        h = WindowHistory(capacity=5)
        h.backfill("btc-updown-5m", slug_step=300, count=3, current_epoch=1900)
        assert len(h) == 3

    @patch("polybot.predict.history._fetch_market_for_backfill")
    def test_backfill_skips_failed_fetches(self, mock_fetch):
        def alternate(slug):
            if "1600" in slug:
                return None
            return self._mock_market_response(slug)
        mock_fetch.side_effect = alternate
        h = WindowHistory(capacity=10)
        h.backfill("btc-updown-5m", slug_step=300, count=3, current_epoch=1900)
        assert len(h) == 2

    @patch("polybot.predict.history._fetch_market_for_backfill")
    def test_backfill_respects_capacity(self, mock_fetch):
        mock_fetch.side_effect = lambda slug: self._mock_market_response(slug)
        h = WindowHistory(capacity=2)
        h.backfill("btc-updown-5m", slug_step=300, count=5, current_epoch=1900)
        assert len(h) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_history.py::TestBackfill -v`
Expected: FAIL — `AttributeError: 'WindowHistory' object has no attribute 'backfill'`

- [ ] **Step 3: Implement backfill**

Add to `polybot/predict/history.py` — append after the `WindowHistory` class:

```python
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

_GAMMA_API = "https://gamma-api.polymarket.com/markets"


def _fetch_market_for_backfill(slug: str) -> Optional[dict]:
    """Fetch a single market by slug for backfill. Returns raw dict or None."""
    try:
        resp = requests.get(_GAMMA_API, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        return None
    except Exception:
        return None


def _parse_backfill_record(m: dict, slug: str) -> Optional[WindowRecord]:
    """Parse a Gamma API market dict into a WindowRecord."""
    try:
        prices_raw = m.get("outcomePrices", "[]")
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else list(prices_raw)
        if len(prices) < 2:
            return None

        up_close = float(prices[0])
        down_close = float(prices[1])

        # Extract epoch from slug: "btc-updown-5m-1713300000"
        parts = slug.rsplit("-", 1)
        window_start = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else 0

        resolved = None
        if up_close > down_close:
            resolved = "up"
        elif down_close > up_close:
            resolved = "down"

        return WindowRecord(
            window_start=window_start,
            up_price_open=0.0,
            up_price_close=up_close,
            down_price_open=0.0,
            down_price_close=down_close,
            up_volume=float(m.get("volume", 0)),
            down_volume=0.0,
            resolved_side=resolved,
        )
    except (ValueError, TypeError, IndexError):
        return None
```

Add `backfill` method to the `WindowHistory` class (inside the class body, after `__len__`):

```python
    def backfill(self, slug_prefix: str, slug_step: int, count: int, current_epoch: int) -> None:
        """Fetch past N windows from Gamma API and populate history."""
        from polybot.predict.history import _fetch_market_for_backfill, _parse_backfill_record

        slugs = []
        for i in range(1, count + 1):
            epoch = current_epoch - i * slug_step
            slugs.append(f"{slug_prefix}-{epoch}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = {pool.submit(_fetch_market_for_backfill, s): s for s in slugs}
            for future in as_completed(futures):
                slug = futures[future]
                try:
                    m = future.result()
                    if m is None:
                        continue
                    rec = _parse_backfill_record(m, slug)
                    if rec is not None:
                        self.record(rec)
                except Exception:
                    continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_history.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/predict/history.py tests/test_history.py
git commit -m "feat: add Gamma API backfill for WindowHistory"
```

---

### Task 3: DirectionPredictor ABC + MomentumPredictor V1

**Files:**
- Create: `polybot/predict/momentum.py`
- Create: `tests/test_momentum.py`

- [ ] **Step 1: Write failing tests for MomentumPredictor**

Create `tests/test_momentum.py`:

```python
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
        """Rising Up token close prices → predict 'up'."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        # Up token prices rising: 0.50 → 0.55 → 0.60
        h.record(_record(1000, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1300, up_close=0.55, down_close=0.45, resolved="up"))
        h.record(_record(1600, up_close=0.60, down_close=0.40, resolved="up"))
        h.record(_record(1900, up_close=0.65, down_close=0.35, resolved="up"))
        h.record(_record(2200, up_close=0.70, down_close=0.30, resolved="up"))
        result = p.predict(h)
        assert result == "up"

    def test_predict_down_on_falling_up_token(self):
        """Falling Up token close prices → predict 'down'."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        # Up token prices falling: 0.60 → 0.55 → 0.50
        h.record(_record(1000, up_close=0.60, down_close=0.40, resolved="down"))
        h.record(_record(1300, up_close=0.55, down_close=0.45, resolved="down"))
        h.record(_record(1600, up_close=0.50, down_close=0.50, resolved="down"))
        h.record(_record(1900, up_close=0.45, down_close=0.55, resolved="down"))
        h.record(_record(2200, up_close=0.40, down_close=0.60, resolved="down"))
        result = p.predict(h)
        assert result == "down"

    def test_predict_none_when_insufficient_history(self):
        """Fewer than min_history records → return None."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        h.record(_record(1000, up_close=0.55, down_close=0.45))
        result = p.predict(h)
        assert result is None

    def test_predict_streak_reversal(self):
        """3+ consecutive 'up' results → momentum predictor leans toward reversal."""
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        # All resolved up, but Up token close stays flat (~0.50) — weak momentum
        h.record(_record(1000, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1300, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1600, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(1900, up_close=0.50, down_close=0.50, resolved="up"))
        h.record(_record(2200, up_close=0.50, down_close=0.50, resolved="up"))
        result = p.predict(h)
        # With flat prices and 5 consecutive ups, streak reversal pushes score negative
        assert result == "down"

    def test_empty_history_returns_none(self):
        p = MomentumPredictor(_btc_5m())
        h = WindowHistory(capacity=10)
        assert p.predict(h) is None

    def test_is_direction_predictor_subclass(self):
        assert issubclass(MomentumPredictor, DirectionPredictor)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_momentum.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.predict.momentum'`

- [ ] **Step 3: Implement DirectionPredictor and MomentumPredictor**

Create `polybot/predict/momentum.py`:

```python
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
      3. Streak reversal (20%) — consecutive same-direction results → mean reversion
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

        Consecutive same-direction results → bet opposite (mean reversion).
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_momentum.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/predict/momentum.py tests/test_momentum.py
git commit -m "feat: add DirectionPredictor ABC and MomentumPredictor V1"
```

---

### Task 4: Direction config in config_loader

**Files:**
- Modify: `polybot/config_loader.py`
- Modify: `tests/test_config_loader.py`

- [ ] **Step 1: Write failing tests for direction config**

Add to `tests/test_config_loader.py` — add import at top and new class at bottom:

Add to imports:
```python
from polybot.config_loader import build_direction_config, DIRECTION_REGISTRY
from polybot.predict.momentum import MomentumPredictor
```

Add class:
```python
class TestBuildDirectionConfig:
    def test_momentum_type_creates_predictor(self):
        cfg = {
            "direction": {"type": "momentum", "fallback_side": "up"},
            "market": {"asset": "btc", "timeframe": "5m"},
        }
        series = build_series(cfg)
        result = build_direction_config(cfg, series)
        assert result["predictor"] is not None
        assert isinstance(result["predictor"], MomentumPredictor)
        assert result["fallback_side"] == "up"

    def test_fixed_type_no_predictor(self):
        cfg = {
            "direction": {"type": "fixed", "fallback_side": "down"},
        }
        result = build_direction_config(cfg, MarketSeries.from_known("btc-updown-5m"))
        assert result["predictor"] is None
        assert result["fallback_side"] == "down"

    def test_no_direction_block_returns_none(self):
        cfg = {}
        result = build_direction_config(cfg, MarketSeries.from_known("btc-updown-5m"))
        assert result["predictor"] is None
        assert result["fallback_side"] is None

    def test_registry_has_momentum(self):
        assert "momentum" in DIRECTION_REGISTRY
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_config_loader.py::TestBuildDirectionConfig -v`
Expected: FAIL — `ImportError: cannot import name 'build_direction_config'`

- [ ] **Step 3: Implement direction config in config_loader**

Add to `polybot/config_loader.py` imports:
```python
from polybot.predict.momentum import MomentumPredictor
```

Add after `STRATEGY_REGISTRY`:
```python
DIRECTION_REGISTRY: dict[str, type] = {
    "momentum": MomentumPredictor,
}
```

Add function after `build_strategy`:
```python
def build_direction_config(cfg: dict, series: "MarketSeries") -> dict:
    """Build direction prediction config.

    Returns dict with keys:
        predictor: DirectionPredictor instance or None
        fallback_side: "up"/"down"/None
    """
    dir_cfg = cfg.get("direction")
    if not dir_cfg:
        return {"predictor": None, "fallback_side": None}

    dir_type = dir_cfg.get("type", "fixed")
    fallback = dir_cfg.get("fallback_side")

    if dir_type == "fixed":
        return {"predictor": None, "fallback_side": fallback}

    cls = DIRECTION_REGISTRY.get(dir_type)
    if cls is None:
        raise ValueError(
            f"Unknown direction type: {dir_type}. "
            f"Available: {', '.join(DIRECTION_REGISTRY.keys())}"
        )

    return {
        "predictor": cls(series),
        "fallback_side": fallback,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_config_loader.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/config_loader.py tests/test_config_loader.py
git commit -m "feat: add direction config parsing and DIRECTION_REGISTRY"
```

---

### Task 5: Integrate predictor into monitor loop

**Files:**
- Modify: `polybot/trading/monitor.py`

- [ ] **Step 1: Write failing test for direction prediction in monitor**

Add to `tests/test_monitor.py`:

```python
from polybot.predict.history import WindowHistory, WindowRecord
from polybot.predict.momentum import MomentumPredictor


class TestDirectionPrediction:
    @pytest.mark.asyncio
    async def test_predictor_sets_side_at_window_start(self):
        """Predictor is called at window start and sets trade_config.side."""
        import datetime
        from polybot.trading.monitor import monitor_window

        utc = datetime.timezone.utc
        now = int(time.time())
        start = (now // 300) * 300  # align to 5m boundary
        window = MarketWindow(
            question="Bitcoin Up or Down - Test",
            up_token="up-tok",
            down_token="down-tok",
            start_time=datetime.datetime.fromtimestamp(start, tz=utc),
            end_time=datetime.datetime.fromtimestamp(start + 300, tz=utc),
            slug="btc-updown-5m-test",
        )

        # Build history that predicts 'down'
        history = WindowHistory(capacity=10)
        for i in range(6):
            history.record(WindowRecord(
                window_start=start - (6 - i) * 300,
                up_price_open=0.55, up_price_close=0.40,
                down_price_open=0.45, down_price_close=0.60,
                up_volume=1.0, down_volume=1.0, resolved_side="down",
            ))

        predictor = MomentumPredictor(
            MarketSeries.from_known("btc-updown-5m")
        )
        tc = TradeConfig(side="up")  # default up, predictor should override

        mock_ws = MagicMock()
        mock_ws.set_on_price = MagicMock()
        mock_ws.switch_tokens = AsyncMock()
        mock_ws.get_latest_price = MagicMock(return_value=None)
        mock_ws.close = AsyncMock()

        with patch("polybot.trading.monitor.find_next_window", return_value=None), \
             patch("polybot.trading.monitor.get_midpoint_async", new_callable=AsyncMock, return_value=None):
            await monitor_window(
                window, dry_run=True, preopened=True, existing_ws=mock_ws,
                trade_config=tc, predictor=predictor, history=history,
            )

        assert tc.side == "down"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_monitor.py::TestDirectionPrediction -v`
Expected: FAIL — `TypeError: monitor_window() got an unexpected keyword argument 'predictor'`

- [ ] **Step 3: Add predictor and history params to monitor_window**

Modify `polybot/trading/monitor.py`:

1. Add import at top:
```python
from polybot.predict.history import WindowHistory
from polybot.predict.momentum import DirectionPredictor
```

2. Update `monitor_window` signature — add `predictor` and `history` params:
```python
async def monitor_window(
    window: MarketWindow,
    dry_run: bool = False,
    preopened: bool = False,
    existing_ws: Optional[PriceStream] = None,
    trade_config: Optional[TradeConfig] = None,
    strategy: Optional[Strategy] = None,
    series: Optional[MarketSeries] = None,
    predictor: Optional[DirectionPredictor] = None,
    history: Optional[WindowHistory] = None,
) -> tuple[Optional[MarketWindow], Optional[PriceStream], bool]:
```

3. After `if strategy is None: strategy = ImmediateStrategy()` (around line 341), add direction prediction logic:
```python
    # Direction prediction — runs once per window
    if predictor is not None and history is not None:
        direction = predictor.predict(history)
        if direction is not None:
            trade_config.side = direction
            log_event(log, logging.INFO, SIGNAL, {
                "action": "DIRECTION_PREDICTED",
                "side": direction.upper(),
                "window": window.short_label,
                "history_len": len(history),
            })
        else:
            log_event(log, logging.WARNING, SIGNAL, {
                "action": "DIRECTION_UNCLEAR",
                "window": window.short_label,
                "history_len": len(history),
            })
```

4. In `_monitor_single_window`, at the EXPIRED case and window-ending-soon case, add history recording after the sell. Find the line `# Always pre-fetch next window on expiry to avoid stale fallback` and add BEFORE it:

```python
            # Record window result in history
            if history is not None and state.bought:
                history.record(WindowRecord(
                    window_start=window.start_epoch,
                    up_price_open=0.0,
                    up_price_close=0.0,
                    down_price_open=0.0,
                    down_price_close=0.0,
                    resolved_side=None,
                ))
```

Note: For now, open/close prices in runtime recording are zero-filled. Full WS price capture will be added in Task 6. The history recording here ensures backfill works and the predictor has data flowing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_monitor.py -v`
Expected: All PASS (including new test)

- [ ] **Step 5: Run full test suite**

Run: `python3.11 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/trading/monitor.py tests/test_monitor.py
git commit -m "feat: integrate DirectionPredictor into monitor_window loop"
```

---

### Task 6: Wire up direction config in run.py

**Files:**
- Modify: `run.py`

- [ ] **Step 1: Check current run.py structure**

Read `run.py` to understand the current entry point flow.

- [ ] **Step 2: Add direction config loading**

In `run.py`, add the direction config loading. After the existing `build_series`, `build_strategy`, `build_trade_config` calls, add:

```python
from polybot.config_loader import build_direction_config
```

And after trade_config is built:
```python
    direction_cfg = build_direction_config(cfg, series)
    predictor = direction_cfg["predictor"]
    fallback_side = direction_cfg["fallback_side"]
    if fallback_side and trade_config is not None:
        trade_config.side = fallback_side
```

Pass `predictor` and `history` to `monitor_window()` calls.

Initialize `history` with backfill when predictor is configured:
```python
    history = None
    if predictor is not None:
        history = WindowHistory.for_timeframe(series.timeframe)
        history.backfill(
            slug_prefix=series.slug_prefix,
            slug_step=series.slug_step,
            count=history._buf.maxlen,
            current_epoch=int(time.time()),
        )
```

The exact wiring depends on `run.py` structure — adjust accordingly.

- [ ] **Step 3: Test dry-run with momentum direction**

Run: `python3.11 run.py --config strategy.yaml --dry`

Verify logs show `DIRECTION_PREDICTED` events or `DIRECTION_UNCLEAR` if insufficient history.

- [ ] **Step 4: Commit**

```bash
git add run.py
git commit -m "feat: wire up direction config and history backfill in run.py"
```

---

### Task 7: Update package exports

**Files:**
- Modify: `polybot/predict/__init__.py`

- [ ] **Step 1: Update `polybot/predict/__init__.py` with exports**

```python
"""Auto direction prediction package."""

from .history import WindowHistory, WindowRecord
from .momentum import DirectionPredictor, MomentumPredictor

__all__ = [
    "DirectionPredictor",
    "MomentumPredictor",
    "WindowHistory",
    "WindowRecord",
]
```

- [ ] **Step 2: Run full test suite**

Run: `python3.11 -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add polybot/predict/__init__.py
git commit -m "feat: export predict package public API"
```
