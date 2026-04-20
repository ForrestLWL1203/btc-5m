"""Edge quality bucket analysis — edge × flow × velocity × persistence.

Breaks down edge events by size, flow alignment, velocity consistency,
and persistence to find the true alpha signal.

Usage: python3.11 analysis/analyze_edge_quality.py data/collect_btc-updown-5m_*.jsonl
"""

import sys
from bisect import bisect_right
from collections import defaultdict

try:
    from analysis.common import compute_velocity, dedup_poly, fit_reaction_model, load_data
except ModuleNotFoundError:
    from common import compute_velocity, dedup_poly, fit_reaction_model, load_data

EDGE_THRESHOLD = 0.005  # lower threshold to see all buckets
NOISE_THRESHOLD = 0.005
HOLD_TIME = 2.0  # evaluate PnL at this hold time

EDGE_BUCKETS = [(0.005, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)]
FLOW_BUCKETS = [(0, 0.1), (0.1, 0.2), (0.2, 0.4), (0.4, 1.0)]


def bucket_label(value, buckets):
    for lo, hi in buckets:
        if lo <= abs(value) < hi:
            return f"{lo:.3f}-{hi:.3f}"
    return None


def analyze_edge_quality(binance, poly_dedup, snapshots, model):
    beta = model["beta"]

    up_ts = sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0])
    down_ts = sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "down"], key=lambda x: x[0])
    if not up_ts or not down_ts or len(binance) < 10:
        return None

    up_t = [t for t, _ in up_ts]
    up_p = [p for _, p in up_ts]
    down_t = [t for t, _ in down_ts]
    down_p = [p for _, p in down_ts]

    velocities, _ = compute_velocity(binance)
    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]

    # Build snapshot lookup for flow data
    snap_by_ts = {round(s.ts, 1): s for s in snapshots if s.btc_flow}

    events = []
    for i in range(len(binance)):
        tick = binance[i]
        idx_2s = bisect_right(btc_ts, tick.ts - 2.0) - 1
        idx_5s = bisect_right(btc_ts, tick.ts - 5.0) - 1
        if idx_2s < 0 or idx_5s < 0:
            continue

        ret_2s = (tick.price - btc_prices[idx_2s]) / btc_prices[idx_2s] * 100
        ret_5s = (tick.price - btc_prices[idx_5s]) / btc_prices[idx_5s] * 100
        if abs(ret_2s) < NOISE_THRESHOLD and abs(ret_5s) < NOISE_THRESHOLD:
            continue

        vel = velocities[i]
        edge = (beta["ret_2s"] * ret_2s + beta["ret_5s"] * ret_5s
                + beta["velocity"] * vel + beta["abs_vel"] * abs(vel))

        if abs(edge) < EDGE_THRESHOLD:
            continue

        direction = "up" if edge > 0 else "down"

        # Current token price
        idx_up = bisect_right(up_t, tick.ts) - 1
        if idx_up < 0 or tick.ts - up_t[idx_up] > 1.0:
            continue
        up_now = up_p[idx_up]

        idx_down = bisect_right(down_t, tick.ts) - 1
        if idx_down < 0 or tick.ts - down_t[idx_down] > 1.0:
            continue
        down_now = down_p[idx_down]

        if direction == "up":
            entry_price = up_now
            lookup_t, lookup_p = up_t, up_p
        else:
            entry_price = down_now
            lookup_t, lookup_p = down_t, down_p

        if entry_price <= 0 or entry_price > 0.90:
            continue

        # Future price at HOLD_TIME
        future_ts = tick.ts + HOLD_TIME
        idx_f = bisect_right(lookup_t, future_ts) - 1
        if idx_f < 0 or abs(lookup_t[idx_f] - future_ts) > 1.0:
            continue
        future_price = lookup_p[idx_f]

        if direction == "up":
            pnl = future_price - entry_price
        else:
            pnl = entry_price - future_price

        # Flow data from nearest snapshot
        flow_imbalance_500ms = 0.0
        snap_key = round(tick.ts, 1)
        snap = snap_by_ts.get(snap_key)
        if snap and snap.btc_flow:
            f500 = snap.btc_flow.get("500ms", {})
            imbalance = f500.get("imbalance", 0)
            # Align: if edge says UP, positive imbalance = supportive
            if direction == "up":
                flow_imbalance_500ms = imbalance
            else:
                flow_imbalance_500ms = -imbalance

        # Velocity consistency: check vel at 1s and 2s ago
        idx_1s = bisect_right(btc_ts, tick.ts - 1.0) - 1
        vel_1s = velocities[idx_1s] if idx_1s >= 0 else 0
        vel_consistent = (vel > 0 and vel_1s > 0) or (vel < 0 and vel_1s < 0)

        events.append({
            "edge": edge,
            "edge_abs": abs(edge),
            "direction": direction,
            "entry_price": entry_price,
            "pnl": pnl,
            "flow_aligned": flow_imbalance_500ms,
            "vel": vel,
            "vel_1s": vel_1s,
            "vel_consistent": vel_consistent,
        })

    return events


