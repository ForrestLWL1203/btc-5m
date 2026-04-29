"""Mid-window crowd-following strategy for BTC 5-minute markets."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from polybot.core import config
from polybot.core.state import MonitorState
from polybot.market.binance import BinancePriceFeed
from polybot.market.polymarket_rtds import PolymarketRTDSPriceFeed
from polybot.market.series import MarketSeries
from .base import Strategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ReverseFilterResult:
    history_ready: bool
    move_pct: Optional[float]
    triggered: bool


class CrowdM1Strategy(Strategy):
    """Buy the higher-best-ask Polymarket leg during the mid-window band.

    The strategy scans between ``entry_elapsed_sec`` and
    ``entry_elapsed_sec + entry_timeout_sec``. It buys the side with the higher
    best ask only when the higher ask leads the lower ask by at least
    ``min_ask_gap``. Optional BTC confirmation is retained for compatibility,
    but the active M1 config keeps it disabled.
    """

    dynamic_side = True
    snapshot_entry = True

    def __init__(
        self,
        series: MarketSeries,
        entry_elapsed_sec: float = 120.0,
        entry_timeout_sec: float = 60.0,
        min_leading_ask: float = 0.0,
        min_ask_gap: float = 0.16,
        max_entry_price: float = 0.75,
        btc_direction_confirm: bool = True,
        btc_reverse_filter_enabled: bool = False,
        btc_reverse_lookback_sec: float = 20.0,
        btc_reverse_min_move_pct: float = 0.02,
        btc_price_feed_source: str = "binance",
        open_price_max_wait_sec: float = 30.0,
        max_book_age_sec: Optional[float] = config.FAK_RETRY_MAX_BEST_ASK_AGE_SEC,
    ):
        self._series = series
        self._entry_elapsed_sec = entry_elapsed_sec
        self._entry_timeout_sec = entry_timeout_sec
        self._min_leading_ask = min_leading_ask
        self._min_ask_gap = min_ask_gap
        self._max_entry_price = max_entry_price
        self._btc_direction_confirm = btc_direction_confirm
        self._btc_reverse_filter_enabled = btc_reverse_filter_enabled
        self._btc_reverse_lookback_sec = btc_reverse_lookback_sec
        self._btc_reverse_min_move_pct = btc_reverse_min_move_pct
        self._btc_price_feed_source = btc_price_feed_source
        self._open_price_max_wait_sec = open_price_max_wait_sec
        self._max_book_age_sec = max_book_age_sec

        self._feed = self._build_price_feed(btc_price_feed_source)
        self._window_start_epoch: float = 0.0
        self._window_open_btc: Optional[float] = None
        self._started = False
        self._evaluated = False
        self._up_mid: Optional[float] = None
        self._down_mid: Optional[float] = None
        self._up_best_ask: Optional[float] = None
        self._down_best_ask: Optional[float] = None
        self._up_best_ask_age_sec: Optional[float] = None
        self._down_best_ask_age_sec: Optional[float] = None
        self._logged_skip_reasons: set[str] = set()
        self._logged_btc_reverse_filter_checks: set[tuple[bool, bool]] = set()

    @property
    def entry_start_remaining_sec(self) -> float:
        return max(0.0, self._series.slug_step - self._entry_elapsed_sec)

    @property
    def entry_end_remaining_sec(self) -> float:
        return max(0.0, self._series.slug_step - self._entry_elapsed_sec - self._entry_timeout_sec)

    @property
    def max_entry_price(self) -> float:
        return self._max_entry_price

    async def start(self) -> None:
        await self._feed.start()
        self._started = True
        log.debug(
            "CrowdM1Strategy started | entry_elapsed=%.0fs timeout=%.0fs | "
            "min_ask_gap=%.3f min_leading_ask=%.3f | max_entry=%.2f | "
            "btc_confirm=%s btc_reverse_filter=%s lookback=%.0fs min_reverse=%.3f%% btc_feed=%s",
            self._entry_elapsed_sec,
            self._entry_timeout_sec,
            self._min_ask_gap,
            self._min_leading_ask,
            self._max_entry_price,
            self._btc_direction_confirm,
            self._btc_reverse_filter_enabled,
            self._btc_reverse_lookback_sec,
            self._btc_reverse_min_move_pct,
            self._btc_price_feed_source,
        )

    async def stop(self) -> None:
        await self._feed.stop()
        self._started = False

    def set_window_start(self, epoch: float) -> None:
        self._window_start_epoch = epoch
        self._window_open_btc = None
        self._evaluated = False
        self._up_mid = None
        self._down_mid = None
        self._up_best_ask = None
        self._down_best_ask = None
        self._up_best_ask_age_sec = None
        self._down_best_ask_age_sec = None
        self._logged_skip_reasons = set()
        self._logged_btc_reverse_filter_checks = set()

    async def preload_open_btc(self, epoch: float) -> None:
        if not self._btc_direction_confirm:
            return
        if self._window_open_btc is not None:
            return
        cached = self._feed.first_price_at_or_after(
            epoch,
            max_forward_sec=self._open_price_max_wait_sec,
        )
        if cached is not None:
            return
        price = await self._feed.fetch_open_at(epoch)
        if price is not None:
            self._window_open_btc = price
            log.debug("OPEN_BTC_REST_SEEDED: epoch=%.0f price=%.2f", epoch, price)

    def set_market_snapshot(
        self,
        *,
        up_mid: Optional[float],
        down_mid: Optional[float],
        up_best_ask: Optional[float] = None,
        down_best_ask: Optional[float] = None,
        up_best_ask_age_sec: Optional[float] = None,
        down_best_ask_age_sec: Optional[float] = None,
    ) -> None:
        self._up_mid = up_mid
        self._down_mid = down_mid
        self._up_best_ask = up_best_ask
        self._down_best_ask = down_best_ask
        self._up_best_ask_age_sec = up_best_ask_age_sec
        self._down_best_ask_age_sec = down_best_ask_age_sec

    def get_side(self, candles: Optional[list] = None) -> Optional[str]:
        return None

    def should_buy(self, price: float, state: MonitorState) -> bool:
        if not self._started or state.bought or self._evaluated:
            return False
        if self._window_start_epoch <= 0:
            return False

        now = time.time()
        elapsed = now - self._window_start_epoch
        if elapsed < self._entry_elapsed_sec:
            return False
        if elapsed > self._entry_elapsed_sec + self._entry_timeout_sec:
            self._evaluated = True
            self._log_decision_skip(reason="entry_timeout", elapsed=elapsed)
            return False

        if self._up_best_ask is None or self._down_best_ask is None:
            self._log_decision_skip(reason="missing_market_snapshot", elapsed=elapsed)
            return False

        up_mid = float(self._up_mid) if self._up_mid is not None else None
        down_mid = float(self._down_mid) if self._down_mid is not None else None
        up_best_ask = float(self._up_best_ask)
        down_best_ask = float(self._down_best_ask)
        if self._is_stale_book_age(self._up_best_ask_age_sec) or self._is_stale_book_age(self._down_best_ask_age_sec):
            self._log_decision_skip(
                reason="stale_cross_leg_book",
                elapsed=elapsed,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                up_best_ask_age_sec=self._up_best_ask_age_sec,
                down_best_ask_age_sec=self._down_best_ask_age_sec,
            )
            return False

        direction = "up" if up_best_ask >= down_best_ask else "down"
        leading_ask = up_best_ask if direction == "up" else down_best_ask
        ask_gap = abs(up_best_ask - down_best_ask)
        if ask_gap < self._min_ask_gap:
            self._log_decision_skip(
                reason="ask_gap_below_min",
                elapsed=elapsed,
                direction=direction,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                ask_gap=ask_gap,
                leading_ask=leading_ask,
            )
            return False

        if leading_ask < self._min_leading_ask:
            self._log_decision_skip(
                reason="leading_ask_below_min",
                elapsed=elapsed,
                direction=direction,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                ask_gap=ask_gap,
                leading_ask=leading_ask,
            )
            return False

        if leading_ask > self._max_entry_price:
            self._log_decision_skip(
                reason="leading_ask_above_max_entry",
                elapsed=elapsed,
                direction=direction,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                ask_gap=ask_gap,
                leading_ask=leading_ask,
            )
            return False

        current_btc = self._feed.latest_price
        open_btc = self._ensure_window_open_btc()
        if self._btc_direction_confirm:
            if current_btc is None or open_btc is None or open_btc <= 0:
                self._log_decision_skip(
                    reason="missing_btc_reference",
                    elapsed=elapsed,
                    direction=direction,
                    up_mid=up_mid,
                    down_mid=down_mid,
                    up_best_ask=up_best_ask,
                    down_best_ask=down_best_ask,
                    ask_gap=ask_gap,
                    leading_ask=leading_ask,
                    open_btc=open_btc,
                    current_btc=current_btc,
                )
                return False
            btc_up = current_btc > open_btc
            if (direction == "up") != btc_up:
                self._log_decision_skip(
                    reason="btc_direction_mismatch",
                    elapsed=elapsed,
                    direction=direction,
                    up_mid=up_mid,
                    down_mid=down_mid,
                    up_best_ask=up_best_ask,
                    down_best_ask=down_best_ask,
                    ask_gap=ask_gap,
                    leading_ask=leading_ask,
                    open_btc=open_btc,
                    current_btc=current_btc,
                )
                return False

        reverse_filter = self._recent_btc_reverse_filter(now, direction)
        if reverse_filter is not None and not reverse_filter.history_ready:
            self._log_decision_skip(
                reason="btc_reverse_history_not_ready",
                elapsed=elapsed,
                direction=direction,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                ask_gap=ask_gap,
                leading_ask=leading_ask,
                current_btc=current_btc,
            )
            return False
        if reverse_filter is not None and reverse_filter.triggered:
            self._log_decision_skip(
                reason="btc_recent_reverse_move",
                elapsed=elapsed,
                direction=direction,
                up_mid=up_mid,
                down_mid=down_mid,
                up_best_ask=up_best_ask,
                down_best_ask=down_best_ask,
                ask_gap=ask_gap,
                leading_ask=leading_ask,
                current_btc=current_btc,
                btc_reverse_move_pct=reverse_filter.move_pct,
            )
            return False

        remaining = self._series.slug_step - elapsed
        state.target_side = direction
        state.signal_reference_price = leading_ask
        state.target_max_entry_price = self._max_entry_price
        state.target_signal_strength = ask_gap / self._min_ask_gap if self._min_ask_gap > 0 else None
        state.target_past_signal_strength = None
        state.target_active_theta_pct = None
        state.target_remaining_sec = remaining
        self._evaluated = True

        return True

    def _log_decision_skip(
        self,
        *,
        reason: str,
        elapsed: float,
        direction: Optional[str] = None,
        up_mid: Optional[float] = None,
        down_mid: Optional[float] = None,
        up_best_ask: Optional[float] = None,
        down_best_ask: Optional[float] = None,
        ask_gap: Optional[float] = None,
        leading_ask: Optional[float] = None,
        open_btc: Optional[float] = None,
        current_btc: Optional[float] = None,
        up_best_ask_age_sec: Optional[float] = None,
        down_best_ask_age_sec: Optional[float] = None,
        btc_reverse_move_pct: Optional[float] = None,
    ) -> None:
        if reason in self._logged_skip_reasons:
            return
        self._logged_skip_reasons.add(reason)

        remaining = self._series.slug_step - elapsed
        log.info(
            "M1_DECISION_SKIP: reason=%s elapsed=%.1fs remaining=%.1fs dir=%s "
            "up_best_ask=%s down_best_ask=%s leading_ask=%s ask_gap=%s min_ask_gap=%.3f min_leading_ask=%.3f "
            "up_mid=%s down_mid=%s btc_open=%s btc_now=%s max_entry=%.3f "
            "up_best_ask_age_ms=%s down_best_ask_age_ms=%s btc_reverse_move_pct=%s",
            reason,
            elapsed,
            remaining,
            direction.upper() if direction else None,
            self._fmt_price(up_best_ask, digits=3),
            self._fmt_price(down_best_ask, digits=3),
            self._fmt_price(leading_ask, digits=3),
            self._fmt_price(ask_gap, digits=3),
            self._min_ask_gap,
            self._min_leading_ask,
            self._fmt_price(up_mid, digits=3),
            self._fmt_price(down_mid, digits=3),
            self._fmt_price(open_btc, digits=1),
            self._fmt_price(current_btc, digits=1),
            self._max_entry_price,
            self._fmt_age_ms(up_best_ask_age_sec),
            self._fmt_age_ms(down_best_ask_age_sec),
            self._fmt_price(btc_reverse_move_pct, digits=4),
        )

    @staticmethod
    def _fmt_price(value: Optional[float], *, digits: int) -> Optional[str]:
        if value is None:
            return None
        return f"{float(value):.{digits}f}"

    def _is_stale_book_age(self, age_sec: Optional[float]) -> bool:
        return (
            self._max_book_age_sec is not None
            and age_sec is not None
            and age_sec > self._max_book_age_sec
        )

    @staticmethod
    def _fmt_age_ms(value: Optional[float]) -> Optional[int]:
        if value is None:
            return None
        return round(float(value) * 1000)

    def _ensure_window_open_btc(self) -> Optional[float]:
        if self._window_open_btc is not None:
            return self._window_open_btc
        self._window_open_btc = self._feed.first_price_at_or_after(
            self._window_start_epoch,
            max_forward_sec=self._open_price_max_wait_sec,
        )
        return self._window_open_btc

    @staticmethod
    def _build_price_feed(source: str):
        normalized = source.lower()
        if normalized == "polymarket_rtds":
            return PolymarketRTDSPriceFeed(symbol="btcusdt")
        if normalized == "binance":
            return BinancePriceFeed(symbol="btcusdt")
        raise ValueError("Unsupported BTC price feed source: " + source)

    def _recent_btc_reverse_filter(self, now: float, direction: str) -> Optional[_ReverseFilterResult]:
        if not self._btc_reverse_filter_enabled:
            return None
        if self._btc_reverse_lookback_sec <= 0 or self._btc_reverse_min_move_pct <= 0:
            return None

        past_btc = self._feed.price_at_or_before(now - self._btc_reverse_lookback_sec)
        current_btc = self._feed.price_at_or_before(now)
        history_ready = past_btc is not None and current_btc is not None and past_btc > 0
        move_pct: Optional[float] = None
        if history_ready:
            move_pct = (float(current_btc) / float(past_btc) - 1.0) * 100.0
        self._log_btc_reverse_filter_check(
            direction=direction,
            past_btc=past_btc,
            current_btc=current_btc,
            move_pct=move_pct,
            history_ready=history_ready,
        )
        if not history_ready:
            return _ReverseFilterResult(history_ready=False, move_pct=None, triggered=False)

        triggered = False
        if direction == "up" and move_pct <= -self._btc_reverse_min_move_pct:
            triggered = True
        elif direction == "down" and move_pct >= self._btc_reverse_min_move_pct:
            triggered = True
        return _ReverseFilterResult(history_ready=True, move_pct=move_pct, triggered=triggered)

    def _log_btc_reverse_filter_check(
        self,
        *,
        direction: str,
        past_btc: Optional[float],
        current_btc: Optional[float],
        move_pct: Optional[float],
        history_ready: bool,
    ) -> None:
        triggered = False
        if move_pct is not None:
            if direction == "up":
                triggered = move_pct <= -self._btc_reverse_min_move_pct
            elif direction == "down":
                triggered = move_pct >= self._btc_reverse_min_move_pct
        log_key = (history_ready, triggered)
        if log_key in self._logged_btc_reverse_filter_checks:
            return
        self._logged_btc_reverse_filter_checks.add(log_key)
        log.info(
            "BTC_REVERSE_FILTER_CHECK: source=%s dir=%s lookback_sec=%.0f min_reverse=%.3f%% "
            "history_ready=%s lookback_btc=%s current_btc=%s move_pct=%s triggered=%s",
            self._btc_price_feed_source,
            direction.upper(),
            self._btc_reverse_lookback_sec,
            self._btc_reverse_min_move_pct,
            history_ready,
            self._fmt_price(past_btc, digits=1),
            self._fmt_price(current_btc, digits=1),
            self._fmt_price(move_pct, digits=4),
            triggered,
        )
