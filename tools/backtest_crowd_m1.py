"""Backtest crowd_m1 candidates against collected JSONL data.

Collector files contain Binance ticks plus Polymarket best bid/ask quotes, but
not full L2 book sizes. Entry fillability is therefore approximated with the
target leg best ask being at or below the configured cap.
"""

from __future__ import annotations

import argparse
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
    stop_loss_start_remaining_sec: float = 60.0
    stop_loss_end_remaining_sec: float = 45.0
    max_entry_price: float = 0.75
    min_ask_gap: float = 0.0
    min_sell_price: float = 0.20


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


def _quote_at(rows: list[dict], ts: float) -> dict[str, dict]:
    quotes: dict[str, dict] = {}
    for row in rows:
        row_ts = row.get("ts")
        if not isinstance(row_ts, (int, float)) or row_ts > ts:
            continue
        if (
            row.get("src") == "poly"
            and row.get("token") in ("up", "down")
            and row.get("ask") is not None
        ):
            quotes[str(row["token"])] = row
    return quotes


def _entry_level_for_leading_ask(leading_ask: float) -> int:
    if leading_ask <= 0.64:
        return 5
    if leading_ask <= 0.68:
        return 4
    if leading_ask <= 0.72:
        return 2
    if leading_ask <= 0.75:
        return 1
    return 9


def _stop_loss_exit(
    rows: list[dict],
    *,
    side: str,
    start_ts: float,
    candidate: Candidate,
) -> tuple[Optional[float], Optional[float]]:
    if candidate.stop_loss_trigger is None:
        return None, None
    stop_price = max(candidate.min_sell_price, candidate.stop_loss_trigger)
    active_start = start_ts + (WINDOW_SEC - candidate.stop_loss_start_remaining_sec)
    active_end = start_ts + (WINDOW_SEC - candidate.stop_loss_end_remaining_sec)
    for row in rows:
        row_ts = row.get("ts")
        if not isinstance(row_ts, (int, float)):
            continue
        if row_ts < active_start or row_ts > active_end:
            continue
        if row.get("src") != "poly" or row.get("token") != side:
            continue
        bid = row.get("bid")
        if bid is None:
            continue
        bid = float(bid)
        if candidate.min_sell_price <= bid <= stop_price:
            return row_ts, bid
    return None, None


def backtest_candidate(
    windows: list[tuple[list[dict], dict]],
    candidate: Candidate,
) -> tuple[list[Trade], dict[str, int]]:
    trades: list[Trade] = []
    skips = {
        "missing_quote": 0,
        "ask_gap": 0,
        "leading": 0,
        "cap": 0,
    }
    for rows, outcome in windows:
        end_ts = float(outcome["ts"])
        start_ts = end_ts - WINDOW_SEC
        entry_ts = start_ts + candidate.entry_elapsed_sec
        quotes = _quote_at(rows, entry_ts)
        if "up" not in quotes or "down" not in quotes:
            skips["missing_quote"] += 1
            continue

        up_ask = float(quotes["up"]["ask"])
        down_ask = float(quotes["down"]["ask"])
        side = "up" if up_ask >= down_ask else "down"
        leading_ask = max(up_ask, down_ask)
        lower_ask = min(up_ask, down_ask)
        ask_gap = leading_ask - lower_ask

        if ask_gap < candidate.min_ask_gap:
            skips["ask_gap"] += 1
            continue
        if leading_ask < candidate.min_leading_ask:
            skips["leading"] += 1
            continue

        entry_price = float(quotes[side]["ask"])
        if entry_price > candidate.max_entry_price:
            skips["cap"] += 1
            continue

        outcome_side = str(outcome.get("direction"))
        won = side == outcome_side
        exit_ts = end_ts
        exit_reason = "settlement"
        exit_price = 1.0 if won else 0.0
        stop_ts, stop_price = _stop_loss_exit(
            rows,
            side=side,
            start_ts=start_ts,
            candidate=candidate,
        )
        if stop_ts is not None and stop_price is not None:
            exit_ts = stop_ts
            exit_price = stop_price
            exit_reason = "stop_loss"

        shares = 1.0 / entry_price
        realized_pnl = shares * exit_price - 1.0
        hold_pnl = shares * (1.0 if won else 0.0) - 1.0
        false_stop = exit_reason == "stop_loss" and won
        trades.append(
            Trade(
                candidate=candidate.name,
                window=str(outcome["window"]),
                start_ts=start_ts,
                entry_ts=entry_ts,
                exit_ts=exit_ts,
                side=side,
                outcome=outcome_side,
                result="WIN" if won else "LOSS",
                exit_reason=exit_reason,
                leading_ask=leading_ask,
                lower_ask=lower_ask,
                ask_gap=ask_gap,
                entry_level=_entry_level_for_leading_ask(leading_ask),
                entry_price=entry_price,
                exit_price=exit_price,
                realized_pnl=realized_pnl,
                hold_pnl=hold_pnl,
                false_stop=false_stop,
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
        "min_leading_ask": candidate.min_leading_ask,
        "stop_loss_trigger": (
            "" if candidate.stop_loss_trigger is None else candidate.stop_loss_trigger
        ),
        "stop_loss_start_remaining_sec": int(candidate.stop_loss_start_remaining_sec),
        "stop_loss_end_remaining_sec": int(candidate.stop_loss_end_remaining_sec),
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
    }


def write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "candidate",
        "entry_elapsed_sec",
        "min_leading_ask",
        "stop_loss_trigger",
        "stop_loss_start_remaining_sec",
        "stop_loss_end_remaining_sec",
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
                }
            )


