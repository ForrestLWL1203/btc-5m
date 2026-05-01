"""Backtest crowd_m1 candidates against collected JSONL data.

Collector files contain Binance ticks plus Polymarket best bid/ask quotes, but
not full L2 book sizes. Entry fillability is therefore approximated with the
target leg best ask being at or below the configured cap.
"""

from __future__ import annotations

import argparse
from bisect import bisect_left, bisect_right
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


WINDOW_SEC = 300.0


@dataclass(frozen=True)
class Candidate:
    name: str
    entry_elapsed_sec: float
    min_leading_ask: float
    stop_loss_trigger: Optional[float]
    entry_timeout_sec: float = 0.0
    entry_end_elapsed_sec: Optional[float] = None
    strong_move_pct: Optional[float] = None
    btc_direction_confirm: bool = False
    stop_loss_drop_pct: Optional[float] = None
    stop_loss_start_remaining_sec: float = 55.0
    stop_loss_end_remaining_sec: float = 40.0
    max_entry_price: float = 0.75
    min_ask_gap: float = 0.0
    min_sell_price: float = 0.20
    entry_ask_level: int = 10
    tick_size: float = 0.01
    entry_buffer_ticks: float = 0.0
    stop_loss_buffer_ticks: float = 0.0


@dataclass
class Trade:
    candidate: str
    window: str
    start_ts: float
    entry_ts: float
    exit_ts: float
    side: str
    outcome: str
    result: str
    exit_reason: str
    leading_ask: float
    lower_ask: float
    ask_gap: float
    entry_level: int
    entry_price: float
    exit_price: float
    realized_pnl: float
    hold_pnl: float
    false_stop: bool
    btc_move_pct: Optional[float] = None


@dataclass(frozen=True)
class EntryDecision:
    ts: float
    side: str
    leading_ask: float
    lower_ask: float
    ask_gap: float
    entry_price: float
    btc_move_pct: Optional[float] = None


@dataclass(frozen=True)
class WindowIndex:
    rows: list[dict]
    outcome: dict
    start_ts: float
    end_ts: float
    btc_ts: list[float]
    btc_prices: list[float]
    poly_rows: list[dict]
    poly_ts: list[float]


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_windows(path: Path) -> list[tuple[list[dict], dict]]:
    windows: list[tuple[list[dict], dict]] = []
    current: list[dict] = []
    with path.open() as f:
        for raw in f:
            if not raw.strip():
                continue
            row = json.loads(raw)
            if row.get("src") == "outcome":
                windows.append((current, row))
                current = []
            else:
                current.append(row)
    return windows


def build_window_indexes(windows: list[tuple[list[dict], dict]]) -> list[WindowIndex]:
    return [_build_window_index(rows, outcome) for rows, outcome in windows]


def _build_window_index(rows: list[dict], outcome: dict) -> WindowIndex:
    end_ts = float(outcome["ts"])
    start_ts = end_ts - WINDOW_SEC
    sorted_rows = sorted(
        rows,
        key=lambda row: row.get("ts") if isinstance(row.get("ts"), (int, float)) else float("inf"),
    )
    btc_ts: list[float] = []
    btc_prices: list[float] = []
    poly_rows: list[dict] = []
    poly_ts: list[float] = []
    for row in sorted_rows:
        row_ts = row.get("ts")
        if not isinstance(row_ts, (int, float)):
            continue
        if row.get("src") == "binance" and row.get("price") is not None:
            btc_ts.append(float(row_ts))
            btc_prices.append(float(row["price"]))
        elif row.get("src") == "poly" and row.get("token") in ("up", "down"):
            poly_rows.append(row)
            poly_ts.append(float(row_ts))
    return WindowIndex(
        rows=sorted_rows,
        outcome=outcome,
        start_ts=start_ts,
        end_ts=end_ts,
        btc_ts=btc_ts,
        btc_prices=btc_prices,
        poly_rows=poly_rows,
        poly_ts=poly_ts,
    )


def _quote_at_index(window: WindowIndex, ts: float) -> dict[str, dict]:
    quotes: dict[str, dict] = {}
    idx = bisect_right(window.poly_ts, ts) - 1
    while idx >= 0 and len(quotes) < 2:
        row = window.poly_rows[idx]
        if row.get("ask") is not None:
            token = str(row["token"])
            if token not in quotes:
                quotes[token] = row
        idx -= 1
    return quotes


