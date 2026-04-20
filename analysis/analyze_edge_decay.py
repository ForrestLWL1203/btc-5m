"""Edge decay analysis — find optimal hold time from collected data.

For each edge event (|edge| > threshold), tracks UP/DOWN token price
at various hold durations to compute expected PnL per hold time.

Output: half-life, optimal exit time, expected PnL, and calibration
for edge_exit_fraction.

Usage: python3.11 analysis/analyze_edge_decay.py data/collect_btc-updown-5m_*.jsonl
"""

import math
import sys
from bisect import bisect_right
from collections import defaultdict

try:
    from analysis.common import compute_velocity, dedup_poly, fit_reaction_model, load_data
except ModuleNotFoundError:
    from common import compute_velocity, dedup_poly, fit_reaction_model, load_data


HOLD_TIMES = [0.2, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]
EDGE_THRESHOLD = 0.01
NOISE_THRESHOLD = 0.005
POLY_LOOKBACK = 10.0  # max seconds to look forward for price


def analyze_edge_decay(binance: list, poly_dedup: list, model: dict):
    """For each edge event, track price at hold_time offsets."""
    beta = model["beta"]

    up_lookup_ts = sorted(
        [(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0]
    )
    down_lookup_ts = sorted(
        [(p.ts, p.mid) for p in poly_dedup if p.token == "down"], key=lambda x: x[0]
    )

    if not up_lookup_ts or not down_lookup_ts or len(binance) < 10:
        return None

    up_ts = [t for t, _ in up_lookup_ts]
    up_prices = [p for _, p in up_lookup_ts]
    down_ts = [t for t, _ in down_lookup_ts]
    down_prices = [p for _, p in down_lookup_ts]

    velocities, _ = compute_velocity(binance)
    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]

    events = []

    for i in range(len(binance)):
        tick = binance[i]
        # BTC features
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

        if abs(edge) < EDGE_THRESHOLD:
            continue

        direction = "up" if edge > 0 else "down"

        # Get current token price
        idx_up = bisect_right(up_ts, tick.ts) - 1
        if idx_up < 0 or tick.ts - up_ts[idx_up] > 1.0:
            continue
        up_now = up_prices[idx_up]

        idx_down = bisect_right(down_ts, tick.ts) - 1
        if idx_down < 0 or tick.ts - down_ts[idx_down] > 1.0:
            continue
        down_now = down_prices[idx_down]

        if direction == "up":
            entry_price = up_now
            lookup_ts, lookup_prices = up_ts, up_prices
        else:
            entry_price = down_now
            lookup_ts, lookup_prices = down_ts, down_prices

        if entry_price <= 0 or entry_price > 0.90:
            continue

        # Track price at each hold_time
        samples = []
        for ht in HOLD_TIMES:
            future_ts = tick.ts + ht
            idx_f = bisect_right(lookup_ts, future_ts) - 1
            if idx_f < 0:
                continue
            if lookup_ts[idx_f] - future_ts > 1.0:
                continue
            future_price = lookup_prices[idx_f]
            samples.append({
                "dt": ht,
                "price": round(future_price, 4),
            })

        if len(samples) < 2:
            continue

        events.append({
            "ts": round(tick.ts, 3),
            "edge": round(edge, 4),
            "direction": direction,
            "entry_price": round(entry_price, 4),
            "btc_price": round(tick.price, 1),
            "ret_2s": round(ret_2s, 4),
            "velocity": round(vel, 2),
            "samples": samples,
        })

    return events


def fit_decay(events: list[dict]):
    """Fit exponential decay to edge ratio over time."""
    xs_all, ys_all = [], []

    for ev in events:
        entry_edge = abs(ev["edge"])
        if entry_edge <= 0:
            continue
        for s in ev["samples"]:
            # Edge ratio approximation: (price - entry) / entry_edge
            direction = ev["direction"]
            entry = ev["entry_price"]
            price = s["price"]
            if direction == "up":
                realized = price - entry
            else:
                realized = entry - price
            ratio = realized / entry_edge if entry_edge > 0 else 0
            if ratio <= 0:
                continue
            xs_all.append(s["dt"])
            ys_all.append(math.log(ratio))

    if len(xs_all) < 5:
        return None

    n = len(xs_all)
    sx = sum(xs_all)
    sy = sum(ys_all)
    sxx = sum(x * x for x in xs_all)
    sxy = sum(x * y for x, y in zip(xs_all, ys_all))

    denom = n * sxx - sx * sx
    if denom == 0:
        return None

    slope = (n * sxy - sx * sy) / denom
    k = -slope
    if k <= 0:
        return None

    half_life = math.log(2) / k
    return k, half_life


