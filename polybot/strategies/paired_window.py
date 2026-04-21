"""Window-level BTC trend strategy for Polymarket up/down markets."""

from __future__ import annotations

import logging
import time
from typing import Optional

from polybot.core.state import MonitorState
from polybot.market.binance import BinancePriceFeed
from polybot.market.series import MarketSeries
from .base import Strategy

log = logging.getLogger(__name__)


class PairedWindowStrategy(Strategy):
    """Trade once per window after BTC establishes a persistent move.

    This is the runtime counterpart to the paired-data research script:
    - wait for BTC to move away from the window open by `theta_pct`
    - require the move to persist for `persistence_sec`
    - only enter during a remaining-time band inside the 5m window
    - only buy when the target token is still in the configured price band
    - hold to resolution; no strategy-level early exit hooks
    """

    def __init__(
        self,
        series: MarketSeries,
        theta_pct: float = 0.02,
        entry_start_remaining_sec: float = 270.0,
        entry_end_remaining_sec: float = 120.0,
        persistence_sec: float = 10.0,
        min_entry_price: float = 0.60,
        max_entry_price: float = 0.70,
        min_move_ratio: float = 0.7,
        open_price_max_wait_sec: float = 30.0,
    ):
        self._series = series
        self._theta_pct = theta_pct
        self._entry_start_remaining_sec = entry_start_remaining_sec
        self._entry_end_remaining_sec = entry_end_remaining_sec
        self._persistence_sec = persistence_sec
        self._min_entry_price = min_entry_price
        self._max_entry_price = max_entry_price
        self._min_move_ratio = min_move_ratio
        self._open_price_max_wait_sec = open_price_max_wait_sec

        symbol = "btcusdt" if series.asset == "btc" else "ethusdt"
        self._feed = BinancePriceFeed(symbol=symbol)
        self._window_start_epoch: float = 0.0
        self._window_open_btc: Optional[float] = None
        self._signal_fired = False
        self._started = False

    @property
    def entry_start_remaining_sec(self) -> float:
        """Latest remaining time at which entries may begin."""
        return self._entry_start_remaining_sec

    @property
    def entry_end_remaining_sec(self) -> float:
        """Earliest remaining time at which entries may still occur."""
        return self._entry_end_remaining_sec

    async def start(self) -> None:
        await self._feed.start()
        self._started = True
        log.info(
            "PairedWindowStrategy started | theta=%.3f%% | remaining=[%.0f, %.0f]s | price=[%.2f, %.2f]",
            self._theta_pct,
            self._entry_end_remaining_sec,
            self._entry_start_remaining_sec,
            self._min_entry_price,
            self._max_entry_price,
        )

    async def stop(self) -> None:
        await self._feed.stop()
        self._started = False

    def set_window_start(self, epoch: float) -> None:
        self._window_start_epoch = epoch
        self._window_open_btc = None
        self._signal_fired = False

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        """Return a placeholder side; actual side resolves per signal."""
        return "up"

    def should_buy(self, price: float, state: MonitorState) -> bool:
        if not self._started or self._signal_fired or state.bought:
            return False
        if self._window_start_epoch <= 0:
            return False

        now = time.time()
        elapsed = now - self._window_start_epoch
        if elapsed < 0:
            return False

        remaining = self._series.slug_step - elapsed
        if remaining > self._entry_start_remaining_sec or remaining < self._entry_end_remaining_sec:
            return False

        open_price = self._ensure_window_open_btc()
        current_btc = self._feed.latest_price
        if open_price is None or current_btc is None or open_price <= 0:
            return False

        past_btc = self._feed.price_at_or_before(now - self._persistence_sec)
        if past_btc is None:
            return False

        move_pct = (current_btc - open_price) / open_price * 100.0
        if abs(move_pct) < self._theta_pct:
            return False

        past_move_pct = (past_btc - open_price) / open_price * 100.0
        if (move_pct > 0) != (past_move_pct > 0):
            return False
        if abs(move_pct) < abs(past_move_pct) * self._min_move_ratio:
            return False

        direction = "up" if move_pct > 0 else "down"
        entry_price = price if direction == "up" else max(0.0, min(1.0, 1.0 - price))
        if entry_price < self._min_entry_price or entry_price > self._max_entry_price:
            return False

        state.target_side = direction
        state.target_entry_price = entry_price
        self._signal_fired = True
        log.info(
            "PAIRED SIGNAL: dir=%s btc_open=%.1f btc_now=%.1f move=%.4f%% past=%.4f%% entry_price=%.3f remaining=%.0fs",
            direction.upper(),
            open_price,
            current_btc,
            move_pct,
            past_move_pct,
            entry_price,
            remaining,
        )
        return True

    def _ensure_window_open_btc(self) -> Optional[float]:
        if self._window_open_btc is not None:
            return self._window_open_btc
        self._window_open_btc = self._feed.first_price_at_or_after(
            self._window_start_epoch,
            max_forward_sec=self._open_price_max_wait_sec,
        )
        return self._window_open_btc