def main():
    if len(sys.argv) < 2:
        print("Usage: python3.11 analysis/analyze_edge_quality.py data/collect_btc-updown-5m_*.jsonl")
        return

    files = sys.argv[1:]
    all_events = []

    for f in files:
        print(f"Loading {f}...")
        binance, poly, snapshots, outcome = load_data(f)
        if not binance or not poly:
            continue

        deduped = dedup_poly(poly)

        model = fit_reaction_model(binance, deduped)
        if "error" in model:
            print(f"  Model error: {model['error']}")
            continue

        print(f"  Model R²={model['r2']:.4f} samples={model['samples']}")

        events = analyze_edge_quality(binance, deduped, snapshots, model)
        if events:
            all_events.extend(events)
            print(f"  {len(events)} edge events")

    if not all_events:
        print("No events")
        return

    # ── 1. Edge bucket analysis ──
    print(f"\n{'='*70}")
    print("EDGE BUCKET ANALYSIS (hold = 2.0s)")
    print(f"{'='*70}")
    print(f"{'Edge Bucket':>15} {'Count':>8} {'WinRate':>8} {'Avg PnL':>10} {'EV/trade':>10}")
    print(f"{'─'*70}")

    for lo, hi in EDGE_BUCKETS:
        bucket = [e for e in all_events if lo <= e["edge_abs"] < hi]
        if not bucket:
            continue
        wins = sum(1 for e in bucket if e["pnl"] > 0)
        avg_pnl = sum(e["pnl"] for e in bucket) / len(bucket)
        avg_win = sum(e["pnl"] for e in bucket if e["pnl"] > 0) / max(wins, 1)
        avg_loss = sum(e["pnl"] for e in bucket if e["pnl"] <= 0) / max(len(bucket) - wins, 1)
        ev = (wins / len(bucket)) * avg_win + (1 - wins / len(bucket)) * avg_loss
        print(f"{lo:.3f}-{hi:.3f} {len(bucket):>8} {wins/len(bucket)*100:>7.1f}% {avg_pnl:>+10.4f} {ev:>+10.4f}")

    # ── 2. Edge × Flow cross-tab ──
    print(f"\n{'='*70}")
    print("EDGE × FLOW ALIGNED (500ms imbalance)")
    print(f"{'='*70}")
    print(f"{'Edge':>15} {'Flow':>15} {'Count':>8} {'WinRate':>8} {'Avg PnL':>10}")
    print(f"{'─'*70}")

    for elo, ehi in EDGE_BUCKETS:
        for flo, fhi in FLOW_BUCKETS:
            bucket = [e for e in all_events
                      if elo <= e["edge_abs"] < ehi
                      and flo <= e["flow_aligned"] < fhi]
            if len(bucket) < 5:
                continue
            wins = sum(1 for e in bucket if e["pnl"] > 0)
            avg_pnl = sum(e["pnl"] for e in bucket) / len(bucket)
            print(f"{elo:.3f}-{ehi:.3f} {flo:.1f}-{fhi:.1f} {len(bucket):>8} "
                  f"{wins/len(bucket)*100:>7.1f}% {avg_pnl:>+10.4f}")

    # ── 3. Velocity consistency ──
    print(f"\n{'='*70}")
    print("VELOCITY CONSISTENCY (vel_1s same sign as vel)")
    print(f"{'='*70}")
    for label, consistent in [("Consistent", True), ("Inconsistent", False)]:
        bucket = [e for e in all_events if e["vel_consistent"] == consistent]
        if not bucket:
            continue
        wins = sum(1 for e in bucket if e["pnl"] > 0)
        avg_pnl = sum(e["pnl"] for e in bucket) / len(bucket)
        print(f"  {label:>15}: n={len(bucket):>5} win={wins/len(bucket)*100:.1f}% avg_pnl={avg_pnl:+.4f}")

    # ── 4. Edge + Flow + Velocity combined ──
    print(f"\n{'='*70}")
    print("COMBINED FILTER: edge>0.03 + flow>0.15 + vel_consistent")
    print(f"{'='*70}")
    combined = [e for e in all_events
                if e["edge_abs"] >= 0.03
                and e["flow_aligned"] >= 0.15
                and e["vel_consistent"]]
    baseline = [e for e in all_events if e["edge_abs"] >= 0.03]

    for label, bucket in [("Baseline (edge>0.03)", baseline), ("Combined filter", combined)]:
        if not bucket:
            print(f"  {label}: no events")
            continue
        wins = sum(1 for e in bucket if e["pnl"] > 0)
        avg_pnl = sum(e["pnl"] for e in bucket) / len(bucket)
        avg_entry = sum(e["entry_price"] for e in bucket) / len(bucket)
        fee = (1 - avg_entry) * 0.02
        print(f"  {label}:")
        print(f"    n={len(bucket)} win={wins/len(bucket)*100:.1f}% avg_pnl={avg_pnl:+.4f} "
              f"fee={fee:.4f} net={avg_pnl-fee:+.4f}")

    # ── 5. Multi-threshold scan ──
    print(f"\n{'='*70}")
    print("THRESHOLD SCAN (PnL by min edge, 2s hold)")
    print(f"{'='*70}")
    print(f"{'Min Edge':>10} {'Count':>8} {'WinRate':>8} {'Avg PnL':>10} {'Net PnL':>10}")
    print(f"{'─'*60}")
    for threshold in [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10]:
        bucket = [e for e in all_events if e["edge_abs"] >= threshold]
        if not bucket:
            continue
        wins = sum(1 for e in bucket if e["pnl"] > 0)
        avg_pnl = sum(e["pnl"] for e in bucket) / len(bucket)
        avg_entry = sum(e["entry_price"] for e in bucket) / len(bucket)
        fee = (1 - avg_entry) * 0.02
        print(f"{threshold:>10.3f} {len(bucket):>8} {wins/len(bucket)*100:>7.1f}% "
              f"{avg_pnl:>+10.4f} {avg_pnl - fee:>+10.4f}")


if __name__ == "__main__":
    main()