def main():
    if len(sys.argv) < 2:
        print("Usage: python3.11 analysis/analyze_edge_decay.py data/collect_btc-updown-5m_*.jsonl")
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

        events = analyze_edge_decay(binance, deduped, model)
        if events:
            all_events.extend(events)
            print(f"  Found {len(events)} edge events")

    if not all_events:
        print("No edge events found")
        return

    # PnL by hold time
    pnl_by_time = defaultdict(list)
    for ev in all_events:
        direction = ev["direction"]
        entry = ev["entry_price"]
        for s in ev["samples"]:
            if direction == "up":
                pnl = s["price"] - entry
            else:
                pnl = entry - s["price"]
            pnl_by_time[s["dt"]].append(pnl)

    # Decay fit
    decay = fit_decay(all_events)

    print(f"\n{'='*60}")
    print("EDGE DECAY ANALYSIS")
    print(f"{'='*60}")
    print(f"Total edge events: {len(all_events)}")
    print(f"Edge threshold: {EDGE_THRESHOLD}")

    # Direction breakdown
    up_count = sum(1 for e in all_events if e["direction"] == "up")
    down_count = len(all_events) - up_count
    print(f"Direction: UP={up_count} DOWN={down_count}")

    if decay:
        k, hl = decay
        print(f"\nEdge decay rate (k): {k:.3f}/s")
        print(f"Edge half-life: {hl:.2f}s")

    print(f"\n{'─'*50}")
    print(f"{'Hold Time':>10} {'Avg PnL':>10} {'Win Rate':>10} {'Samples':>10}")
    print(f"{'─'*50}")

    best_t = None
    best_pnl = -999
    times = sorted(pnl_by_time.keys())

    for t in times:
        pnls = pnl_by_time[t]
        avg = sum(pnls) / len(pnls)
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls) * 100
        print(f"{t:>9.1f}s {avg:>+10.4f} {win_rate:>9.1f}% {len(pnls):>10}")
        if avg > best_pnl:
            best_pnl = avg
            best_t = t

    print(f"{'─'*50}")
    print(f"\nBest exit time: {best_t}s")
    print(f"Expected PnL: {best_pnl:+.4f} per trade")

    # Fee analysis
    print(f"\n{'='*60}")
    print("FEE & PROFITABILITY CHECK")
    print(f"{'='*60}")
    fee_rate = 0.02  # Polymarket 2% fee on winning trades
    avg_entry = sum(e["entry_price"] for e in all_events) / len(all_events)
    print(f"Average entry price: {avg_entry:.3f}")
    print(f"Fee on $1 trade (2% of payout): ~${(1 - avg_entry) * fee_rate:.4f}")
    print(f"Best PnL per trade: {best_pnl:+.4f}")
    if best_pnl > 0:
        print(f"After fee: {best_pnl - (1 - avg_entry) * fee_rate:+.4f}")
        if best_pnl > (1 - avg_entry) * fee_rate:
            print("→ Strategy has positive edge after fees")
        else:
            print("→ WARNING: Edge consumed by fees")
    else:
        print("→ WARNING: Negative expected PnL before fees")

    # Calibration recommendation
    print(f"\n{'='*60}")
    print("STRATEGY CALIBRATION")
    print(f"{'='*60}")
    if decay:
        hl = decay[1]
        print(f"Recommended edge_exit_fraction: {math.exp(-math.log(2) * best_t / hl):.2f}")
        print(f"  (edge threshold * fraction = exit when edge decays to this fraction)")
    print(f"Recommended hold time before exit: {best_t}s")
    print(f"Current config: edge_exit_fraction=0.5, hold=dynamic")

    # Sample event details
    print(f"\n{'='*60}")
    print("SAMPLE EVENTS (top 5 by edge)")
    print(f"{'='*60}")
    top_events = sorted(all_events, key=lambda e: abs(e["edge"]), reverse=True)[:5]
    for ev in top_events:
        print(f"\n  ts={ev['ts']} dir={ev['direction']} edge={ev['edge']:+.4f} "
              f"entry={ev['entry_price']:.3f} btc={ev['btc_price']:.1f}")
        for s in ev["samples"]:
            pnl = (s["price"] - ev["entry_price"] if ev["direction"] == "up"
                   else ev["entry_price"] - s["price"])
            print(f"    hold={s['dt']:.1f}s → price={s['price']:.4f} pnl={pnl:+.4f}")


if __name__ == "__main__":
    main()
