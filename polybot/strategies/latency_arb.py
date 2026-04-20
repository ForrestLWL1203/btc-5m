"""Latency arbitrage strategy — exploit BTC lead over Polymarket token prices.

V2: Multi-signal entry with edge persistence and time-based exit.

Edge quality findings from data analysis:
  - Edge < 0.01: pure noise, negative PnL
  - Edge 0.01-0.02: still noise
  - Edge >= 0.02: positive net PnL after fees (+0.0308)
  - Best hold time: 2.0s
  - Velocity consistency NOT a useful filter (counter-intuitive)
  - Low flow + medium edge is the strongest combination
"""

import logging
import time
from collections import deque
from dataclasses import replace
from typing import Optional

from polybot.core.state import MonitorState
from polybot.market.binance import BinanceTradeFeed
from polybot.market.series import MarketSeries
from .base import Strategy

log = logging.getLogger(__name__)


class LatencyArbStrategy(Strategy):
    """Latency arbitrage: trade UP/DOWN based on BTC price lead signal."""

    def __init__(
        self,
        series: MarketSeries,
        coefficients: Optional[dict[str, float]] = None,
        edge_threshold: float = 0.02,
        noise_threshold: float = 0.005,
        max_data_age_ms: float = 500.0,
        min_entry_price: float = 0.0,
        max_entry_price: float = 0.85,
        entry_window_sec: float = 240.0,
        edge_exit_fraction: float = 0.3,
        max_hold_sec: float = 2.0,
        edge_decay_grace_ms: float = 0.0,
        persistence_ms: float = 200.0,
        cooldown_sec: float = 0.5,
        min_reentry_gap_sec: float = 0.0,
        edge_rearm_threshold: float = 0.0,
        phase_one_sec: float = 0.0,
        max_entries_phase_one: Optional[int] = None,
        phase_two_sec: float = 0.0,
        max_entries_phase_two: Optional[int] = None,
        disable_after_sec: float = 0.0,
    ):
        self._series = series
        self._coefficients = coefficients or {}
        self._edge_threshold = edge_threshold
        self._noise_threshold = noise_threshold
        self._max_data_age_ms = max_data_age_ms
        self._min_entry_price = min_entry_price
        self._max_entry_price = max_entry_price
        self._entry_window_sec = entry_window_sec
        self._edge_exit_threshold = edge_threshold * edge_exit_fraction
        self._max_hold_sec = max_hold_sec
        self._edge_decay_grace_ms = edge_decay_grace_ms
        self._persistence_ms = persistence_ms
        self._cooldown_sec = cooldown_sec
        self._min_reentry_gap_sec = min_reentry_gap_sec
        self._edge_rearm_threshold = edge_rearm_threshold
        self._phase_one_sec = phase_one_sec
        self._max_entries_phase_one = max_entries_phase_one
        self._phase_two_sec = phase_two_sec
        self._max_entries_phase_two = max_entries_phase_two
        self._disable_after_sec = disable_after_sec

        symbol = "btcusdt" if series.asset == "btc" else "ethusdt"
        self._feed = BinanceTradeFeed(symbol=symbol)
        self._started = False
        self._window_start_epoch: float = 0

        # Edge persistence: track recent edges to verify sustained signal
        self._edge_history: deque[tuple[float, float]] = deque(maxlen=20)

        # Position tracking
        self._entry_ts: float = 0
        self._entry_edge: float = 0
        self._last_signal_ts: float = 0  # last time should_buy returned True
        self._last_entry_ts: float = 0
        self._entry_rearmed: bool = True
        self._cached_features = None
        self._cached_features_ts: float = 0.0
        self._last_diag_log_ts: float = 0.0

    async def start(self) -> None:
        """Connect Binance WS. Called once before first window."""
        await self._feed.start()
        self._started = True
        log.info(
            "LatencyArbStrategy V2 started | threshold=%.3f | exit=%.4f | "
            "max_hold=%.1fs | persist=%.0fms | max_price=%.2f",
            self._edge_threshold, self._edge_exit_threshold,
            self._max_hold_sec, self._persistence_ms, self._max_entry_price,
        )

    async def stop(self) -> None:
        """Disconnect Binance WS."""
        await self._feed.stop()
        self._started = False

    def set_window_start(self, epoch: float) -> None:
        """Called by monitor at window start."""
        self._window_start_epoch = epoch
        self._entry_ts = 0
        self._entry_edge = 0
        self._last_entry_ts = 0
        self._entry_rearmed = True
        self._edge_history.clear()

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        """Return 'up' placeholder so monitor doesn't skip the window."""
        return "up"

    def should_buy(self, price: float, state: MonitorState) -> bool:
        """Multi-signal entry: edge + persistence + freshness."""
        if not self._started:
            return False

        features = self._get_features()
        if features is None:
            return False

        now = time.time()

        # Track edge for persistence check
        edge = self._compute_edge(features)
        self._record_edge(now, edge)
        self._maybe_rearm_entry(edge)

        # Freshness check
        if features.data_age_ms > self._max_data_age_ms:
            self._log_blocked(now, "stale_data", price, features, edge)
            return False

        # Noise filter
        if abs(features.ret_2s) < self._noise_threshold and abs(features.ret_5s) < self._noise_threshold:
            self._log_blocked(now, "noise", price, features, edge)
            return False

        # Entry time gate
        elapsed = now - self._window_start_epoch
        if elapsed > self._entry_window_sec or elapsed < 0:
            self._log_blocked(now, "outside_entry_window", price, features, edge)
            return False
        if self._disable_after_sec > 0 and elapsed > self._disable_after_sec:
            self._log_blocked(now, "phase_disable_after", price, features, edge)
            return False
        phase_block = self._phase_entry_block(elapsed, state)
        if phase_block is not None:
            self._log_blocked(now, phase_block, price, features, edge)
            return False

        # Cooldown between trade signals
        if self._last_signal_ts > 0 and now - self._last_signal_ts < self._cooldown_sec:
            self._log_blocked(now, "cooldown", price, features, edge)
            return False

        if self._last_entry_ts > 0 and now - self._last_entry_ts < self._min_reentry_gap_sec:
            self._log_blocked(now, "min_reentry_gap", price, features, edge)
            return False

        if not self._entry_rearmed:
            self._log_blocked(now, "waiting_rearm", price, features, edge)
            return False

        if price < self._min_entry_price:
            self._log_blocked(now, "min_entry_price", price, features, edge)
            return False

        # Max entry price
        if price > self._max_entry_price:
            self._log_blocked(now, "max_entry_price", price, features, edge)
            return False

        # Edge magnitude
        if abs(edge) < self._edge_threshold:
            self._log_blocked(now, "edge_below_threshold", price, features, edge)
            return False

        # Edge persistence: edge must have been above threshold for N ms
        if not self._check_persistence(now, edge):
            self._log_blocked(now, "waiting_persistence", price, features, edge)
            return False

        direction = "up" if edge > 0 else "down"

        state.target_side = direction
        self._entry_edge = edge
        self._last_signal_ts = now
        self._entry_rearmed = False

        log.info(
            "EDGE SIGNAL: dir=%s edge=%.4f | ret_2s=%.4f ret_5s=%.4f vel=%.2f "
            "flow=%.3f | btc=%.1f age=%.0fms",
            direction.upper(), edge,
            features.ret_2s, features.ret_5s, features.velocity,
            features.flow_imbalance,
            features.btc_price, features.data_age_ms,
        )
        return True

    def check_edge_exit(self, state: MonitorState) -> Optional[str]:
        """Exit on edge reversal, decay, or max hold time."""
        if not self._started or not state.bought:
            return None

        now = time.time()

        # Time-based exit: sell after max_hold_sec
        if self._entry_ts > 0 and now - self._entry_ts > self._max_hold_sec:
            self._on_exit(now)
            return "max_hold"

        features = self._get_features()
        if features is None:
            return None

        edge = self._compute_edge(features)

        # Edge reversed direction
        if state.target_side == "up" and edge < -self._edge_exit_threshold:
            self._on_exit(now)
            return "edge_reversed"
        if state.target_side == "down" and edge > self._edge_exit_threshold:
            self._on_exit(now)
            return "edge_reversed"

        # Edge decayed below exit fraction. Allow a short grace period right after
        # entry so we do not churn on tiny post-fill wobble, while still letting
        # true reversals exit immediately.
        if (
            self._entry_ts > 0
            and self._edge_decay_grace_ms > 0
            and (now - self._entry_ts) * 1000 < self._edge_decay_grace_ms
        ):
            return None

        if abs(edge) < self._edge_exit_threshold:
            self._on_exit(now)
            return "edge_decayed"

        return None

    def _on_exit(self, now: float) -> None:
        """Reset state after exit for next trade."""
        self._last_signal_ts = now
        self._entry_ts = 0
        self._edge_history.clear()

    def on_buy_confirmed(self, entry_ts: float) -> None:
        """Called when buy is confirmed to start hold timer."""
        self._entry_ts = entry_ts
        self._last_entry_ts = entry_ts

    def _get_features(self):
        """Reuse the latest feature snapshot until Binance receives a new tick."""
        latest_ts = self._feed.latest_ts
        if latest_ts > 0 and latest_ts == self._cached_features_ts:
            if self._cached_features is None:
                return None
            return replace(
                self._cached_features,
                data_age_ms=(time.time() - latest_ts) * 1000,
            )

        features = self._feed.compute_features()
        self._cached_features = features
        self._cached_features_ts = latest_ts
        return features

    def _check_persistence(self, now: float, current_edge: float) -> bool:
        """Edge must have stayed above threshold for persistence_ms."""
        self._prune_edge_history(now)
        if not self._edge_history:
            return True

        if len(self._edge_history) < 2:
            return False

        # All samples in persistence window must be same direction and above threshold
        direction = 1 if current_edge > 0 else -1
        for _, e in self._edge_history:
            if direction * e < self._edge_threshold:
                return False

        return True

    def _record_edge(self, now: float, edge: float) -> None:
        """Append a fresh edge sample and keep only the persistence window."""
        self._edge_history.append((now, edge))
        self._prune_edge_history(now)

    def _prune_edge_history(self, now: float) -> None:
        """Drop edge samples older than the persistence window."""
        cutoff = now - self._persistence_ms / 1000.0
        while self._edge_history and self._edge_history[0][0] < cutoff:
            self._edge_history.popleft()

    def _compute_edge(self, features) -> float:
        """predicted_up_delta = sum(beta_i * feature_i)"""
        beta = self._coefficients
        return (
            beta.get("ret_2s", 0) * features.ret_2s
            + beta.get("ret_5s", 0) * features.ret_5s
            + beta.get("velocity", 0) * features.velocity
            + beta.get("abs_vel", 0) * features.abs_vel
        )

    def _maybe_rearm_entry(self, edge: float) -> None:
        """Allow a new entry only after the edge has cooled below the rearm threshold."""
        if self._entry_rearmed:
            return
        if self._edge_rearm_threshold <= 0:
            self._entry_rearmed = True
            return
        if abs(edge) <= self._edge_rearm_threshold:
            self._entry_rearmed = True

    def _phase_entry_block(self, elapsed: float, state: MonitorState) -> Optional[str]:
        """Apply time-bucketed entry caps inside the window."""
        if self._window_start_epoch <= 0:
            return None
        if self._max_entries_phase_one is not None and self._phase_one_sec > 0 and elapsed <= self._phase_one_sec:
            count_phase_one = sum(
                1 for ts in state.entry_timestamps
                if ts - self._window_start_epoch <= self._phase_one_sec
            )
            if count_phase_one >= self._max_entries_phase_one:
                return "phase_one_cap"

        if self._max_entries_phase_two is not None and self._phase_two_sec > 0 and elapsed <= self._phase_two_sec:
            count_phase_two = sum(
                1 for ts in state.entry_timestamps
                if ts - self._window_start_epoch <= self._phase_two_sec
            )
            if count_phase_two >= self._max_entries_phase_two:
                return "phase_two_cap"

        return None

    def _log_blocked(self, now: float, reason: str, price: float, features, edge: float) -> None:
        """Emit a low-frequency diagnostic log for non-triggered entry decisions."""
        if now - self._last_diag_log_ts < 5.0:
            return
        self._last_diag_log_ts = now
        log.info(
            "ENTRY BLOCKED: reason=%s edge=%.4f price=%.4f | ret_2s=%.4f "
            "ret_5s=%.4f vel=%.2f flow=%.3f | btc=%.1f age=%.0fms",
            reason,
            edge,
            price,
            features.ret_2s,
            features.ret_5s,
            features.velocity,
            features.flow_imbalance,
            features.btc_price,
            features.data_age_ms,
        )