def _btc_price_at_or_before_index(window: WindowIndex, ts: float) -> Optional[float]:
    idx = bisect_right(window.btc_ts, ts) - 1
    if idx < 0:
        return None
    return window.btc_prices[idx]


def _btc_price_at_or_after_index(
    window: WindowIndex,
    ts: float,
    *,
    max_forward_sec: float = 30.0,
) -> Optional[float]:
    idx = bisect_left(window.btc_ts, ts)
    if idx >= len(window.btc_ts):
        return None
    if window.btc_ts[idx] - ts > max_forward_sec:
        return None
    return window.btc_prices[idx]


def _btc_dynamic_signal_index(
    window: WindowIndex,
    *,
    ts: float,
    candidate: Candidate,
    skips: dict[str, int],
) -> Optional[tuple[str, float]]:
    if not candidate.btc_direction_confirm and candidate.strong_move_pct is None:
        return None
    open_btc = _btc_price_at_or_after_index(window, window.start_ts)
    current_btc = _btc_price_at_or_before_index(window, ts)
    if open_btc is None or current_btc is None or open_btc <= 0:
        skips["btc_missing"] += 1
        return None
    move_pct = (current_btc / open_btc - 1.0) * 100.0
    if candidate.strong_move_pct is not None and abs(move_pct) < candidate.strong_move_pct:
        skips["btc_strength"] += 1
        return None
    if move_pct == 0:
        skips["btc_strength"] += 1
        return None
    return ("up" if move_pct > 0 else "down", move_pct)


def _buffered_buy_price(ask: float, candidate: Candidate) -> float:
    return min(1.0, ask + candidate.tick_size * candidate.entry_buffer_ticks)


def _buffered_sell_price(bid: float, candidate: Candidate) -> float:
    return max(0.0, bid - candidate.tick_size * candidate.stop_loss_buffer_ticks)


def _entry_decision_from_quotes(
    quotes: dict[str, dict],
    *,
    ts: float,
    candidate: Candidate,
    skips: dict[str, int],
    btc_side: Optional[str] = None,
    btc_move_pct: Optional[float] = None,
) -> Optional[EntryDecision]:
    if "up" not in quotes or "down" not in quotes:
        skips["missing_quote"] += 1
        return None

    up_ask = float(quotes["up"]["ask"])
    down_ask = float(quotes["down"]["ask"])
    side = "up" if up_ask >= down_ask else "down"
    leading_ask = max(up_ask, down_ask)
    lower_ask = min(up_ask, down_ask)
    ask_gap = leading_ask - lower_ask

    if ask_gap < candidate.min_ask_gap:
        skips["ask_gap"] += 1
        return None
    if leading_ask < candidate.min_leading_ask:
        skips["leading"] += 1
        return None

    entry_price = _buffered_buy_price(float(quotes[side]["ask"]), candidate)
    if entry_price > candidate.max_entry_price:
        skips["cap"] += 1
        return None
    if btc_side is not None and side != btc_side:
        skips["btc_direction"] += 1
        return None

    return EntryDecision(
        ts=ts,
        side=side,
        leading_ask=leading_ask,
        lower_ask=lower_ask,
        ask_gap=ask_gap,
        entry_price=entry_price,
        btc_move_pct=btc_move_pct,
    )


def _entry_decision_index(
    window: WindowIndex,
    *,
    candidate: Candidate,
    skips: dict[str, int],
) -> Optional[EntryDecision]:
    entry_start_ts = window.start_ts + candidate.entry_elapsed_sec
    entry_end_ts = (
        window.start_ts + candidate.entry_end_elapsed_sec
        if candidate.entry_end_elapsed_sec is not None
        else entry_start_ts + candidate.entry_timeout_sec
    )
    uses_btc_filter = candidate.btc_direction_confirm or candidate.strong_move_pct is not None

    def evaluate(ts: float, quotes: dict[str, dict]) -> Optional[EntryDecision]:
        btc_signal = _btc_dynamic_signal_index(
            window,
            ts=ts,
            candidate=candidate,
            skips=skips,
        )
        if uses_btc_filter and btc_signal is None:
            return None
        return _entry_decision_from_quotes(
            quotes,
            ts=ts,
            candidate=candidate,
            skips=skips,
            btc_side=btc_signal[0] if btc_signal else None,
            btc_move_pct=btc_signal[1] if btc_signal else None,
        )

    if entry_end_ts <= entry_start_ts:
        return evaluate(entry_start_ts, _quote_at_index(window, entry_start_ts))

    quotes = _quote_at_index(window, entry_start_ts)
    decision = evaluate(entry_start_ts, quotes)
    if decision is not None:
        return decision

    idx = bisect_right(window.poly_ts, entry_start_ts)
    end_idx = bisect_right(window.poly_ts, entry_end_ts)
    for row in window.poly_rows[idx:end_idx]:
        if row.get("ask") is None:
            continue
        row_ts = float(row["ts"])
        quotes[str(row["token"])] = row
        decision = evaluate(row_ts, quotes)
        if decision is not None:
            return decision
    return None


