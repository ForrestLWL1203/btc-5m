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
    """Trade once per window after BTC establishes a persistent move from window open.

    - wait for BTC to move away from the window open by fixed or dynamic theta
    - require the move to persist for `persistence_sec`
    - only enter during a remaining-time band inside the 5m window
    - only buy when the target token is at or below the active cap
    - direction is locked on first valid signal and cannot flip within a window
    - hold to resolution; no strategy-level early exit hooks
    """

    def __init__(
        self,
        series: MarketSeries,
        theta_pct: float = 0.02,
        theta_start_pct: Optional[float] = None,
        theta_end_pct: Optional[float] = None,
        entry_start_remaining_sec: float = 255.0,
        entry_end_remaining_sec: float = 120.0,
        persistence_sec: float = 10.0,
        max_entry_price: float = 0.75,
        min_move_ratio: float = 0.7,
        open_price_max_wait_sec: float = 30.0,
    ):
        self._series = series
        self._theta_pct = theta_pct
        self._theta_start_pct = theta_start_pct
        self._theta_end_pct = theta_end_pct
        self._entry_start_remaining_sec = entry_start_remaining_sec
        self._entry_end_remaining_sec = entry_end_remaining_sec
        self._persistence_sec = persistence_sec
        self._max_entry_price = max_entry_price
        self._min_move_ratio = min_move_ratio
        self._open_price_max_wait_sec = open_price_max_wait_sec

        symbol = "btcusdt" if series.asset == "btc" else "ethusdt"
        self._feed = BinancePriceFeed(symbol=symbol)
        self._window_start_epoch: float = 0.0
        self._window_open_btc: Optional[float] = None
        self._committed_direction: Optional[str] = None
        self._signal_logged = False
        self._started = False

    @property
    def entry_start_remaining_sec(self) -> float:
        return self._entry_start_remaining_sec

    @property
    def entry_end_remaining_sec(self) -> float:
        return self._entry_end_remaining_sec

    @property
    def max_entry_price(self) -> float:
        return self._max_entry_price

    async def start(self) -> None:
        await self._feed.start()
        self._started = True
        log.debug(
            "PairedWindowStrategy started | theta=%.3f%% dynamic=[%s,%s] | persistence=%ds | "
            "remaining=[%.0f, %.0f]s | max_entry=%.2f",
            self._theta_pct,
            self._theta_start_pct,
            self._theta_end_pct,
            self._persistence_sec,
            self._entry_end_remaining_sec,
            self._entry_start_remaining_sec,
            self._max_entry_price,
        )

    async def stop(self) -> None:
        await self._feed.stop()
        self._started = False

    def set_window_start(self, epoch: float) -> None:
        self._window_start_epoch = epoch
        self._window_open_btc = None
        self._committed_direction = None
        self._signal_logged = False

    async def preload_open_btc(self, epoch: float) -> None:
        """Seed window open BTC price via REST if WS feed has no coverage."""
        if self._window_open_btc is not None:
            return
        cached = self._feed.first_price_at_or_after(
            epoch, max_forward_sec=self._open_price_max_wait_sec,
        )
        if cached is not None:
            return
        price = await self._feed.fetch_open_at(epoch)
        if price is not None:
            self._window_open_btc = price
            log.debug("OPEN_BTC_REST_SEEDED: epoch=%.0f price=%.2f", epoch, price)

    dynamic_side = True

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        return "up"

    def should_buy(self, price: float, state: MonitorState) -> bool:
        if not self._started or state.bought:
            return False
        if self._window_start_epoch <= 0:
            return False

        now = time.time()
        elapsed = now - self._window_start_epoch
        if elapsed < 0:
            return False

        remaining = self._series.slug_step - elapsed

        open_price = self._ensure_window_open_btc()
        current_btc = self._feed.latest_price
        if open_price is None or current_btc is None or open_price <= 0:
            return False

        past_btc = self._feed.price_at_or_before(now - self._persistence_sec)
        if past_btc is None:
            return False

        move_pct = (current_btc - open_price) / open_price * 100.0
        active_theta_pct = self._active_theta_pct(elapsed)
        if abs(move_pct) < active_theta_pct:
            return False

        past_move_pct = (past_btc - open_price) / open_price * 100.0
        if (move_pct > 0) != (past_move_pct > 0):
            return False
        if abs(move_pct) < abs(past_move_pct) * self._min_move_ratio:
            return False

        signal_strength = abs(move_pct) / active_theta_pct if active_theta_pct > 0 else 0.0
        past_signal_strength = abs(past_move_pct) / active_theta_pct if active_theta_pct > 0 else 0.0
        if remaining > self._entry_start_remaining_sec or remaining < self._entry_end_remaining_sec:
            return False

        direction = "up" if move_pct > 0 else "down"
        if self._committed_direction is not None and direction != self._committed_direction:
            return False
        self._committed_direction = direction

        state.target_side = direction
        state.signal_reference_price = (
            price if direction == "up" else max(0.0, min(1.0, 1.0 - price))
        )
        state.target_max_entry_price = self._max_entry_price
        state.target_signal_confidence = "normal"
        state.target_signal_strength = signal_strength
        state.target_past_signal_strength = past_signal_strength
        state.target_active_theta_pct = active_theta_pct
        state.target_remaining_sec = remaining

        if not self._signal_logged:
            log.info(
                "SIGNAL: dir=%s btc_open=%.1f btc_now=%.1f move=%.4f%% past=%.4f%% "
                "theta=%.4f%% strength=%.2fx signal_ref_price=%.3f max_entry=%.3f remaining=%.0fs",
                direction.upper(),
                open_price,
                current_btc,
                move_pct,
                past_move_pct,
                active_theta_pct,
                signal_strength,
                state.signal_reference_price,
                state.target_max_entry_price,
                remaining,
            )
            self._signal_logged = True
        return True

    def _ensure_window_open_btc(self) -> Optional[float]:
        if self._window_open_btc is not None:
            return self._window_open_btc
        self._window_open_btc = self._feed.first_price_at_or_after(
            self._window_start_epoch,
            max_forward_sec=self._open_price_max_wait_sec,
        )
        return self._window_open_btc

    def _active_theta_pct(self, elapsed_sec: float) -> float:
        """Return fixed theta or linearly increasing theta over the entry band."""
        if self._theta_start_pct is None or self._theta_end_pct is None:
            return self._theta_pct
        start_elapsed = max(0.0, self._series.slug_step - self._entry_start_remaining_sec)
        end_elapsed = max(start_elapsed, self._series.slug_step - self._entry_end_remaining_sec)
        if end_elapsed <= start_elapsed:
            return self._theta_end_pct
        progress = (elapsed_sec - start_elapsed) / (end_elapsed - start_elapsed)
        progress = max(0.0, min(1.0, progress))
        return self._theta_start_pct + progress * (self._theta_end_pct - self._theta_start_pct)
