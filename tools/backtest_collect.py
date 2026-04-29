"""Backtest the active paired-window strategy against collected JSONL data.

This replays collector output from ``tools/collect_data.py``. Collector files
contain Binance ticks plus Polymarket best bid/ask quotes, but not full L2 book
sizes, so entry fillability is approximated with target-leg best ask <= cap.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polybot.config_loader import build_trade_config, load_config


WINDOW_SEC = 300.0


@dataclass
class Quote:
    mid: float
    bid: float
    ask: float
    ts: float


@dataclass
class Trade:
    window: str
    start_ts: float
    entry_ts: float
    side: str
    outcome: str
    entry_ask: float
    exit_ts: float
    exit_reason: str
    exit_price: float
    exit_mid: float
    amount: float
    shares: float
    signal_strength: float
    active_theta_pct: float
    move_pct: float
    past_move_pct: float
    remaining_sec: float
    mark_pnl: float
    settlement_pnl: float
    result: str


@dataclass
class RiskState:
    daily_wins: int = 0
    daily_losses: int = 0
    consecutive_losses: int = 0
    daily_realized_pnl: float = 0.0
    consecutive_loss_amount: float = 0.0
    windows_to_skip: int = 0
    last_reset_date: Optional[str] = None
    min_trades_for_eval: int = 30


def _utc8_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(
        timezone(timedelta(hours=8))
    ).strftime("%Y-%m-%d")


def _reset_daily_if_needed(state: RiskState, ts: float) -> None:
    date = _utc8_date(ts)
    if state.last_reset_date != date:
        state.last_reset_date = date
        state.daily_wins = 0
        state.daily_losses = 0
        state.daily_realized_pnl = 0.0
        state.consecutive_losses = 0
        state.consecutive_loss_amount = 0.0
        state.windows_to_skip = 0


def _process_risk(state: RiskState, won: bool, pnl: float, trade_config) -> None:
    state.daily_realized_pnl += pnl
    if won:
        state.daily_wins += 1
        state.consecutive_losses = 0
        state.consecutive_loss_amount = 0.0
    else:
        state.daily_losses += 1
        state.consecutive_losses += 1
        state.consecutive_loss_amount += abs(pnl)

        if state.consecutive_losses >= 5:
            state.windows_to_skip = 2
            state.consecutive_losses = 0

        if (
            trade_config.consecutive_loss_amount_limit is not None
            and state.consecutive_loss_amount >= trade_config.consecutive_loss_amount_limit
        ):
            state.windows_to_skip = max(
                state.windows_to_skip,
                trade_config.consecutive_loss_pause_windows,
            )
            state.consecutive_loss_amount = 0.0

    total_trades = state.daily_wins + state.daily_losses
    if total_trades >= state.min_trades_for_eval:
        win_rate = state.daily_wins / total_trades
        if win_rate < 0.50 and state.windows_to_skip == 0:
            state.windows_to_skip = 5

    if (
        trade_config.daily_loss_amount_limit is not None
        and state.daily_realized_pnl <= -trade_config.daily_loss_amount_limit
    ):
        state.windows_to_skip = max(
            state.windows_to_skip,
            trade_config.daily_loss_pause_windows,
        )


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


def _price_at_or_before(history: list[tuple[float, float]], ts: float) -> Optional[float]:
    selected = None
    for item_ts, price in history:
        if item_ts <= ts:
            selected = price
        else:
            break
    return selected


def _active_theta(strat: dict, elapsed: float) -> float:
    theta_start = strat.get("theta_start_pct")
    theta_end = strat.get("theta_end_pct")
    if theta_start is None or theta_end is None:
        return float(strat.get("theta_pct", 0.036))
    entry_start_remaining = float(strat.get("entry_start_remaining_sec", 255.0))
    entry_end_remaining = float(strat.get("entry_end_remaining_sec", 180.0))
    start_elapsed = max(0.0, WINDOW_SEC - entry_start_remaining)
    end_elapsed = max(start_elapsed, WINDOW_SEC - entry_end_remaining)
    if end_elapsed <= start_elapsed:
        return float(theta_end)
    progress = max(0.0, min(1.0, (elapsed - start_elapsed) / (end_elapsed - start_elapsed)))
    return float(theta_start) + progress * (float(theta_end) - float(theta_start))


def _fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def backtest(path: Path, cfg: dict) -> tuple[list[Trade], dict]:
    strat = cfg.get("strategy", {})
    trade_config = build_trade_config(cfg)
    windows = _load_windows(path)
    risk = RiskState()
    trades: list[Trade] = []
    skipped_by_risk = 0
    no_signal = 0
    signal_no_fill = 0

    entry_start_remaining = float(strat.get("entry_start_remaining_sec", 255.0))
    entry_end_remaining = float(strat.get("entry_end_remaining_sec", 180.0))
    persistence_sec = float(strat.get("persistence_sec", 10.0))
    min_move_ratio = float(strat.get("min_move_ratio", 0.7))
    max_entry_price = float(strat.get("max_entry_price", 0.75))

    for rows, outcome in windows:
        end_ts = float(outcome["ts"])
        start_ts = end_ts - WINDOW_SEC
        _reset_daily_if_needed(risk, start_ts)
        if risk.windows_to_skip > 0:
            risk.windows_to_skip -= 1
            skipped_by_risk += 1
            continue

        open_btc = float(outcome["open"])
        btc_history: list[tuple[float, float]] = []
        latest_btc: Optional[float] = None
        quotes: dict[str, Quote] = {}
        committed_direction: Optional[str] = None
        saw_signal = False
        trade: Optional[Trade] = None

        for row in rows:
            ts = float(row["ts"])
            src = row.get("src")
            if src == "binance":
                latest_btc = float(row["price"])
                btc_history.append((ts, latest_btc))
                continue
            if src != "poly":
                continue

            token = row.get("token")
            if token not in ("up", "down"):
                continue
            if row.get("mid") is None or row.get("bid") is None or row.get("ask") is None:
                continue
            quotes[token] = Quote(
                mid=float(row["mid"]),
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                ts=ts,
            )

            # Runtime computes entry signals only on UP token updates.
            if token != "up" or trade is not None:
                continue
            if latest_btc is None or open_btc <= 0:
                continue
            elapsed = ts - start_ts
            if elapsed < 0:
                continue
            remaining = WINDOW_SEC - elapsed
            if remaining > entry_start_remaining or remaining < entry_end_remaining:
                continue

            past_btc = _price_at_or_before(btc_history, ts - persistence_sec)
            if past_btc is None:
                continue

            move_pct = (latest_btc - open_btc) / open_btc * 100.0
            theta = _active_theta(strat, elapsed)
            if abs(move_pct) < theta:
                continue

            past_move_pct = (past_btc - open_btc) / open_btc * 100.0
            if (move_pct > 0) != (past_move_pct > 0):
                continue
            if abs(move_pct) < abs(past_move_pct) * min_move_ratio:
                continue

            direction = "up" if move_pct > 0 else "down"
            if committed_direction is not None and direction != committed_direction:
                continue
            committed_direction = direction
            saw_signal = True

            quote = quotes.get(direction)
            if quote is None:
                continue
            if quote.ask > max_entry_price:
                continue

            strength = abs(move_pct) / theta if theta > 0 else 0.0
            amount = trade_config.amount_for_signal_strength(strength)
            shares = amount / quote.ask if quote.ask > 0 else 0.0

            exit_ts = end_ts
            exit_reason = "settlement"
            exit_price = 0.0
            exit_mid = quote.mid
            for later in rows:
                if float(later.get("ts", 0.0)) < ts:
                    continue
                if (
                    later.get("src") == "poly"
                    and later.get("token") == direction
                    and later.get("mid") is not None
                ):
                    exit_mid = float(later["mid"])

                if (
                    trade_config.stop_loss_enabled
                    and quote.ask >= trade_config.stop_loss_disable_below_entry_price
                    and later.get("src") == "poly"
                    and later.get("token") == direction
                    and later.get("bid") is not None
                ):
                    later_ts = float(later["ts"])
                    stop_remaining = WINDOW_SEC - (later_ts - start_ts)
                    if stop_remaining > trade_config.stop_loss_start_remaining_sec:
                        continue
                    if stop_remaining < trade_config.stop_loss_end_remaining_sec:
                        continue
                    bid = float(later["bid"])
                    stop_price = max(
                        trade_config.stop_loss_min_sell_price,
                        trade_config.stop_loss_trigger_price,
                    )
                    if trade_config.stop_loss_min_sell_price <= bid <= stop_price:
                        exit_ts = later_ts
                        exit_reason = "stop_loss"
                        exit_price = bid
                        exit_mid = float(later["mid"]) if later.get("mid") is not None else bid
                        break

            won = direction == outcome.get("direction")
            if exit_reason == "stop_loss":
                mark_pnl = shares * exit_price - amount
                settlement_pnl = mark_pnl
                result = "WIN" if mark_pnl >= 0 else "LOSS"
            else:
                mark_pnl = shares * exit_mid - amount
                settlement_pnl = (shares if won else 0.0) - amount
                result = "WIN" if won else "LOSS"
            trade = Trade(
                window=str(outcome["window"]),
                start_ts=start_ts,
                entry_ts=ts,
                side=direction,
                outcome=str(outcome.get("direction")),
                entry_ask=quote.ask,
                exit_ts=exit_ts,
                exit_reason=exit_reason,
                exit_price=exit_price if exit_reason == "stop_loss" else exit_mid,
                exit_mid=exit_mid,
                amount=amount,
                shares=shares,
                signal_strength=strength,
                active_theta_pct=theta,
                move_pct=move_pct,
                past_move_pct=past_move_pct,
                remaining_sec=remaining,
                mark_pnl=mark_pnl,
                settlement_pnl=settlement_pnl,
                result=result,
            )
            break

        if trade is None:
            if saw_signal:
                signal_no_fill += 1
            else:
                no_signal += 1
            continue

        trades.append(trade)
        _process_risk(risk, trade.result == "WIN", trade.mark_pnl, trade_config)

    summary = {
        "windows": len(windows),
        "trades": len(trades),
        "wins": sum(1 for t in trades if t.result == "WIN"),
        "losses": sum(1 for t in trades if t.result == "LOSS"),
        "skipped_by_risk": skipped_by_risk,
        "no_signal": no_signal,
        "signal_no_fill": signal_no_fill,
        "mark_pnl": sum(t.mark_pnl for t in trades),
        "settlement_pnl": sum(t.settlement_pnl for t in trades),
        "amount": sum(t.amount for t in trades),
        "stop_losses": sum(1 for t in trades if t.exit_reason == "stop_loss"),
    }
    return trades, summary


def write_trades(path: Path, trades: list[Trade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "window", "start_utc", "entry_utc", "exit_utc", "side", "outcome",
        "result", "exit_reason", "entry_ask", "exit_price", "exit_mid",
        "amount", "shares", "signal_strength",
        "active_theta_pct", "move_pct", "past_move_pct", "remaining_sec",
        "mark_pnl", "settlement_pnl",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow({
                "window": trade.window,
                "start_utc": _fmt_ts(trade.start_ts),
                "entry_utc": _fmt_ts(trade.entry_ts),
                "exit_utc": _fmt_ts(trade.exit_ts),
                "side": trade.side,
                "outcome": trade.outcome,
                "result": trade.result,
                "exit_reason": trade.exit_reason,
                "entry_ask": round(trade.entry_ask, 4),
                "exit_price": round(trade.exit_price, 4),
                "exit_mid": round(trade.exit_mid, 4),
                "amount": round(trade.amount, 4),
                "shares": round(trade.shares, 6),
                "signal_strength": round(trade.signal_strength, 4),
                "active_theta_pct": round(trade.active_theta_pct, 5),
                "move_pct": round(trade.move_pct, 5),
                "past_move_pct": round(trade.past_move_pct, 5),
                "remaining_sec": round(trade.remaining_sec, 3),
                "mark_pnl": round(trade.mark_pnl, 6),
                "settlement_pnl": round(trade.settlement_pnl, 6),
            })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--config", default="paired_window_early_entry_dry.yaml")
    parser.add_argument("--stop-loss-enabled", action="store_true")
    parser.add_argument("--trades-out", type=Path)
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.stop_loss_enabled:
        params = cfg.setdefault("params", {})
        stop_loss = params.setdefault("stop_loss", {})
        stop_loss["enabled"] = True
    trades, summary = backtest(args.jsonl, cfg)
    if args.trades_out:
        write_trades(args.trades_out, trades)

    win_rate = summary["wins"] / summary["trades"] if summary["trades"] else 0.0
    roi_mark = summary["mark_pnl"] / summary["amount"] if summary["amount"] else 0.0
    roi_settlement = summary["settlement_pnl"] / summary["amount"] if summary["amount"] else 0.0

    print("Backtest input:", args.jsonl)
    print("Config:", args.config)
    if args.stop_loss_enabled:
        print("Stop loss: enabled by CLI override")
    print("Assumption: collector has no L2 depth sizes; target best ask <= cap is used as fill proxy.")
    print("Assumption: stop-loss fill uses held-leg best bid as a proxy for bid depth execution.")
    print(f"Windows: {summary['windows']}")
    print(f"Trades: {summary['trades']}  Wins: {summary['wins']}  Losses: {summary['losses']}  Win rate: {win_rate:.2%}")
    print(f"Stop losses: {summary['stop_losses']}")
    print(f"Skipped by risk: {summary['skipped_by_risk']}  No signal: {summary['no_signal']}  Signal but no fill proxy: {summary['signal_no_fill']}")
    print(f"Total amount: {summary['amount']:.2f}")
    print(f"Mark PnL: {summary['mark_pnl']:.4f}  ROI: {roi_mark:.2%}")
    print(f"Settlement PnL: {summary['settlement_pnl']:.4f}  ROI: {roi_settlement:.2%}")
    if trades:
        print("First trade:", trades[0])
        print("Last trade:", trades[-1])
    if args.trades_out:
        print("Trades CSV:", args.trades_out)


if __name__ == "__main__":
    main()