def _stop_loss_exit_index(
    window: WindowIndex,
    *,
    side: str,
    entry_price: float,
    candidate: Candidate,
) -> tuple[Optional[float], Optional[float]]:
    if candidate.stop_loss_trigger is None and candidate.stop_loss_drop_pct is None:
        return None, None
    if candidate.stop_loss_drop_pct is not None:
        stop_price = max(candidate.min_sell_price, entry_price * (1.0 - candidate.stop_loss_drop_pct))
    else:
        stop_price = max(candidate.min_sell_price, candidate.stop_loss_trigger or 0.0)
    active_start = window.start_ts + (WINDOW_SEC - candidate.stop_loss_start_remaining_sec)
    active_end = window.start_ts + (WINDOW_SEC - candidate.stop_loss_end_remaining_sec)
    idx = bisect_left(window.poly_ts, active_start)
    end_idx = bisect_right(window.poly_ts, active_end)
    for row in window.poly_rows[idx:end_idx]:
        if row.get("token") != side:
            continue
        bid = row.get("bid")
        if bid is None:
            continue
        bid = float(bid)
        if candidate.min_sell_price <= bid <= stop_price:
            return float(row["ts"]), _buffered_sell_price(bid, candidate)
    return None, None


def backtest_candidate(
    windows: list[tuple[list[dict], dict]],
    candidate: Candidate,
) -> tuple[list[Trade], dict[str, int]]:
    return backtest_indexed_candidate(build_window_indexes(windows), candidate)


def backtest_indexed_candidate(
    windows: list[WindowIndex],
    candidate: Candidate,
) -> tuple[list[Trade], dict[str, int]]:
    trades: list[Trade] = []
    skips = {
        "missing_quote": 0,
        "ask_gap": 0,
        "leading": 0,
        "cap": 0,
        "btc_missing": 0,
        "btc_strength": 0,
        "btc_direction": 0,
    }
    for window in windows:
        decision = _entry_decision_index(
            window,
            candidate=candidate,
            skips=skips,
        )
        if decision is None:
            continue

        outcome_side = str(window.outcome.get("direction"))
        won = decision.side == outcome_side
        exit_ts = window.end_ts
        exit_reason = "settlement"
        exit_price = 1.0 if won else 0.0
        stop_ts, stop_price = _stop_loss_exit_index(
            window,
            side=decision.side,
            entry_price=decision.entry_price,
            candidate=candidate,
        )
        if stop_ts is not None and stop_price is not None:
            exit_ts = stop_ts
            exit_price = stop_price
            exit_reason = "stop_loss"

        shares = 1.0 / decision.entry_price
        realized_pnl = shares * exit_price - 1.0
        hold_pnl = shares * (1.0 if won else 0.0) - 1.0
        false_stop = exit_reason == "stop_loss" and won
        trades.append(
            Trade(
                candidate=candidate.name,
                window=str(window.outcome["window"]),
                start_ts=window.start_ts,
                entry_ts=decision.ts,
                exit_ts=exit_ts,
                side=decision.side,
                outcome=outcome_side,
                result="WIN" if won else "LOSS",
                exit_reason=exit_reason,
                leading_ask=decision.leading_ask,
                lower_ask=decision.lower_ask,
                ask_gap=decision.ask_gap,
                entry_level=candidate.entry_ask_level,
                entry_price=decision.entry_price,
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                hold_pnl=hold_pnl,
                false_stop=false_stop,
                btc_move_pct=decision.btc_move_pct,
            )
        )
    return trades, skips


