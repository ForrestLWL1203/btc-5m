"""MonitorState — mutable state shared between callbacks and the monitoring loop.

Extracted from monitor.py to avoid circular imports between strategies and monitor.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class MonitorState:
    """Mutable state shared between callbacks and the main loop."""

    bought: bool = False
    holding_size: float = 0.0  # shares held
    entry_price: float = 0.0
    exit_triggered: bool = False
    entry_count: int = 0  # total successful entries this window
    entry_timestamps: list[float] = field(default_factory=list)  # confirmed entry times (epoch seconds)
    latest_midpoint: Optional[float] = None
    latest_midpoint_received_at: Optional[float] = None
    buy_blocked_window_cap: bool = False  # blocked because max_entries_per_window was reached
    target_side: Optional[str] = None  # optional strategy-specific side override ("up"/"down")
    target_entry_price: Optional[float] = None  # strategy-computed token price to use for fills/logging
    signal_reference_price: Optional[float] = None  # theoretical target-leg price derived from the UP-leg signal
    target_max_entry_price: Optional[float] = None  # strategy-adjusted cap for the active signal
    target_signal_strength: Optional[float] = None
    target_past_signal_strength: Optional[float] = None
    target_active_theta_pct: Optional[float] = None
    target_remaining_sec: Optional[float] = None
    entry_amount: float = 0.0
    entry_avg_price: float = 0.0
    stop_loss_triggered: bool = False
    stop_loss_attempted: bool = False
    stop_loss_price: Optional[float] = None
    last_stop_loss_check_key: Optional[tuple] = None
    last_stop_loss_check_logged_at: float = 0.0
    stop_loss_check_logged_reasons: set[str] = field(default_factory=set)
    last_entry_check_side: Optional[str] = None  # target side for the last entry-band check
    last_entry_check_best_ask: Optional[float] = None  # target ask used by the last entry-band check
    snapshot_entry_check_logged: bool = False
    last_signal_eval_key: Optional[tuple] = None
    last_signal_eval_logged_at: float = 0.0
    last_depth_skip_key: Optional[tuple] = None
    last_depth_skip_logged_at: float = 0.0
    depth_skip_count: int = 0
    depth_skip_first_logged: bool = False
    depth_skip_last_reason: Optional[str] = None
    depth_skip_min_best_ask: Optional[float] = None
    depth_skip_max_best_ask: Optional[float] = None
    depth_skip_min_entry_ask: Optional[float] = None
    depth_skip_max_entry_ask: Optional[float] = None
    depth_skip_max_notional: float = 0.0
    entry_replay_check_count: int = 0
    entry_replay_signal_count: int = 0
    entry_replay_buy_signal_count: int = 0
    entry_replay_min_leading_ask: Optional[float] = None
    entry_replay_max_leading_ask: Optional[float] = None
    entry_replay_min_best_ask: Optional[float] = None
    entry_replay_max_best_ask: Optional[float] = None
    entry_replay_min_selected_ask: Optional[float] = None
    entry_replay_max_selected_ask: Optional[float] = None
    entry_replay_max_depth_notional: float = 0.0
    entry_replay_min_best_ask_age_ms: Optional[int] = None
    entry_replay_max_best_ask_age_ms: Optional[int] = None
    stop_replay_check_count: int = 0
    stop_replay_triggered_count: int = 0
    stop_replay_missing_or_stale_bid_count: int = 0
    stop_replay_insufficient_depth_count: int = 0
    stop_replay_min_best_bid: Optional[float] = None
    stop_replay_max_best_bid: Optional[float] = None
    stop_replay_min_selected_bid: Optional[float] = None
    stop_replay_max_selected_bid: Optional[float] = None
    stop_replay_max_bid_shares_available: float = 0.0
    stop_replay_min_best_bid_age_ms: Optional[int] = None
    stop_replay_max_best_bid_age_ms: Optional[int] = None
    trade_lock: asyncio.Lock = None  # prevents concurrent buy/sell from WS callbacks
    started: bool = False  # set True when window officially starts — prevents pre-start trades

    # Risk management (UTC+8 daily reset)
    daily_wins: int = 0
    daily_losses: int = 0
    consecutive_losses: int = 0
    daily_realized_pnl: float = 0.0
    consecutive_loss_amount: float = 0.0
    windows_to_skip: int = 0
    last_reset_date: Optional[str] = None  # "YYYY-MM-DD" in UTC+8, for detecting date change
    min_trades_for_eval: int = 30  # minimum trades before evaluating win rate

    def __post_init__(self):
        self.trade_lock = asyncio.Lock()