def run_report(
    jsonl: Path,
    *,
    summary_out: Path,
    trades_dir: Path,
    trade_candidate_names: set[str],
    stop_loss_start_remaining_sec: float = 60.0,
    stop_loss_end_remaining_sec: float = 45.0,
) -> list[dict[str, object]]:
    windows = _load_windows(jsonl)
    summary_rows: list[dict[str, object]] = []
    for base_candidate in default_candidates():
        candidate = Candidate(
            name=base_candidate.name,
            entry_elapsed_sec=base_candidate.entry_elapsed_sec,
            min_leading_ask=base_candidate.min_leading_ask,
            stop_loss_trigger=base_candidate.stop_loss_trigger,
            stop_loss_start_remaining_sec=stop_loss_start_remaining_sec,
            stop_loss_end_remaining_sec=stop_loss_end_remaining_sec,
            max_entry_price=base_candidate.max_entry_price,
            min_ask_gap=base_candidate.min_ask_gap,
            min_sell_price=base_candidate.min_sell_price,
        )
        trades, skips = backtest_candidate(windows, candidate)
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
    live_rows = [row for row in rows if int(row["entry_elapsed_sec"]) in (120, 150, 180)]
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
            "sl={stop_loss_trigger} trades={trades} win_rate={win_rate:.2%} "
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
        default=60.0,
        help="Stop-loss window start, expressed as remaining seconds",
    )
    parser.add_argument(
        "--stop-loss-end-remaining",
        type=float,
        default=45.0,
        help="Stop-loss window end, expressed as remaining seconds",
    )
    args = parser.parse_args()
    if args.stop_loss_start_remaining <= args.stop_loss_end_remaining:
        parser.error("--stop-loss-start-remaining must be greater than --stop-loss-end-remaining")

    rows = run_report(
        args.jsonl,
        summary_out=args.summary_out,
        trades_dir=args.trades_dir,
        trade_candidate_names=default_trade_candidate_names(),
        stop_loss_start_remaining_sec=args.stop_loss_start_remaining,
        stop_loss_end_remaining_sec=args.stop_loss_end_remaining,
    )
    print("Backtest input:", args.jsonl)
    print("Windows:", rows[0]["windows"] if rows else 0)
    print("Summary CSV:", args.summary_out)
    print("Trade CSV dir:", args.trades_dir)
    print("Assumption: collector has no L2 depth sizes; best ask/bid are used as fill proxies.")
    _print_top(rows)


if __name__ == "__main__":
    main()