def default_candidates() -> list[Candidate]:
    candidates = [
        Candidate(
            name="baseline_090_l060_sl035",
            entry_elapsed_sec=90.0,
            min_leading_ask=0.60,
            stop_loss_trigger=0.35,
        )
    ]
    for elapsed in (120.0, 150.0, 180.0):
        for min_leading in (0.58, 0.60, 0.62, 0.64, 0.66, 0.68):
            for trigger in (None, 0.30, 0.35, 0.40):
                trigger_label = "none" if trigger is None else f"{int(trigger * 100):03d}"
                leading_label = int(round(min_leading * 100))
                candidates.append(
                    Candidate(
                        name=(
                            f"live_{int(elapsed):03d}_l{leading_label:03d}"
                            f"_sl{trigger_label}"
                        ),
                        entry_elapsed_sec=elapsed,
                        min_leading_ask=min_leading,
                        stop_loss_trigger=trigger,
                    )
                )
    return candidates


def default_trade_candidate_names() -> set[str]:
    return {
        "baseline_090_l060_sl035",
        "live_120_l066_slnone",
        "live_120_l068_slnone",
        "live_150_l068_sl035",
        "live_180_l062_sl035",
        "live_180_l064_sl035",
    }


def _label_float(value: float, scale: int = 100) -> str:
    return f"{int(round(value * scale)):03d}"


def _build_custom_candidates(
    *,
    elapsed_values: list[float],
    min_leading_values: list[float],
    max_entry_values: list[float],
    strong_move_values: list[Optional[float]],
    entry_end_elapsed_sec: Optional[float],
    btc_direction_confirm: bool,
    stop_loss_trigger: Optional[float],
    stop_loss_drop_pct: Optional[float],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for elapsed in elapsed_values:
        for min_leading in min_leading_values:
            for max_entry in max_entry_values:
                for strong_move in strong_move_values:
                    strong_label = (
                        "none" if strong_move is None else _label_float(strong_move, scale=1000)
                    )
                    stop_label = (
                        f"drop{_label_float(stop_loss_drop_pct)}"
                        if stop_loss_drop_pct is not None
                        else "none"
                        if stop_loss_trigger is None
                        else f"sl{_label_float(stop_loss_trigger)}"
                    )
                    candidates.append(
                        Candidate(
                            name=(
                                f"custom_{int(elapsed):03d}"
                                f"_to{'' if entry_end_elapsed_sec is None else int(entry_end_elapsed_sec)}"
                                f"_l{_label_float(min_leading)}"
                                f"_cap{_label_float(max_entry)}"
                                f"_btc{strong_label}_{stop_label}"
                            ),
                            entry_elapsed_sec=elapsed,
                            entry_end_elapsed_sec=entry_end_elapsed_sec,
                            min_leading_ask=min_leading,
                            max_entry_price=max_entry,
                            stop_loss_trigger=stop_loss_trigger,
                            stop_loss_drop_pct=stop_loss_drop_pct,
                            strong_move_pct=strong_move,
                            btc_direction_confirm=btc_direction_confirm,
                        )
                    )
    return candidates


def summarize(
    *,
    candidate: Candidate,
    windows_count: int,
    trades: list[Trade],
    skips: dict[str, int],
) -> dict[str, object]:
    wins = sum(1 for trade in trades if trade.result == "WIN")
    losses = len(trades) - wins
    stop_losses = sum(1 for trade in trades if trade.exit_reason == "stop_loss")
    false_stops = sum(1 for trade in trades if trade.false_stop)
    return {
        "candidate": candidate.name,
        "entry_elapsed_sec": int(candidate.entry_elapsed_sec),
        "entry_end_elapsed_sec": (
            "" if candidate.entry_end_elapsed_sec is None else int(candidate.entry_end_elapsed_sec)
        ),
        "entry_timeout_sec": int(candidate.entry_timeout_sec),
        "min_leading_ask": candidate.min_leading_ask,
        "strong_move_pct": "" if candidate.strong_move_pct is None else candidate.strong_move_pct,
        "stop_loss_trigger": (
            "" if candidate.stop_loss_trigger is None else candidate.stop_loss_trigger
        ),
        "stop_loss_drop_pct": (
            "" if candidate.stop_loss_drop_pct is None else candidate.stop_loss_drop_pct
        ),
        "stop_loss_start_remaining_sec": int(candidate.stop_loss_start_remaining_sec),
        "stop_loss_end_remaining_sec": int(candidate.stop_loss_end_remaining_sec),
        "max_entry_price": candidate.max_entry_price,
        "entry_buffer_ticks": candidate.entry_buffer_ticks,
        "stop_loss_buffer_ticks": candidate.stop_loss_buffer_ticks,
        "windows": windows_count,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades), 6) if trades else 0.0,
        "stop_losses": stop_losses,
        "false_stop_losses": false_stops,
        "realized_pnl": round(sum(trade.realized_pnl for trade in trades), 6),
        "hold_pnl": round(sum(trade.hold_pnl for trade in trades), 6),
        "skip_missing_quote": skips["missing_quote"],
        "skip_ask_gap": skips["ask_gap"],
        "skip_leading": skips["leading"],
        "skip_cap": skips["cap"],
        "skip_btc_missing": skips["btc_missing"],
        "skip_btc_strength": skips["btc_strength"],
        "skip_btc_direction": skips["btc_direction"],
    }


