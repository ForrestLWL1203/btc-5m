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
        )
        if entry is None:
            continue
        skipped_no_quote += entry["skipped_no_quote"]
        skipped_price_cap += entry["skipped_price_cap"]
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
    }


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
) -> None:
    """Print a window-by-window replay like a dry-run log."""
    import datetime

    trades = collect_trades(
        windows, theta_pct, lo_rem, hi_rem,
        persistence_sec=persistence_sec,
        max_entry_price=max_entry_price,
        min_entry_price=min_entry_price,
        entry_delay_sec=entry_delay_sec,
    )
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

        print(f"  [{label}]  {w.direction.upper():4s}  "
              f"ENTER {side} @ {entry:.3f}  {shares:.2f}sh  {result}  "
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
        )
        return

    delays = [float(d) for d in args.delays.split(",")]
    thetas = [0.02, 0.03, 0.05, 0.08, 0.10]
    bands = [
        (60, 180),    # last 1-3 min (prior best)
        (60, 240),    # last 1-4 min
        (120, 240),   # last 2-4 min (early)
        (120, 270),   # last 2-4.5 min (very early)
        (180, 270),   # last 3-4.5 min (earliest)
    ]

    sim_cache: dict[tuple[float, int, int, int, float, float, float], dict] = {}
    trade_cache: dict[tuple[float, int, int, int, float, float, float], list[dict]] = {}

    def run_sim(theta: float, lo: int, hi: int, max_entry_price: float, delay: float) -> dict:
        key = (theta, lo, hi, args.persistence, max_entry_price, delay, 0.0)
        if key not in sim_cache:
            sim_cache[key] = simulate(
                windows,
                theta,
                lo,
                hi,
                persistence_sec=args.persistence,
                max_entry_price=max_entry_price,
                entry_delay_sec=delay,
            )
        return sim_cache[key]

    def run_trades(theta: float, lo: int, hi: int, max_entry_price: float, delay: float) -> list[dict]:
        key = (theta, lo, hi, args.persistence, max_entry_price, delay, 0.0)
        if key not in trade_cache:
            trade_cache[key] = collect_trades(
                windows,
                theta,
                lo,
                hi,
                persistence_sec=args.persistence,
                max_entry_price=max_entry_price,
                entry_delay_sec=delay,
            )
        return trade_cache[key]

    for delay in delays:
        print(f"\n=== Delay={delay:.0f}s  (persistence={args.persistence}s, "
              f"cap={args.max_entry_price}) ===")
        hdr = (f"{'theta%':>7} {'band':>10} {'N':>4} {'wins':>5} "
               f"{'winR':>6} {'CIlo':>6} {'avgP':>6} {'slip':>6} "
               f"{'EV/trd':>7} {'flip':>4} {'capSk':>6}")
        print(hdr)
        for theta in thetas:
            for lo, hi in bands:
                r = run_sim(theta, lo, hi, args.max_entry_price, delay)
                print(
                    f"{theta:>7.2f} {f'[{lo},{hi}]':>10} {r['entries']:>4d} "
                    f"{r['wins']:>5d} {r['win_rate']:>6.1%} "
                    f"{r['win_rate_ci_lo']:>6.1%} {r['avg_entry_price']:>6.3f} "
                    f"{r['avg_slippage']:>+6.3f} {r['ev_per_trade']:>+7.4f} "
                    f"{r['direction_flipped']:>4d} {r['skipped_price_cap']:>6d}"
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
            "open_price": w.open_price,
            "win": win,
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
) -> dict | None:
    """Return the first valid entry candidate for one window.

    This centralizes the original signal/entry rules so simulate() and
    collect_trades() do not each rebuild the same per-window state.
    """
    if not w.btc:
        return None

    entry_start = w.start_epoch + (300 - hi_rem)
    entry_end = w.start_epoch + (300 - lo_rem)
    start_idx = bisect_right(w.btc_ts, entry_start - 1e-9)
    skipped_no_quote = 0
    skipped_price_cap = 0
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

        direction_up = move_pct > 0
        lookup = w.up_ask_lookup if direction_up else w.down_ask_lookup
        sig_price = lookup.at(tick.ts, max_data_age) if lookup else None
        fill_ts = tick.ts + entry_delay_sec
        if fill_ts >= w.end_epoch:
            continue
        fill_price = lookup.at(fill_ts, max_data_age) if lookup else None
        if fill_price is None or fill_price <= 0 or fill_price >= 1:
            skipped_no_quote += 1
            continue
        if fill_price > max_entry_price:
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
            "signal_ts": tick.ts,
            "fill_ts": fill_ts,
            "direction_up": direction_up,
            "sig_price": sig_price,
            "fill_price": fill_price,
            "skipped_no_quote": skipped_no_quote,
            "skipped_price_cap": skipped_price_cap,
            "direction_flipped": direction_flipped,
        }
    if skipped_no_quote or skipped_price_cap:
        return {
            "signal_ts": None,
            "fill_ts": None,
            "direction_up": None,
            "sig_price": None,
            "fill_price": None,
            "skipped_no_quote": skipped_no_quote,
            "skipped_price_cap": skipped_price_cap,
            "direction_flipped": False,
        }
    return None


if __name__ == "__main__":
    main()
