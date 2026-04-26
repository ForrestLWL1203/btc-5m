"""Paired-data win-rate and EV analysis for the current paired-window strategy.

Consumes collect_data.py JSONL (Binance BTC ticks + Poly UP/DOWN prices +
per-window outcome records) and simulates the paired-window strategy across a
parameter grid using real Poly prices to compute true expected value
(WinRate - EntryPrice).

Usage:
    python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<ts>.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class BtcTick:
    ts: float
    price: float


@dataclass
class PolyUp:
    ts: float
    mid: float
    bid: float
    ask: float
    token: str  # "up" or "down"


@dataclass
class Window:
    label: str
    start_epoch: float
    end_epoch: float
    open_price: float
    close_price: float
    direction: str  # "up" or "down"
    btc: list[BtcTick] = field(default_factory=list)
    poly: list[PolyUp] = field(default_factory=list)
    btc_ts: list[float] = field(default_factory=list)
    btc_px: list[float] = field(default_factory=list)
    up_ask_lookup: "SeriesLookup | None" = None
    down_ask_lookup: "SeriesLookup | None" = None
    up_bid_lookup: "SeriesLookup | None" = None
    down_bid_lookup: "SeriesLookup | None" = None
    up_bid_series: list[tuple[float, float]] = field(default_factory=list)
    down_bid_series: list[tuple[float, float]] = field(default_factory=list)
    up_bid_ts: list[float] = field(default_factory=list)
    down_bid_ts: list[float] = field(default_factory=list)
    btc_series: list[tuple[float, float]] = field(default_factory=list)
    up_tick_size: float = 0.01
    down_tick_size: float = 0.01

    @property
    def up_wins(self) -> bool:
        return self.direction == "up"

    def prepare(self) -> None:
        """Pre-compute lookup structures once per window.

        The original script rebuilt these arrays and SeriesLookup instances for
        every parameter combination. On multi-hundred-MB JSONL files that turns
        into the dominant cost.
        """
        self.btc_ts = [t.ts for t in self.btc]
        self.btc_px = [t.price for t in self.btc]
        self.btc_series = list(zip(self.btc_ts, self.btc_px))
        up_ask = [(p.ts, p.ask) for p in self.poly if p.token == "up"]
        down_ask = [(p.ts, p.ask) for p in self.poly if p.token == "down"]
        self.up_bid_series = [(p.ts, p.bid) for p in self.poly if p.token == "up"]
        self.down_bid_series = [(p.ts, p.bid) for p in self.poly if p.token == "down"]
        self.up_bid_ts = [ts for ts, _ in self.up_bid_series]
        self.down_bid_ts = [ts for ts, _ in self.down_bid_series]
        self.up_ask_lookup = SeriesLookup(up_ask)
        self.down_ask_lookup = SeriesLookup(down_ask)
        self.up_bid_lookup = SeriesLookup(self.up_bid_series)
        self.down_bid_lookup = SeriesLookup(self.down_bid_series)
        self.up_tick_size = _estimate_tick_size([ask for _, ask in up_ask])
        self.down_tick_size = _estimate_tick_size([ask for _, ask in down_ask])


class SeriesLookup:
    def __init__(self, pairs: list[tuple[float, float]]):
        pairs = sorted(pairs, key=lambda x: x[0])
        self._ts = [p[0] for p in pairs]
        self._vals = [p[1] for p in pairs]

    def at(self, ts: float, max_lookback: float = 3.0):
        if not self._ts:
            return None
        idx = bisect_right(self._ts, ts) - 1
        if idx < 0:
            return None
        if ts - self._ts[idx] > max_lookback:
            return None
        return self._vals[idx]


def _estimate_tick_size(values: list[float]) -> float:
    """Estimate book tick size from observed ask values."""
    uniq = sorted({round(v, 6) for v in values if v is not None and 0 < v < 1})
    min_diff = None
    for i in range(1, len(uniq)):
        diff = round(uniq[i] - uniq[i - 1], 6)
        if diff <= 0:
            continue
        if min_diff is None or diff < min_diff:
            min_diff = diff
    return min_diff if min_diff is not None else 0.01


def parse_file(path: str) -> list[Window]:
    """Split JSONL into per-window segments delimited by outcome records.

    The original implementation re-scanned every tick and every Poly update for
    every outcome record, which becomes quadratic on large files. Here we bucket
    records by 5-minute window during the single file scan, then materialize the
    windows in outcome order.
    """
    outcomes: list[dict] = []
    ticks_by_end: dict[int, list[BtcTick]] = defaultdict(list)
    polys_by_end: dict[int, list[PolyUp]] = defaultdict(list)

    def _window_end_from_ts(ts: float) -> int:
        return int(ts // 300) * 300 + 300

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            src = rec.get("src")
            if src == "binance":
                tick = BtcTick(ts=rec["ts"], price=float(rec["price"]))
                ticks_by_end[_window_end_from_ts(tick.ts)].append(tick)
            elif src == "poly":
                poly = PolyUp(
                    ts=rec["ts"],
                    mid=float(rec["mid"]),
                    bid=float(rec.get("bid") or rec["mid"]),
                    ask=float(rec.get("ask") or rec["mid"]),
                    token=rec["token"],
                )
                polys_by_end[_window_end_from_ts(poly.ts)].append(poly)
            elif src == "outcome":
                outcomes.append(rec)

    windows: list[Window] = []
    for oc in sorted(outcomes, key=lambda x: float(x["ts"])):
        t_end = float(oc["ts"])
        window_end = int(t_end // 300) * 300
        window_start = window_end - 300
        w_ticks = ticks_by_end.get(window_end, [])
        w_polys = polys_by_end.get(window_end, [])
        if not w_ticks:
            continue
        window = Window(
            label=oc.get("window", ""),
            start_epoch=window_start,
            end_epoch=window_end,
            open_price=float(oc.get("open") or w_ticks[0].price),
            close_price=float(oc.get("close") or w_ticks[-1].price),
            direction=oc.get("direction") or (
                "up" if w_ticks[-1].price > w_ticks[0].price else "down"
            ),
            btc=w_ticks,
            poly=w_polys,
        )
        window.prepare()
        windows.append(window)
    return windows


def wilson_lower(wins: int, n: int, z: float = 1.96) -> float:
    if n == 0:
        return 0.0
    phat = wins / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))
    return max(0.0, (center - margin) / denom)


def simulate(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int = 10,
    max_data_age: float = 2.0,
    max_entry_price: float = 0.95,
    entry_delay_sec: float = 0.0,
    min_entry_price: float = 0.0,
    price_hint_buffer_ticks: float = 0.0,
) -> dict:
    """Simulate strategy with optional entry fill delay.

    Signal (direction + threshold clear) detected at time T.
    Actual fill happens at T + entry_delay_sec:
      - Entry price = Poly ask at T+delay (slippage modeled)
      - If price at T+delay exceeds max_entry_price cap, order is rejected
        (emulates FOK rejection or pre-fill price check)
      - Direction is committed at T (cannot change after signal)
    """
    entries = 0
    wins = 0
    entry_prices: list[float] = []
    entry_prices_at_signal: list[float] = []
    slippage: list[float] = []
    winning_payoffs: list[float] = []
    losing_payoffs: list[float] = []
    up_entries = 0
    up_wins = 0
    down_entries = 0
    down_wins = 0
    skipped_price_cap = 0
    skipped_no_quote = 0
    skipped_band = 0
    direction_flipped = 0  # BTC reversed in delay window

    for w in windows:
        entry = _find_window_entry(
            w,
            theta_pct=theta_pct,
            lo_rem=lo_rem,
            hi_rem=hi_rem,
            persistence_sec=persistence_sec,
            max_data_age=max_data_age,
            max_entry_price=max_entry_price,
            entry_delay_sec=entry_delay_sec,
            min_entry_price=min_entry_price,
            price_hint_buffer_ticks=price_hint_buffer_ticks,
        )
        if entry is None:
            continue
        skipped_no_quote += entry["skipped_no_quote"]
        skipped_price_cap += entry["skipped_price_cap"]
        skipped_band += entry["skipped_band"]
        if entry["fill_price"] is None:
            continue

        direction_up = entry["direction_up"]
        fill_price = entry["fill_price"]
        sig_price = entry["sig_price"]

        if entry["direction_flipped"]:
            direction_flipped += 1

        entries += 1
        entry_prices.append(fill_price)
        if sig_price is not None:
            entry_prices_at_signal.append(sig_price)
            slippage.append(fill_price - sig_price)
        win = (direction_up and w.up_wins) or (not direction_up and not w.up_wins)
        if win:
            wins += 1
            winning_payoffs.append(1.0 - fill_price)
        else:
            losing_payoffs.append(-fill_price)
        if direction_up:
            up_entries += 1
            if win:
                up_wins += 1
        else:
            down_entries += 1
            if win:
                down_wins += 1

    ev_per_trade = (
        (sum(winning_payoffs) + sum(losing_payoffs)) / entries if entries else 0.0
    )
    avg_entry = sum(entry_prices) / len(entry_prices) if entry_prices else 0.0
    avg_slip = sum(slippage) / len(slippage) if slippage else 0.0
    return {
        "entries": entries,
        "wins": wins,
        "win_rate": wins / entries if entries else 0.0,
        "win_rate_ci_lo": wilson_lower(wins, entries),
        "avg_entry_price": avg_entry,
        "avg_slippage": avg_slip,
        "avg_win_payoff": sum(winning_payoffs) / len(winning_payoffs) if winning_payoffs else 0.0,
        "avg_loss_payoff": sum(losing_payoffs) / len(losing_payoffs) if losing_payoffs else 0.0,
        "ev_per_trade": ev_per_trade,
        "up": (up_entries, up_wins),
        "down": (down_entries, down_wins),
        "skipped_price_cap": skipped_price_cap,
        "skipped_no_quote": skipped_no_quote,
        "direction_flipped": direction_flipped,
        "skipped_band": skipped_band,
    }


def simulate_stop_loss(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int = 10,
    max_data_age: float = 2.0,
    max_entry_price: float = 0.95,
    entry_delay_sec: float = 0.0,
    min_entry_price: float = 0.0,
    price_hint_buffer_ticks: float = 0.0,    stop_loss_pct: float = 0.10,
    stop_confirm_sec: float = 5.0,
    min_hold_sec: float = 30.0,
    sell_buffer_ticks: float = 1.0,
    btc_invalidation_mode: str = "reverse_theta",
    amount: float = 1.0,
) -> dict:
    """Compare hold-to-resolution with a token+BTC confluence stop-loss.

    PnL is modeled for a fixed USD stake per trade.  Entry uses the same target
    leg ask logic as live BUY; stop exit uses target leg best_bid minus a sell
    buffer to mirror BUY's best_ask plus buffer.
    """
    entries = 0
    resolution_wins = 0
    stopped = 0
    stopped_would_win = 0
    stopped_would_loss = 0
    stop_pnls: list[float] = []
    no_stop_pnl = 0.0
    stop_pnl = 0.0
    entry_prices: list[float] = []
    exit_prices: list[float] = []

    for w in windows:
        entry = _find_window_entry(
            w,
            theta_pct=theta_pct,
            lo_rem=lo_rem,
            hi_rem=hi_rem,
            persistence_sec=persistence_sec,
            max_data_age=max_data_age,
            max_entry_price=max_entry_price,
            entry_delay_sec=entry_delay_sec,
            min_entry_price=min_entry_price,
            price_hint_buffer_ticks=price_hint_buffer_ticks,        )
        if entry is None or entry["fill_price"] is None:
            continue

        entries += 1
        direction_up = entry["direction_up"]
        fill_ts = entry["fill_ts"]
        entry_price = entry["fill_price"]
        entry_prices.append(entry_price)
        resolution_win = (direction_up and w.up_wins) or (
            not direction_up and not w.up_wins
        )
        if resolution_win:
            resolution_wins += 1

        no_stop_trade_pnl = (
            (amount / entry_price) - amount if resolution_win else -amount
        )
        no_stop_pnl += no_stop_trade_pnl

        stop_exit = _find_stop_exit(
            w=w,
            direction_up=direction_up,
            fill_ts=fill_ts,
            entry_price=entry_price,
            reference_move_pct=_move_pct_at(w, fill_ts),
            theta_pct=theta_pct,
            max_data_age=max_data_age,
            stop_loss_pct=stop_loss_pct,
            stop_confirm_sec=stop_confirm_sec,
            min_hold_sec=min_hold_sec,
            sell_buffer_ticks=sell_buffer_ticks,
            btc_invalidation_mode=btc_invalidation_mode,
        )
        if stop_exit is None:
            stop_pnl += no_stop_trade_pnl
            continue

        stopped += 1
        if resolution_win:
            stopped_would_win += 1
        else:
            stopped_would_loss += 1
        exit_price = stop_exit["exit_price"]
        exit_prices.append(exit_price)
        trade_pnl = (amount / entry_price) * exit_price - amount
        stop_pnls.append(trade_pnl)
        stop_pnl += trade_pnl

    return {
        "entries": entries,
        "resolution_wins": resolution_wins,
        "resolution_win_rate": resolution_wins / entries if entries else 0.0,
        "avg_entry_price": sum(entry_prices) / len(entry_prices) if entry_prices else 0.0,
        "no_stop_pnl": no_stop_pnl,
        "no_stop_ev": no_stop_pnl / entries if entries else 0.0,
        "stop_pnl": stop_pnl,
        "stop_ev": stop_pnl / entries if entries else 0.0,
        "delta_pnl": stop_pnl - no_stop_pnl,
        "stopped": stopped,
        "stopped_rate": stopped / entries if entries else 0.0,
        "stopped_would_win": stopped_would_win,
        "stopped_would_loss": stopped_would_loss,
        "avg_stop_exit": sum(exit_prices) / len(exit_prices) if exit_prices else 0.0,
        "avg_stop_pnl": sum(stop_pnls) / len(stop_pnls) if stop_pnls else 0.0,
    }


def _find_stop_exit(
    w: Window,
    direction_up: bool,
    fill_ts: float,
    entry_price: float,
    reference_move_pct: float | None,
    theta_pct: float,
    max_data_age: float,
    stop_loss_pct: float,
    stop_confirm_sec: float,
    min_hold_sec: float,
    sell_buffer_ticks: float,
    btc_invalidation_mode: str,
) -> dict | None:
    """Find first confluence stop exit after entry, if any."""
    if btc_invalidation_mode not in {"reverse_theta", "signal_half", "near_open"}:
        raise ValueError(f"unsupported btc_invalidation_mode={btc_invalidation_mode}")

    bid_lookup = w.up_bid_lookup if direction_up else w.down_bid_lookup
    tick_size = w.up_tick_size if direction_up else w.down_tick_size
    if bid_lookup is None:
        return None

    start_ts = fill_ts + min_hold_sec
    stop_bid = entry_price * (1.0 - stop_loss_pct)
    confirm_start: float | None = None
    start_idx = bisect_right(w.btc_ts, start_ts - 1e-9)

    for tick in w.btc[start_idx:]:
        if tick.ts >= w.end_epoch:
            break
        bid = bid_lookup.at(tick.ts, max_data_age)
        if bid is None or bid <= 0:
            confirm_start = None
            continue

        move_pct = (tick.price - w.open_price) / w.open_price * 100.0
        btc_invalid = _btc_stop_invalidated(
            direction_up=direction_up,
            move_pct=move_pct,
            reference_move_pct=reference_move_pct,
            theta_pct=theta_pct,
            mode=btc_invalidation_mode,
        )
        token_invalid = bid <= stop_bid

        if not (btc_invalid and token_invalid):
            confirm_start = None
            continue
        if confirm_start is None:
            confirm_start = tick.ts
            continue
        if tick.ts - confirm_start < stop_confirm_sec:
            continue

        buffered_exit = bid - tick_size * sell_buffer_ticks
        exit_price = max(0.0, math.floor(buffered_exit / tick_size) * tick_size)
        return {
            "exit_ts": tick.ts,
            "exit_bid": bid,
            "exit_price": exit_price,
            "btc_move_pct": move_pct,
        }
    return None


def _move_pct_at(w: Window, ts: float) -> float | None:
    idx = bisect_right(w.btc_ts, ts) - 1
    if idx < 0:
        return None
    return (w.btc_px[idx] - w.open_price) / w.open_price * 100.0


def _btc_stop_invalidated(
    direction_up: bool,
    move_pct: float,
    reference_move_pct: float | None,
    theta_pct: float,
    mode: str,
) -> bool:
    if mode == "reverse_theta":
        return move_pct <= -theta_pct if direction_up else move_pct >= theta_pct
    if mode == "signal_half":
        if reference_move_pct is None:
            return False
        threshold = reference_move_pct * 0.5
        return move_pct <= threshold if direction_up else move_pct >= threshold
    if mode == "near_open":
        return abs(move_pct) <= theta_pct
    raise ValueError(f"unsupported btc_invalidation_mode={mode}")


def _find_token_floor_exit(
    w: Window,
    direction_up: bool,
    fill_ts: float,
    entry_price: float,
    token_floor: float,
    min_hold_sec: float,
    sell_buffer_ticks: float,
    max_data_age: float = 2.0,
) -> dict | None:
    """Find first exit where target token bid falls below an absolute floor price.

    No BTC confirmation required — fires as soon as the bid is below token_floor
    after the minimum hold period.  The floor is an absolute price (e.g. 0.25),
    not a percentage drawdown from entry.
    """
    bid_lookup = w.up_bid_lookup if direction_up else w.down_bid_lookup
    tick_size = w.up_tick_size if direction_up else w.down_tick_size
    if bid_lookup is None:
        return None

    bid_ts = w.up_bid_ts if direction_up else w.down_bid_ts
    bid_series = w.up_bid_series if direction_up else w.down_bid_series
    start_ts = fill_ts + min_hold_sec

    start_idx = bisect_right(bid_ts, start_ts - 1e-9)
    for ts, bid in bid_series[start_idx:]:
        if ts >= w.end_epoch:
            break
        if bid <= 0:
            continue
        if bid <= token_floor:
            buffered_exit = bid - tick_size * sell_buffer_ticks
            exit_price = max(0.0, math.floor(buffered_exit / tick_size) * tick_size)
            return {"exit_ts": ts, "exit_bid": bid, "exit_price": exit_price}
    return None


def simulate_token_floor(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int = 5,
    max_data_age: float = 2.0,
    max_entry_price: float = 0.61,
    entry_delay_sec: float = 1.0,
    min_entry_price: float = 0.0,
    price_hint_buffer_ticks: float = 1.0,    token_floor: float = 0.25,
    min_hold_sec: float = 30.0,
    sell_buffer_ticks: float = 1.0,
    amount: float = 1.0,
) -> dict:
    """Compare hold-to-resolution with a pure token bid floor stop-loss."""
    entries = 0
    resolution_wins = 0
    stopped = 0
    stopped_would_win = 0
    stopped_would_loss = 0
    stop_pnls: list[float] = []
    no_stop_pnl = 0.0
    stop_pnl = 0.0
    entry_prices: list[float] = []
    exit_prices: list[float] = []

    for w in windows:
        entry = _find_window_entry(
            w,
            theta_pct=theta_pct,
            lo_rem=lo_rem,
            hi_rem=hi_rem,
            persistence_sec=persistence_sec,
            max_data_age=max_data_age,
            max_entry_price=max_entry_price,
            entry_delay_sec=entry_delay_sec,
            min_entry_price=min_entry_price,
            price_hint_buffer_ticks=price_hint_buffer_ticks,        )
        if entry is None or entry["fill_price"] is None:
            continue

        entries += 1
        direction_up = entry["direction_up"]
        fill_ts = entry["fill_ts"]
        entry_price = entry["fill_price"]
        entry_prices.append(entry_price)
        resolution_win = (direction_up and w.up_wins) or (
            not direction_up and not w.up_wins
        )
        if resolution_win:
            resolution_wins += 1

        no_stop_trade_pnl = (
            (amount / entry_price) - amount if resolution_win else -amount
        )
        no_stop_pnl += no_stop_trade_pnl

        stop_exit = _find_token_floor_exit(
            w=w,
            direction_up=direction_up,
            fill_ts=fill_ts,
            entry_price=entry_price,
            token_floor=token_floor,
            min_hold_sec=min_hold_sec,
            sell_buffer_ticks=sell_buffer_ticks,
            max_data_age=max_data_age,
        )
        if stop_exit is None:
            stop_pnl += no_stop_trade_pnl
            continue

        stopped += 1
        if resolution_win:
            stopped_would_win += 1
        else:
            stopped_would_loss += 1
        exit_price = stop_exit["exit_price"]
        exit_prices.append(exit_price)
        trade_pnl = (amount / entry_price) * exit_price - amount
        stop_pnls.append(trade_pnl)
        stop_pnl += trade_pnl

    return {
        "entries": entries,
        "resolution_wins": resolution_wins,
        "resolution_win_rate": resolution_wins / entries if entries else 0.0,
        "avg_entry_price": sum(entry_prices) / len(entry_prices) if entry_prices else 0.0,
        "no_stop_pnl": no_stop_pnl,
        "no_stop_ev": no_stop_pnl / entries if entries else 0.0,
        "stop_pnl": stop_pnl,
        "stop_ev": stop_pnl / entries if entries else 0.0,
        "delta_pnl": stop_pnl - no_stop_pnl,
        "stopped": stopped,
        "stopped_would_win": stopped_would_win,
        "stopped_would_loss": stopped_would_loss,
        "avg_stop_exit": sum(exit_prices) / len(exit_prices) if exit_prices else 0.0,
        "avg_stop_pnl": sum(stop_pnls) / len(stop_pnls) if stop_pnls else 0.0,
    }


def token_floor_grid(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int,
    max_entry_price: float,
    min_entry_price: float,
    entry_delay_sec: float,
    price_hint_buffer_ticks: float,
    token_floors: list[float],
    min_hold_secs: list[float],
    sell_buffer_ticks: float,    amount: float = 1.0,
) -> None:
    """Print pure token bid floor stop-loss grid."""
    baseline = simulate_token_floor(
        windows,
        theta_pct=theta_pct,
        lo_rem=lo_rem,
        hi_rem=hi_rem,
        persistence_sec=persistence_sec,
        max_entry_price=max_entry_price,
        min_entry_price=min_entry_price,
        entry_delay_sec=entry_delay_sec,
        price_hint_buffer_ticks=price_hint_buffer_ticks,        token_floor=0.0,
        min_hold_sec=9999.0,
        sell_buffer_ticks=sell_buffer_ticks,
        amount=amount,
    )
    print("\n=== Token bid floor stop-loss (no BTC confirmation) ===")
    print(f"Entry config: theta={theta_pct:.3f}% persistence={persistence_sec}s "
          f"band=[{lo_rem},{hi_rem}] cap=[{min_entry_price:.3f},{max_entry_price:.3f}] "
          f"delay={entry_delay_sec:.1f}s amount=${amount:.2f}")
    print(f"No stop: N={baseline['entries']} wins={baseline['resolution_wins']} "
          f"winR={baseline['resolution_win_rate']:.1%} "
          f"avgEntry={baseline['avg_entry_price']:.3f} "
          f"PnL=${baseline['no_stop_pnl']:+.3f} "
          f"EV/trade=${baseline['no_stop_ev']:+.4f}")
    print(f"{'floor':>6} {'hold':>5} {'stops':>6} {'stopW':>6} {'stopL':>6} "
          f"{'avgExit':>8} {'PnL':>9} {'EV/trd':>8} {'delta':>9}")
    for floor in token_floors:
        for hold in min_hold_secs:
            r = simulate_token_floor(
                windows,
                theta_pct=theta_pct,
                lo_rem=lo_rem,
                hi_rem=hi_rem,
                persistence_sec=persistence_sec,
                max_entry_price=max_entry_price,
                min_entry_price=min_entry_price,
                entry_delay_sec=entry_delay_sec,
                price_hint_buffer_ticks=price_hint_buffer_ticks,                token_floor=floor,
                min_hold_sec=hold,
                sell_buffer_ticks=sell_buffer_ticks,
                amount=amount,
            )
            print(f"{floor:>6.2f} {hold:>5.0f} {r['stopped']:>6d} "
                  f"{r['stopped_would_win']:>6d} {r['stopped_would_loss']:>6d} "
                  f"{r['avg_stop_exit']:>8.3f} "
                  f"${r['stop_pnl']:>+8.3f} ${r['stop_ev']:>+7.4f} "
                  f"${r['delta_pnl']:>+8.3f}")


def stop_loss_grid(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int,
    max_entry_price: float,
    min_entry_price: float,
    entry_delay_sec: float,
    price_hint_buffer_ticks: float,
    stop_loss_pcts: list[float],
    stop_confirm_secs: list[float],
    min_hold_secs: list[float],
    sell_buffer_ticks: float,
    btc_invalidation_mode: str,    amount: float = 1.0,
) -> None:
    """Print focused stop-loss comparison for one entry config."""
    baseline = simulate_stop_loss(
        windows,
        theta_pct=theta_pct,
        lo_rem=lo_rem,
        hi_rem=hi_rem,
        persistence_sec=persistence_sec,
        max_entry_price=max_entry_price,
        min_entry_price=min_entry_price,
        entry_delay_sec=entry_delay_sec,
        price_hint_buffer_ticks=price_hint_buffer_ticks,        stop_loss_pct=999.0,
        stop_confirm_sec=999.0,
        min_hold_sec=999.0,
        sell_buffer_ticks=sell_buffer_ticks,
        btc_invalidation_mode=btc_invalidation_mode,
        amount=amount,
    )
    print("\n=== Token+BTC stop-loss comparison ===")
    print(f"Entry config: theta={theta_pct:.3f}% persistence={persistence_sec}s "
          f"band=[{lo_rem},{hi_rem}] cap=[{min_entry_price:.3f},{max_entry_price:.3f}] "
          f"delay={entry_delay_sec:.1f}s amount=${amount:.2f} "
          f"btcStop={btc_invalidation_mode}")
    print(f"No stop: N={baseline['entries']} wins={baseline['resolution_wins']} "
          f"winR={baseline['resolution_win_rate']:.1%} "
          f"avgEntry={baseline['avg_entry_price']:.3f} "
          f"PnL=${baseline['no_stop_pnl']:+.3f} "
          f"EV/trade=${baseline['no_stop_ev']:+.4f}")
    print(f"{'SL%':>5} {'hold':>5} {'conf':>5} {'stops':>6} {'stopW':>5} "
          f"{'stopL':>5} {'avgExit':>7} {'PnL':>9} {'EV/trd':>8} {'delta':>9}")
    for sl in stop_loss_pcts:
        for hold in min_hold_secs:
            for conf in stop_confirm_secs:
                r = simulate_stop_loss(
                    windows,
                    theta_pct=theta_pct,
                    lo_rem=lo_rem,
                    hi_rem=hi_rem,
                    persistence_sec=persistence_sec,
                    max_entry_price=max_entry_price,
                    min_entry_price=min_entry_price,
                    entry_delay_sec=entry_delay_sec,
                    price_hint_buffer_ticks=price_hint_buffer_ticks,                    stop_loss_pct=sl,
                    stop_confirm_sec=conf,
                    min_hold_sec=hold,
                    sell_buffer_ticks=sell_buffer_ticks,
                    btc_invalidation_mode=btc_invalidation_mode,
                    amount=amount,
                )
                print(f"{sl:>5.0%} {hold:>5.0f} {conf:>5.0f} "
                      f"{r['stopped']:>6d} {r['stopped_would_win']:>5d} "
                      f"{r['stopped_would_loss']:>5d} {r['avg_stop_exit']:>7.3f} "
                      f"${r['stop_pnl']:>+8.3f} ${r['stop_ev']:>+7.4f} "
                      f"${r['delta_pnl']:>+8.3f}")


def replay(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int = 10,
    max_entry_price: float = 0.99,
    min_entry_price: float = 0.0,
    entry_delay_sec: float = 10.0,
    amount: float = 1.0,
    price_hint_buffer_ticks: float = 0.0,) -> None:
    """Print a window-by-window replay like a dry-run log."""
    import datetime

    trades = collect_trades(
        windows, theta_pct, lo_rem, hi_rem,
        persistence_sec=persistence_sec,
        max_entry_price=max_entry_price,
        min_entry_price=min_entry_price,
        entry_delay_sec=entry_delay_sec,
        price_hint_buffer_ticks=price_hint_buffer_ticks,    )
    trade_by_ts = {t["ts"]: t for t in trades}

    total_pnl = 0.0
    wins = losses = skipped = 0

    print(f"\n{'='*70}")
    print(f"REPLAY  theta={theta_pct:.3f}%  band=[{lo_rem},{hi_rem}]  "
          f"cap=[{min_entry_price:.2f},{max_entry_price:.2f}]  "
          f"delay={entry_delay_sec:.0f}s  amount=${amount:.2f}")
    print(f"{'='*70}")

    for w in windows:
        start_dt = datetime.datetime.utcfromtimestamp(w.start_epoch).strftime("%H:%M")
        end_dt   = datetime.datetime.utcfromtimestamp(w.end_epoch).strftime("%H:%M")
        label = f"{start_dt}-{end_dt} UTC"

        # Find matching trade for this window (by fill_ts inside window)
        matched = next(
            (t for t in trades
             if w.start_epoch <= t["ts"] < w.end_epoch),
            None
        )

        if matched is None:
            skipped += 1
            print(f"  [{label}]  {w.direction.upper():4s}  — no signal")
            continue

        entry  = matched["entry_price"]
        side   = "UP  " if matched["direction_up"] else "DOWN"
        win    = matched["win"]
        pnl    = (1.0 - entry) * amount / entry if win else -amount
        total_pnl += pnl
        shares = amount / entry

        if win:
            wins += 1
            result = f"✓ WIN   exit≈1.00  PnL={pnl:+.3f}"
        else:
            losses += 1
            result = f"✗ LOSS  exit≈0.00  PnL={pnl:+.3f}"

        conf = matched.get("confidence", "normal").upper()
        print(f"  [{label}]  {w.direction.upper():4s}  "
              f"ENTER {side} @ {entry:.3f} {conf:6s} {shares:.2f}sh  {result}  "
              f"cumPnL={total_pnl:+.3f}")

    total = wins + losses + skipped
    print(f"{'='*70}")
    print(f"  Windows: {total}  |  Entered: {wins+losses}  |  Skipped: {skipped}")
    if wins + losses:
        wr = wins / (wins + losses)
        print(f"  Win rate: {wins}/{wins+losses} = {wr:.1%}  |  "
              f"Total PnL: ${total_pnl:+.3f}  |  "
              f"EV/trade: ${total_pnl/(wins+losses):+.4f}")
    print()


def strength_bucket_analysis(
    windows: list[Window],
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int,
    max_entry_price: float,
    min_entry_price: float,
    entry_delay_sec: float,
    price_hint_buffer_ticks: float,
    bucket_edges: list[float],
) -> None:
    """Print win rate / EV by signal-strength bucket for one entry config."""
    trades = collect_trades(
        windows,
        theta_pct,
        lo_rem,
        hi_rem,
        persistence_sec=persistence_sec,
        max_entry_price=max_entry_price,
        min_entry_price=min_entry_price,
        entry_delay_sec=entry_delay_sec,
        price_hint_buffer_ticks=price_hint_buffer_ticks,
    )
    if not trades:
        print("\n=== Signal strength bucket analysis ===")
        print("No trades for selected config.")
        return

    edges = sorted(bucket_edges)
    if not edges:
        edges = [1.0, 1.5, 2.0, 3.0]

    print("\n=== Signal strength bucket analysis ===")
    print(f"Config: theta={theta_pct:.3f}% band=[{lo_rem},{hi_rem}] "
          f"persistence={persistence_sec}s cap=[{min_entry_price:.3f},{max_entry_price:.3f}] "
          f"delay={entry_delay_sec:.1f}s")
    print(f"Total trades: {len(trades)}")
    print(f"{'bucket':>12} {'N':>4} {'winR':>6} {'avgP':>6} {'avgSig':>7} {'EV/trd':>8}")

    bounds = [0.0] + edges + [float("inf")]
    for lo_edge, hi_edge in zip(bounds, bounds[1:]):
        sub = [
            t for t in trades
            if lo_edge <= t["signal_strength"] < hi_edge
        ]
        if not sub:
            continue
        n = len(sub)
        wins = sum(1 for t in sub if t["win"])
        avg_entry = sum(t["entry_price"] for t in sub) / n
        avg_strength = sum(t["signal_strength"] for t in sub) / n
        ev = sum(
            ((1.0 - t["entry_price"]) if t["win"] else -t["entry_price"])
            for t in sub
        ) / n
        hi_label = "inf" if hi_edge == float("inf") else f"{hi_edge:.2f}"
        print(f"{f'[{lo_edge:.2f},{hi_label})':>12} {n:>4d} "
              f"{wins/n:>6.1%} {avg_entry:>6.3f} {avg_strength:>7.2f} {ev:>+8.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="+")
    ap.add_argument("--max-entry-price", type=float, default=0.99)
    ap.add_argument("--min-entry-price", type=float, default=0.0)
    ap.add_argument("--persistence", type=int, default=10)
    ap.add_argument("--delays", default="0,2,5,10",
                    help="Comma-separated entry delays (seconds) to compare")
    ap.add_argument("--replay", action="store_true",
                    help="Print window-by-window replay instead of grid analysis")
    ap.add_argument("--theta", type=float, default=0.02,
                    help="BTC move threshold %% for replay mode")
    ap.add_argument("--lo", type=int, default=180,
                    help="Entry band lo_rem for replay mode")
    ap.add_argument("--hi", type=int, default=270,
                    help="Entry band hi_rem for replay mode")
    ap.add_argument("--amount", type=float, default=1.0,
                    help="Simulated trade amount in USD for replay mode")
    ap.add_argument("--price-hint-buffer-ticks", type=float, default=1.0,
                    help="Approximate live BUY hint buffer in ticks (default 1.0)")
    ap.add_argument("--stop-loss-grid", action="store_true",
                    help="Compare hold-to-resolution with token+BTC confluence stop-loss")
    ap.add_argument("--token-floor-grid", action="store_true",
                    help="Compare hold-to-resolution with pure token bid floor stop-loss")
    ap.add_argument("--token-floors", default="0.20,0.25,0.30",
                    help="Comma-separated absolute token bid floor prices for --token-floor-grid")
    ap.add_argument("--stop-loss-pcts", default="0.05,0.10,0.15,0.20",
                    help="Comma-separated token bid drawdown thresholds for stop-loss")
    ap.add_argument("--stop-confirm-secs", default="3,5",
                    help="Comma-separated confluence confirmation seconds")
    ap.add_argument("--min-hold-secs", default="20,30",
                    help="Comma-separated minimum hold seconds before stop-loss can fire")
    ap.add_argument("--sell-buffer-ticks", type=float, default=1.0,
                    help="Approximate live SELL hint buffer in ticks (best_bid - buffer)")
    ap.add_argument("--btc-invalidation-mode", default="reverse_theta",
                    choices=["reverse_theta", "signal_half", "near_open"],
                    help="BTC side of stop-loss confluence")
    ap.add_argument("--strength-bucket-analysis", action="store_true",
                    help="Show signal-strength buckets for selected single config")
    ap.add_argument("--strength-buckets", default="1.0,1.25,1.5,2.0,3.0",
                    help="Comma-separated signal-strength bucket edges")
    args = ap.parse_args()

    # Support multiple input files — concatenate windows in order
    all_windows: list[Window] = []
    for p in args.path:
        ws = parse_file(p)
        all_windows.extend(ws)
    # Sort by window start epoch so multi-file runs are chronological
    all_windows.sort(key=lambda w: w.start_epoch)
    windows = all_windows

    print(f"Loaded {len(windows)} windows from {len(args.path)} file(s)")
    if not windows:
        sys.exit(1)

    up_resolves = sum(1 for w in windows if w.up_wins)
    print(f"Baseline: UP resolved in {up_resolves}/{len(windows)} "
          f"= {up_resolves/len(windows):.1%}")
    poly_counts = [len(w.poly) for w in windows]
    btc_counts = [len(w.btc) for w in windows]
    print(f"Per-window: BTC ticks median={sorted(btc_counts)[len(btc_counts)//2]}, "
          f"Poly updates median={sorted(poly_counts)[len(poly_counts)//2]}")

    if args.replay:
        replay(
            windows,
            theta_pct=args.theta,
            lo_rem=args.lo,
            hi_rem=args.hi,
            persistence_sec=args.persistence,
            max_entry_price=args.max_entry_price,
            min_entry_price=args.min_entry_price,
            entry_delay_sec=float(args.delays.split(",")[-1]),
            amount=args.amount,
            price_hint_buffer_ticks=args.price_hint_buffer_ticks,        )
        return

    if args.token_floor_grid:
        token_floor_grid(
            windows,
            theta_pct=args.theta,
            lo_rem=args.lo,
            hi_rem=args.hi,
            persistence_sec=args.persistence,
            max_entry_price=args.max_entry_price,
            min_entry_price=args.min_entry_price,
            entry_delay_sec=float(args.delays.split(",")[-1]),
            price_hint_buffer_ticks=args.price_hint_buffer_ticks,
            token_floors=[float(v) for v in args.token_floors.split(",")],
            min_hold_secs=[float(v) for v in args.min_hold_secs.split(",")],
            sell_buffer_ticks=args.sell_buffer_ticks,            amount=args.amount,
        )
        return

    if args.stop_loss_grid:
        stop_loss_grid(
            windows,
            theta_pct=args.theta,
            lo_rem=args.lo,
            hi_rem=args.hi,
            persistence_sec=args.persistence,
            max_entry_price=args.max_entry_price,
            min_entry_price=args.min_entry_price,
            entry_delay_sec=float(args.delays.split(",")[-1]),
            price_hint_buffer_ticks=args.price_hint_buffer_ticks,
            stop_loss_pcts=[float(v) for v in args.stop_loss_pcts.split(",")],
            stop_confirm_secs=[float(v) for v in args.stop_confirm_secs.split(",")],
            min_hold_secs=[float(v) for v in args.min_hold_secs.split(",")],
            sell_buffer_ticks=args.sell_buffer_ticks,
            btc_invalidation_mode=args.btc_invalidation_mode,            amount=args.amount,
        )
        return

    if args.strength_bucket_analysis:
        strength_bucket_analysis(
            windows,
            theta_pct=args.theta,
            lo_rem=args.lo,
            hi_rem=args.hi,
            persistence_sec=args.persistence,
            max_entry_price=args.max_entry_price,
            min_entry_price=args.min_entry_price,
            entry_delay_sec=float(args.delays.split(",")[-1]),
            price_hint_buffer_ticks=args.price_hint_buffer_ticks,
            bucket_edges=[float(v) for v in args.strength_buckets.split(",") if v],
        )
        return

    delays = [float(d) for d in args.delays.split(",")]
    thetas = [0.02, 0.025, 0.03, 0.05, 0.08, 0.10]
    bands = [
        (60, 180),    # last 1-3 min (prior best)
        (60, 240),    # last 1-4 min
        (120, 240),   # last 2-4 min (early)
        (120, 270),   # last 2-4.5 min (very early)
        (180, 270),   # last 3-4.5 min (earliest)
    ]

    sim_cache: dict[tuple, dict] = {}
    trade_cache: dict[tuple, list[dict]] = {}

    def run_sim(theta: float, lo: int, hi: int, max_entry_price: float, delay: float) -> dict:
        key = (theta, lo, hi, args.persistence, max_entry_price, delay,
               args.min_entry_price, args.price_hint_buffer_ticks)
        if key not in sim_cache:
            sim_cache[key] = simulate(
                windows, theta, lo, hi,
                persistence_sec=args.persistence,
                max_entry_price=max_entry_price,
                min_entry_price=args.min_entry_price,
                entry_delay_sec=delay,
                price_hint_buffer_ticks=args.price_hint_buffer_ticks,
            )
        return sim_cache[key]

    def run_trades(theta: float, lo: int, hi: int, max_entry_price: float, delay: float) -> list[dict]:
        key = (theta, lo, hi, args.persistence, max_entry_price, delay,
               args.min_entry_price, args.price_hint_buffer_ticks)
        if key not in trade_cache:
            trade_cache[key] = collect_trades(
                windows, theta, lo, hi,
                persistence_sec=args.persistence,
                max_entry_price=max_entry_price,
                min_entry_price=args.min_entry_price,
                entry_delay_sec=delay,
                price_hint_buffer_ticks=args.price_hint_buffer_ticks,
            )
        return trade_cache[key]

    for delay in delays:
        print(f"\n=== Delay={delay:.0f}s  (persistence={args.persistence}s, "
              f"cap={args.max_entry_price}) ===")
        hdr = (f"{'theta%':>8} {'band':>10} {'N':>4} {'wins':>5} "
               f"{'winR':>6} {'CIlo':>6} {'avgP':>6} {'slip':>6} "
               f"{'EV/trd':>7} {'flip':>4} {'bandSk':>6} {'capSk':>6} {'noQ':>5}")
        print(hdr)
        for theta in thetas:
            for lo, hi in bands:
                r = run_sim(theta, lo, hi, args.max_entry_price, delay)
                print(
                    f"{theta:>8.3f} {f'[{lo},{hi}]':>10} {r['entries']:>4d} "
                    f"{r['wins']:>5d} {r['win_rate']:>6.1%} "
                    f"{r['win_rate_ci_lo']:>6.1%} {r['avg_entry_price']:>6.3f} "
                    f"{r['avg_slippage']:>+6.3f} {r['ev_per_trade']:>+7.4f} "
                    f"{r['direction_flipped']:>4d} {r['skipped_band']:>6d} "
                    f"{r['skipped_price_cap']:>6d} {r['skipped_no_quote']:>5d}"
                )

    # Entry-price-bucket analysis: for each (theta, band, delay=10s), split trades
    # into price buckets and show win rate per bucket
    print("\n=== Entry price bucket analysis (delay=10s) ===")
    print("For each config, we see: how trades distribute across buckets and "
          "whether cheap entries retain win rate.")
    buckets = [(0.50, 0.60), (0.60, 0.70), (0.70, 0.80),
               (0.80, 0.90), (0.90, 1.00)]
    bucket_delay = 10.0
    for theta in thetas:
        for lo, hi in bands:
            # Collect all individual trade rows (no max-price filter)
            trades = run_trades(theta, lo, hi, 0.999, bucket_delay)
            if not trades:
                continue
            # Bucket stats
            rows = []
            for lo_p, hi_p in buckets:
                sub = [t for t in trades if lo_p <= t["entry_price"] < hi_p]
                if not sub:
                    rows.append((lo_p, hi_p, 0, 0, 0.0, 0.0))
                    continue
                wins = sum(1 for t in sub if t["win"])
                ev = sum(
                    (1 - t["entry_price"]) if t["win"] else -t["entry_price"]
                    for t in sub
                ) / len(sub)
                rows.append((lo_p, hi_p, len(sub), wins, wins / len(sub), ev))
            total_n = len(trades)
            total_w = sum(1 for t in trades if t["win"])
            print(f"\n  θ={theta:.2f}% band=[{lo},{hi}]  total N={total_n} "
                  f"winR={total_w/total_n:.1%}")
            print(f"    {'bucket':>12} {'N':>4} {'winR':>6} {'EV/trd':>8}")
            for lo_p, hi_p, n, w, wr, ev in rows:
                if n == 0:
                    continue
                print(f"    [{lo_p:.2f},{hi_p:.2f}) {n:>4d} "
                      f"{wr:>6.1%} {ev:>+8.4f}")

    # Final: top configs filtered to entry price < 0.70 (user's target)
    print("\n=== Top 5 by EV (entries ≥ 5, avg entry price < 0.75, delay=10s) ===")
    ranks = []
    for theta in thetas:
        for lo, hi in bands:
            r = run_sim(theta, lo, hi, 0.75, 10.0)
            if r["entries"] >= 5:
                ranks.append((r["ev_per_trade"], theta, (lo, hi), r))
    ranks.sort(reverse=True)
    for ev, theta, band, r in ranks[:5]:
        print(f"  θ={theta:.2f}% band={band}: N={r['entries']} "
              f"winR={r['win_rate']:.1%} CIlo={r['win_rate_ci_lo']:.1%} "
              f"avgEntry={r['avg_entry_price']:.3f} EV={ev:+.4f}/$1")

    # ------------------------------------------------------------------
    # Focused analysis: trades with entry price in [0.60, 0.70) — the "0.65"
    # bucket the user asked about.  Pooled across configs at delay=10s.
    # ------------------------------------------------------------------
    print("\n=== Bucket [0.60, 0.70) — 'entry ≈ 0.65' focus (delay=10s) ===")
    print(f"{'theta%':>7} {'band':>10} {'N':>4} {'winR':>6} {'avgP':>6} "
          f"{'EV/trd':>7}")
    bucket_lo, bucket_hi = 0.60, 0.70
    for theta in thetas:
        for lo, hi in bands:
            all_trades = run_trades(theta, lo, hi, 0.999, 10.0)
            sub = [t for t in all_trades
                   if bucket_lo <= t["entry_price"] < bucket_hi]
            if not sub:
                continue
            n = len(sub)
            w = sum(1 for t in sub if t["win"])
            avg_p = sum(t["entry_price"] for t in sub) / n
            ev = sum((1 - t["entry_price"]) if t["win"] else -t["entry_price"]
                     for t in sub) / n
            print(f"{theta:>7.2f} {f'[{lo},{hi}]':>10} {n:>4d} "
                  f"{w/n:>6.1%} {avg_p:>6.3f} {ev:>+7.4f}")

def collect_trades(
    windows, theta_pct, lo_rem, hi_rem,
    persistence_sec=10, max_data_age=2.0, max_entry_price=0.999,
    entry_delay_sec=0.0,
    min_entry_price=0.0,
    price_hint_buffer_ticks: float = 0.0,
) -> list[dict]:
    """Same logic as simulate() but returns per-trade records."""
    trades: list[dict] = []
    for w in windows:
        entry = _find_window_entry(
            w,
            theta_pct=theta_pct,
            lo_rem=lo_rem,
            hi_rem=hi_rem,
            persistence_sec=persistence_sec,
            max_data_age=max_data_age,
            max_entry_price=max_entry_price,
            entry_delay_sec=entry_delay_sec,
            min_entry_price=min_entry_price,
            price_hint_buffer_ticks=price_hint_buffer_ticks,
        )
        if entry is None or entry["fill_price"] is None:
            continue

        direction_up = entry["direction_up"]
        fill_ts = entry["fill_ts"]
        fill_price = entry["fill_price"]
        win = (direction_up and w.up_wins) or (not direction_up and not w.up_wins)
        trades.append({
            "ts": entry["signal_ts"],
            "fill_ts": fill_ts,
            "window_end": w.end_epoch,
            "direction_up": direction_up,
            "entry_price": fill_price,
            "signal_price": entry["sig_price"],
            "signal_strength": entry.get("signal_strength"),
            "past_signal_strength": entry.get("past_signal_strength"),
            "open_price": w.open_price,
            "win": win,
            "confidence": entry.get("confidence", "normal"),
            "max_entry_price": entry.get("effective_max_entry_price", max_entry_price),
        })
    return trades


def _find_window_entry(
    w: Window,
    theta_pct: float,
    lo_rem: int,
    hi_rem: int,
    persistence_sec: int,
    max_data_age: float,
    max_entry_price: float,
    entry_delay_sec: float,
    min_entry_price: float,
    price_hint_buffer_ticks: float,
) -> dict | None:
    """Return the first valid entry candidate for one window."""
    if not w.btc:
        return None

    entry_start = w.start_epoch + (300 - hi_rem)
    entry_end = w.start_epoch + (300 - lo_rem)
    start_idx = bisect_right(w.btc_ts, entry_start - 1e-9)
    skipped_no_quote = 0
    skipped_price_cap = 0
    skipped_band = 0
    committed_direction_up: bool | None = None
    first_signal_ts: float | None = None
    for tick in w.btc[start_idx:]:
        if tick.ts > entry_end:
            break
        move_pct = (tick.price - w.open_price) / w.open_price * 100.0
        if abs(move_pct) < theta_pct:
            continue
        past_target = tick.ts - persistence_sec
        j = bisect_right(w.btc_ts, past_target) - 1
        if j < 0:
            continue
        past_move = (w.btc_px[j] - w.open_price) / w.open_price * 100.0
        if (move_pct > 0) != (past_move > 0):
            continue
        if abs(move_pct) < abs(past_move) * 0.7:
            continue
        signal_strength = abs(move_pct) / theta_pct if theta_pct > 0 else 0.0
        past_signal_strength = abs(past_move) / theta_pct if theta_pct > 0 else 0.0

        direction_up = move_pct > 0
        if committed_direction_up is not None and direction_up != committed_direction_up:
            continue
        if committed_direction_up is None:
            committed_direction_up = direction_up
            first_signal_ts = tick.ts

        lookup = w.up_ask_lookup if direction_up else w.down_ask_lookup
        tick_size = w.up_tick_size if direction_up else w.down_tick_size
        sig_price = lookup.at(tick.ts, max_data_age) if lookup else None
        if sig_price is None or sig_price <= 0 or sig_price >= 1:
            skipped_no_quote += 1
            continue
        if sig_price > max_entry_price or sig_price < min_entry_price:
            skipped_band += 1
            continue
        fill_ts = tick.ts + entry_delay_sec
        if fill_ts >= w.end_epoch:
            continue
        fill_price = lookup.at(fill_ts, max_data_age) if lookup else None
        if fill_price is None or fill_price <= 0 or fill_price >= 1:
            skipped_no_quote += 1
            continue
        buffered_fill_price = fill_price + tick_size * price_hint_buffer_ticks
        buffered_fill_price = min(
            1.0,
            math.ceil(buffered_fill_price / tick_size) * tick_size,
        )
        if buffered_fill_price > max_entry_price:
            skipped_price_cap += 1
            continue
        if fill_price < min_entry_price:
            continue

        k = bisect_right(w.btc_ts, fill_ts) - 1
        direction_flipped = False
        if k >= 0:
            fill_move_pct = (w.btc_px[k] - w.open_price) / w.open_price * 100.0
            direction_flipped = (fill_move_pct > 0) != direction_up
        return {
            "signal_ts": first_signal_ts if first_signal_ts is not None else tick.ts,
            "fill_ts": fill_ts,
            "direction_up": direction_up,
            "sig_price": sig_price,
            "fill_price": buffered_fill_price,
            "signal_strength": signal_strength,
            "past_signal_strength": past_signal_strength,
            "effective_max_entry_price": max_entry_price,
            "skipped_no_quote": skipped_no_quote,
            "skipped_price_cap": skipped_price_cap,
            "skipped_band": skipped_band,
            "direction_flipped": direction_flipped,
        }
    if skipped_no_quote or skipped_price_cap or skipped_band:
        return {
            "signal_ts": None,
            "fill_ts": None,
            "direction_up": None,
            "sig_price": None,
            "fill_price": None,
            "skipped_no_quote": skipped_no_quote,
            "skipped_price_cap": skipped_price_cap,
            "skipped_band": skipped_band,
            "direction_flipped": False,
        }
    return None


if __name__ == "__main__":
    main()