def write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "candidate",
        "entry_elapsed_sec",
        "entry_end_elapsed_sec",
        "entry_timeout_sec",
        "min_leading_ask",
        "strong_move_pct",
        "stop_loss_trigger",
        "stop_loss_drop_pct",
        "stop_loss_start_remaining_sec",
        "stop_loss_end_remaining_sec",
        "max_entry_price",
        "entry_buffer_ticks",
        "stop_loss_buffer_ticks",
        "windows",
        "trades",
        "wins",
        "losses",
        "win_rate",
        "stop_losses",
        "false_stop_losses",
        "realized_pnl",
        "hold_pnl",
        "skip_missing_quote",
        "skip_ask_gap",
        "skip_leading",
        "skip_cap",
        "skip_btc_missing",
        "skip_btc_strength",
        "skip_btc_direction",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "candidate",
        "window",
        "start_utc",
        "entry_utc",
        "exit_utc",
        "side",
        "outcome",
        "result",
        "exit_reason",
        "leading_ask",
        "lower_ask",
        "ask_gap",
        "entry_level",
        "entry_price",
        "exit_price",
        "realized_pnl",
        "hold_pnl",
        "false_stop",
        "btc_move_pct",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    "candidate": trade.candidate,
                    "window": trade.window,
                    "start_utc": _fmt_ts(trade.start_ts),
                    "entry_utc": _fmt_ts(trade.entry_ts),
                    "exit_utc": _fmt_ts(trade.exit_ts),
                    "side": trade.side,
                    "outcome": trade.outcome,
                    "result": trade.result,
                    "exit_reason": trade.exit_reason,
                    "leading_ask": round(trade.leading_ask, 4),
                    "lower_ask": round(trade.lower_ask, 4),
                    "ask_gap": round(trade.ask_gap, 4),
                    "entry_level": trade.entry_level,
                    "entry_price": round(trade.entry_price, 4),
                    "exit_price": round(trade.exit_price, 4),
                    "realized_pnl": round(trade.realized_pnl, 6),
                    "hold_pnl": round(trade.hold_pnl, 6),
                    "false_stop": int(trade.false_stop),
                    "btc_move_pct": (
                        "" if trade.btc_move_pct is None else round(trade.btc_move_pct, 6)
                    ),
                }
            )


def _parse_float_list(raw: Optional[str]) -> Optional[list[float]]:
    if raw is None:
        return None
    return [float(part) for part in raw.split(",") if part]


def _parse_optional_float_list(raw: Optional[str]) -> Optional[list[Optional[float]]]:
    if raw is None:
        return None
    values: list[Optional[float]] = []
    for part in raw.split(","):
        if not part:
            continue
        if part.lower() in {"none", "null", "off"}:
            values.append(None)
        else:
            values.append(float(part))
    return values


