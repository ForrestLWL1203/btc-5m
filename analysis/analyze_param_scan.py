"""Parameter scan for latency-arb window controls.

Evaluates candidate settings for:
  - edge_threshold
  - cooldown_sec
  - max_entries_per_window
  - max_edge_reentry

The scan reuses the regression model fit from each input JSONL file, then runs
an approximate greedy execution policy:
  - enter when an edge event passes the filters
  - hold for a fixed duration
  - wait for hold_sec + cooldown_sec before the next entry
  - stop after max_entries_per_window fills

Usage:
  python3.11 analysis/analyze_param_scan.py data/collect_btc-updown-5m_*.jsonl
"""

from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import dataclass
from itertools import product
from statistics import mean

try:
    from analysis.common import compute_velocity, dedup_poly, fit_reaction_model, load_data
except ModuleNotFoundError:
    from common import compute_velocity, dedup_poly, fit_reaction_model, load_data

DEFAULT_EDGE_THRESHOLDS = [0.02, 0.025, 0.03]
DEFAULT_COOLDOWNS = [0.5, 1.0, 1.5]
DEFAULT_ENTRY_CAPS = [3, 5, 7]
DEFAULT_EDGE_REENTRIES = [2, 4, 6]
NOISE_THRESHOLD = 0.005
FEE_RATE = 0.02


@dataclass
class CandidateEvent:
    ts: float
    edge_abs: float
    direction: str
    entry_price: float
    future_price: float

    @property
    def gross_pnl(self) -> float:
        if self.direction == "up":
            return self.future_price - self.entry_price
        return self.entry_price - self.future_price

    @property
    def fee(self) -> float:
        if self.gross_pnl <= 0:
            return 0.0
        return (1.0 - self.entry_price) * FEE_RATE

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fee


@dataclass
class ScanResult:
    edge_threshold: float
    cooldown_sec: float
    max_entries: int
    max_edge_reentry: int
    total_trades: int
    mean_trades_per_window: float
    mean_edge: float
    gross_pnl_per_trade: float
    net_pnl_per_trade: float
    total_net_pnl: float
    windows_with_cap_hit: int


def _parse_float_list(raw: str) -> list[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_int_list(raw: str) -> list[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def collect_candidate_events(
    filepath: str,
    edge_threshold: float,
    hold_sec: float,
    max_entry_price: float,
    entry_window_sec: float,
    event_spacing_sec: float,
) -> list[CandidateEvent]:
    binance, poly, snapshots, outcome = load_data(filepath)
    poly_dedup = dedup_poly(poly)
    model = fit_reaction_model(binance, poly_dedup)
    if "error" in model:
        return []

    beta = model["beta"]

    up_pairs = sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0])
    down_pairs = sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "down"], key=lambda x: x[0])
    if not up_pairs or not down_pairs:
        return []

    up_ts = [t for t, _ in up_pairs]
    up_prices = [p for _, p in up_pairs]
    down_ts = [t for t, _ in down_pairs]
    down_prices = [p for _, p in down_pairs]

    velocities, _ = compute_velocity(binance)
    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]
    window_start = btc_ts[0]

    raw_events: list[CandidateEvent] = []

    for i, tick in enumerate(binance):
        elapsed = tick.ts - window_start
        if elapsed < 0 or elapsed > entry_window_sec:
            continue

        idx_2s = bisect_right(btc_ts, tick.ts - 2.0) - 1
        idx_5s = bisect_right(btc_ts, tick.ts - 5.0) - 1
        if idx_2s < 0 or idx_5s < 0:
            continue

        ret_2s = (tick.price - btc_prices[idx_2s]) / btc_prices[idx_2s] * 100
        ret_5s = (tick.price - btc_prices[idx_5s]) / btc_prices[idx_5s] * 100
        if abs(ret_2s) < NOISE_THRESHOLD and abs(ret_5s) < NOISE_THRESHOLD:
            continue

        vel = velocities[i]
        edge = (
            beta["ret_2s"] * ret_2s
            + beta["ret_5s"] * ret_5s
            + beta["velocity"] * vel
            + beta["abs_vel"] * abs(vel)
        )
        edge_abs = abs(edge)
        if edge_abs < edge_threshold:
            continue

        idx_up_now = bisect_right(up_ts, tick.ts) - 1
        idx_down_now = bisect_right(down_ts, tick.ts) - 1
        if idx_up_now < 0 or idx_down_now < 0:
            continue
        if tick.ts - up_ts[idx_up_now] > 1.0 or tick.ts - down_ts[idx_down_now] > 1.0:
            continue

        direction = "up" if edge > 0 else "down"
        if direction == "up":
            entry_price = up_prices[idx_up_now]
            lookup_ts = up_ts
            lookup_prices = up_prices
        else:
            entry_price = down_prices[idx_down_now]
            lookup_ts = down_ts
            lookup_prices = down_prices

        if entry_price <= 0 or entry_price > max_entry_price:
            continue

        idx_future = bisect_right(lookup_ts, tick.ts + hold_sec) - 1
        if idx_future < 0:
            continue
        if abs(lookup_ts[idx_future] - (tick.ts + hold_sec)) > 1.0:
            continue

        raw_events.append(
            CandidateEvent(
                ts=tick.ts,
                edge_abs=edge_abs,
                direction=direction,
                entry_price=entry_price,
                future_price=lookup_prices[idx_future],
            )
        )

    # Collapse bursts of near-identical events into the strongest edge sample.
    deduped: list[CandidateEvent] = []
    for event in raw_events:
        if deduped and event.ts - deduped[-1].ts < event_spacing_sec:
            if event.edge_abs > deduped[-1].edge_abs:
                deduped[-1] = event
            continue
        deduped.append(event)
    return deduped