def run_report(
    jsonl: Path,
    *,
    summary_out: Path,
    trades_dir: Path,
    trade_candidate_names: set[str],
    stop_loss_start_remaining_sec: float = 55.0,
    stop_loss_end_remaining_sec: float = 40.0,
    entry_timeout_sec: float = 0.0,
    max_entry_price: float = 0.75,
    max_entry_values: Optional[list[float]] = None,
    min_leading_values: Optional[list[float]] = None,
    entry_buffer_ticks: float = 0.0,
    stop_loss_buffer_ticks: float = 0.0,
    elapsed_values: Optional[list[float]] = None,
    entry_end_elapsed_sec: Optional[float] = None,
    strong_move_values: Optional[list[Optional[float]]] = None,
    btc_direction_confirm: bool = False,
    stop_loss_drop_pct: Optional[float] = None,
) -> list[dict[str, object]]:
    windows = _load_windows(jsonl)
    window_indexes = build_window_indexes(windows)
    summary_rows: list[dict[str, object]] = []
    custom_grid = any(
        value is not None
        for value in (
            elapsed_values,
            min_leading_values,
            max_entry_values,
            entry_end_elapsed_sec,
            strong_move_values,
            stop_loss_drop_pct,
        )
    ) or btc_direction_confirm
    if custom_grid:
        base_candidates = _build_custom_candidates(
            elapsed_values=elapsed_values or [120.0],
            min_leading_values=min_leading_values or [0.62],
            max_entry_values=max_entry_values or [max_entry_price],
            strong_move_values=strong_move_values or [None],
            entry_end_elapsed_sec=entry_end_elapsed_sec,
            btc_direction_confirm=btc_direction_confirm,
            stop_loss_trigger=None if stop_loss_drop_pct is not None else 0.35,
            stop_loss_drop_pct=stop_loss_drop_pct,
        )
    else:
        base_candidates = default_candidates()
    for base_candidate in base_candidates:
        candidate = Candidate(
            name=base_candidate.name,
            entry_elapsed_sec=base_candidate.entry_elapsed_sec,
            entry_end_elapsed_sec=base_candidate.entry_end_elapsed_sec,
            min_leading_ask=base_candidate.min_leading_ask,
            stop_loss_trigger=base_candidate.stop_loss_trigger,
            stop_loss_drop_pct=base_candidate.stop_loss_drop_pct,
            entry_timeout_sec=entry_timeout_sec,
            strong_move_pct=base_candidate.strong_move_pct,
            btc_direction_confirm=base_candidate.btc_direction_confirm,
            stop_loss_start_remaining_sec=stop_loss_start_remaining_sec,
            stop_loss_end_remaining_sec=stop_loss_end_remaining_sec,
            max_entry_price=base_candidate.max_entry_price if custom_grid else max_entry_price,
            min_ask_gap=base_candidate.min_ask_gap,
            min_sell_price=base_candidate.min_sell_price,
            entry_ask_level=base_candidate.entry_ask_level,
            entry_buffer_ticks=entry_buffer_ticks,
            stop_loss_buffer_ticks=stop_loss_buffer_ticks,
        )
        trades, skips = backtest_indexed_candidate(window_indexes, candidate)
        summary_rows.append(
            summarize(
                candidate=candidate,
                windows_count=len(windows),
                trades=trades,
                skips=skips,
            )
        )
        if candidate.name in trade_candidate_names:
            write_trades(trades_dir / f"{candidate.name}_trades.csv", trades)
    write_summary(summary_out, summary_rows)
    return summary_rows


def _print_top(rows: list[dict[str, object]]) -> None:
    live_rows = [row for row in rows if int(row["entry_elapsed_sec"]) in (120, 150, 175, 180)]
    ranked = sorted(
        live_rows,
        key=lambda row: (
            -int(row["false_stop_losses"]),
            float(row["win_rate"]),
            float(row["realized_pnl"]),
            int(row["trades"]),
        ),
        reverse=True,
    )
    print("Top live-friendly candidates:")
    for row in ranked[:10]:
        print(
            "{candidate}: elapsed={entry_elapsed_sec}s min_leading={min_leading_ask} "
            "timeout={entry_timeout_sec}s cap={max_entry_price} sl={stop_loss_trigger} "
            "trades={trades} win_rate={win_rate:.2%} "
            "stops={stop_losses} false_stops={false_stop_losses} "
            "realized={realized_pnl} hold={hold_pnl}".format(**row)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument(
        "--summary-out",
        type=Path,
        default=Path("analysis/crowd_m1_125w_timing_comparison.csv"),
    )
    parser.add_argument(
        "--trades-dir",
        type=Path,
        default=Path("analysis/crowd_m1_125w_trades"),
    )
    parser.add_argument(
        "--stop-loss-start-remaining",
        type=float,
        default=55.0,
        help="Stop-loss window start, expressed as remaining seconds",
    )
    parser.add_argument(
        "--stop-loss-end-remaining",
        type=float,
        default=40.0,
        help="Stop-loss window end, expressed as remaining seconds",
    )
    parser.add_argument(
        "--entry-timeout-sec",
        type=float,
        default=0.0,
        help="Entry scan duration after entry_elapsed_sec; 0 keeps legacy point-in-time mode",
    )
    parser.add_argument(
        "--max-entry-price",
        type=float,
        default=0.75,
    )
    parser.add_argument(
        "--max-entry-values",
        default=None,
        help="Comma-separated max entry caps for custom grids, e.g. 0.72,0.74,0.76",
    )
    parser.add_argument(
        "--min-leading-values",
        default=None,
        help="Comma-separated min leading asks for custom grids, e.g. 0.60,0.62,0.64",
    )
    parser.add_argument(
        "--entry-buffer-ticks",
        type=float,
        default=0.0,
        help="Conservative BUY price buffer in ticks",
    )
    parser.add_argument(
        "--stop-loss-buffer-ticks",
        type=float,
        default=0.0,
        help="Conservative SELL price buffer in ticks",
    )
    parser.add_argument(
        "--elapsed-values",
        default=None,
        help="Comma-separated custom entry elapsed seconds; uses current crowd defaults",
    )
    parser.add_argument(
        "--entry-end-elapsed",
        type=float,
        default=None,
        help="End of dynamic entry scan band, expressed as elapsed seconds",
    )
    parser.add_argument(
        "--strong-move-values",
        default=None,
        help="Comma-separated BTC open-to-now thresholds in percent; use none to disable",
    )
    parser.add_argument(
        "--btc-direction-confirm",
        action="store_true",
        help="Require the Polymarket leading side to match BTC open-to-now direction",
    )
    parser.add_argument(
        "--stop-loss-drop-pct",
        type=float,
        default=None,
        help="Use entry_price * (1 - drop_pct) stop trigger instead of a fixed trigger",
    )
    args = parser.parse_args()
    if args.stop_loss_start_remaining <= args.stop_loss_end_remaining:
        parser.error("--stop-loss-start-remaining must be greater than --stop-loss-end-remaining")

    elapsed_values = _parse_float_list(args.elapsed_values)
    min_leading_values = _parse_float_list(args.min_leading_values)
    max_entry_values = _parse_float_list(args.max_entry_values)
    strong_move_values = _parse_optional_float_list(args.strong_move_values)

    rows = run_report(
        args.jsonl,
        summary_out=args.summary_out,
        trades_dir=args.trades_dir,
        trade_candidate_names=default_trade_candidate_names(),
        stop_loss_start_remaining_sec=args.stop_loss_start_remaining,
        stop_loss_end_remaining_sec=args.stop_loss_end_remaining,
        entry_timeout_sec=args.entry_timeout_sec,
        max_entry_price=args.max_entry_price,
        max_entry_values=max_entry_values,
        min_leading_values=min_leading_values,
        entry_buffer_ticks=args.entry_buffer_ticks,
        stop_loss_buffer_ticks=args.stop_loss_buffer_ticks,
        elapsed_values=elapsed_values,
        entry_end_elapsed_sec=args.entry_end_elapsed,
        strong_move_values=strong_move_values,
        btc_direction_confirm=args.btc_direction_confirm,
        stop_loss_drop_pct=args.stop_loss_drop_pct,
    )
    print("Backtest input:", args.jsonl)
    print("Windows:", rows[0]["windows"] if rows else 0)
    print("Summary CSV:", args.summary_out)
    print("Trade CSV dir:", args.trades_dir)
    print("Assumption: collector has no L2 depth sizes; best ask/bid are used as fill proxies.")
    print(
        "Execution model: entry ask plus %.1f ticks, stop-loss bid minus %.1f ticks."
        % (args.entry_buffer_ticks, args.stop_loss_buffer_ticks)
    )
    _print_top(rows)


if __name__ == "__main__":
    main()