def simulate_window(
    events: list[CandidateEvent],
    hold_sec: float,
    cooldown_sec: float,
    max_entries: int,
) -> tuple[list[CandidateEvent], bool]:
    next_allowed_ts = float("-inf")
    selected: list[CandidateEvent] = []
    for event in events:
        if len(selected) >= max_entries:
            break
        if event.ts < next_allowed_ts:
            continue
        selected.append(event)
        next_allowed_ts = event.ts + hold_sec + cooldown_sec
    cap_hit = len(selected) >= max_entries and len(events) > len(selected)
    return selected, cap_hit


def scan_parameters(
    files: list[str],
    edge_thresholds: list[float],
    cooldowns: list[float],
    entry_caps: list[int],
    edge_reentries: list[int],
    hold_sec: float,
    max_entry_price: float,
    entry_window_sec: float,
    event_spacing_sec: float,
) -> list[ScanResult]:
    event_cache: dict[float, dict[str, list[CandidateEvent]]] = {}
    for threshold in edge_thresholds:
        event_cache[threshold] = {
            path: collect_candidate_events(
                path,
                edge_threshold=threshold,
                hold_sec=hold_sec,
                max_entry_price=max_entry_price,
                entry_window_sec=entry_window_sec,
                event_spacing_sec=event_spacing_sec,
            )
            for path in files
        }

    results: list[ScanResult] = []
    for threshold, cooldown_sec, max_entries, max_edge_reentry in product(
        edge_thresholds, cooldowns, entry_caps, edge_reentries
    ):
        per_window_counts = []
        per_window_cap_hits = 0
        chosen_events: list[CandidateEvent] = []

        for path in files:
            effective_cap = min(max_entries, max_edge_reentry + 1)
            selected, cap_hit = simulate_window(
                event_cache[threshold][path],
                hold_sec=hold_sec,
                cooldown_sec=cooldown_sec,
                max_entries=effective_cap,
            )
            per_window_counts.append(len(selected))
            chosen_events.extend(selected)
            if cap_hit:
                per_window_cap_hits += 1

        total_trades = len(chosen_events)
        mean_edge = mean([e.edge_abs for e in chosen_events]) if chosen_events else 0.0
        gross_per_trade = mean([e.gross_pnl for e in chosen_events]) if chosen_events else 0.0
        net_per_trade = mean([e.net_pnl for e in chosen_events]) if chosen_events else 0.0
        total_net = sum(e.net_pnl for e in chosen_events)

        results.append(
            ScanResult(
                edge_threshold=threshold,
                cooldown_sec=cooldown_sec,
                max_entries=max_entries,
                max_edge_reentry=max_edge_reentry,
                total_trades=total_trades,
                mean_trades_per_window=mean(per_window_counts) if per_window_counts else 0.0,
                mean_edge=mean_edge,
                gross_pnl_per_trade=gross_per_trade,
                net_pnl_per_trade=net_per_trade,
                total_net_pnl=total_net,
                windows_with_cap_hit=per_window_cap_hits,
            )
        )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan latency-arb parameter combinations.")
    parser.add_argument("files", nargs="+", help="Input collect_*.jsonl files")
    parser.add_argument("--edge-thresholds", default="0.02,0.025,0.03")
    parser.add_argument("--cooldowns", default="0.5,1.0,1.5")
    parser.add_argument("--entry-caps", default="3,5,7")
    parser.add_argument("--edge-reentries", default="2,4,6")
    parser.add_argument("--hold-sec", type=float, default=2.0)
    parser.add_argument("--max-entry-price", type=float, default=0.70)
    parser.add_argument("--entry-window-sec", type=float, default=240.0)
    parser.add_argument("--event-spacing-sec", type=float, default=0.05)
    parser.add_argument("--top", type=int, default=8, help="How many top combinations to print")
    args = parser.parse_args()

    edge_thresholds = _parse_float_list(args.edge_thresholds)
    cooldowns = _parse_float_list(args.cooldowns)
    entry_caps = _parse_int_list(args.entry_caps)
    edge_reentries = _parse_int_list(args.edge_reentries)

    results = scan_parameters(
        files=args.files,
        edge_thresholds=edge_thresholds,
        cooldowns=cooldowns,
        entry_caps=entry_caps,
        edge_reentries=edge_reentries,
        hold_sec=args.hold_sec,
        max_entry_price=args.max_entry_price,
        entry_window_sec=args.entry_window_sec,
        event_spacing_sec=args.event_spacing_sec,
    )

    results.sort(
        key=lambda r: (r.total_net_pnl, r.net_pnl_per_trade, r.mean_edge, -r.mean_trades_per_window),
        reverse=True,
    )

    print("\n" + "=" * 92)
    print("LATENCY-ARB PARAMETER SCAN")
    print("=" * 92)
    print(f"files={len(args.files)} hold={args.hold_sec:.1f}s max_entry_price={args.max_entry_price:.2f} "
          f"entry_window={args.entry_window_sec:.0f}s")
    print(f"edge_thresholds={edge_thresholds}")
    print(f"cooldowns={cooldowns}")
    print(f"entry_caps={entry_caps}")
    print(f"edge_reentries={edge_reentries}")
    print("-" * 92)
    print(f"{'edge':>7} {'cool':>6} {'cap':>5} {'edge_r':>7} {'trades':>7} {'avg/win':>8} "
          f"{'avg_edge':>9} {'gross/t':>9} {'net/t':>9} {'net_total':>10} {'cap_hits':>8}")
    print("-" * 92)
    for result in results[:args.top]:
        print(
            f"{result.edge_threshold:>7.3f} "
            f"{result.cooldown_sec:>6.1f} "
            f"{result.max_entries:>5} "
            f"{result.max_edge_reentry:>7} "
            f"{result.total_trades:>7} "
            f"{result.mean_trades_per_window:>8.2f} "
            f"{result.mean_edge:>9.4f} "
            f"{result.gross_pnl_per_trade:>+9.4f} "
            f"{result.net_pnl_per_trade:>+9.4f} "
            f"{result.total_net_pnl:>+10.4f} "
            f"{result.windows_with_cap_hit:>8}"
        )

    print("\nRecommended starting point:")
    best = results[0]
    print(
        f"  edge_threshold={best.edge_threshold:.3f}, cooldown_sec={best.cooldown_sec:.1f}, "
        f"max_entries_per_window={best.max_entries}, max_edge_reentry={best.max_edge_reentry}"
    )


if __name__ == "__main__":
    main()
