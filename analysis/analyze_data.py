"""Analyze collected JSONL data: measure latency, build reaction function.

Reads data/collect_*.jsonl. Supports both raw tick data and 100ms snapshots.
Uses bisect for O(log n) lookups, lag-compensated edge detection, and
linear regression for reaction function modeling.

Usage: python3.11 analysis/analyze_data.py [data_file]
"""

import sys
import os
from bisect import bisect_left, bisect_right
from collections import defaultdict
try:
    from analysis.common import (
        BinanceTick,
        PolyUpdate,
        Snapshot,
        SeriesLookup,
        compute_velocity,
        dedup_poly,
        fit_reaction_model,
        load_data,
    )
except ModuleNotFoundError:
    from common import (
        BinanceTick,
        PolyUpdate,
        Snapshot,
        SeriesLookup,
        compute_velocity,
        dedup_poly,
        fit_reaction_model,
        load_data,
    )


DATA_DIR = "data"


def measure_latency(binance: list[BinanceTick], poly_dedup: list[PolyUpdate]) -> dict:
    """Cross-correlation: BTC delta vs UP token delta at various offsets.
    Uses bisect for O(log n) lookups. Peak correlation offset = reaction latency.
    """
    up_lookup = SeriesLookup(
        sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0])
    )
    if not up_lookup._ts or len(binance) < 20:
        return {"error": "insufficient data"}

    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]
    btc_base = btc_prices[0]
    start_ts = max(btc_ts[0], up_lookup._ts[0])
    end_ts = min(btc_ts[-1], up_lookup._ts[-1])

    results = {}
    for offset in [0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]:
        btc_deltas = []
        up_deltas = []

        sample_ts = start_ts + 2.0
        while sample_ts < end_ts - 2.0:
            # BTC 2s delta via bisect
            idx_now = bisect_right(btc_ts, sample_ts) - 1
            idx_2s = bisect_right(btc_ts, sample_ts - 2.0) - 1
            if idx_now < 0 or idx_2s < 0:
                sample_ts += 0.5
                continue
            btc_now = btc_prices[idx_now]
            btc_2s = btc_prices[idx_2s]

            # UP delta at offset
            up_now = up_lookup.at(sample_ts + offset)
            up_2s = up_lookup.at(sample_ts + offset - 2.0)

            if btc_now and btc_2s and up_now is not None and up_2s is not None:
                btc_deltas.append(btc_now - btc_2s)
                up_deltas.append(up_now - up_2s)

            sample_ts += 0.5

        if len(btc_deltas) > 5:
            n = len(btc_deltas)
            mean_b = sum(btc_deltas) / n
            mean_u = sum(up_deltas) / n
            cov = sum((btc_deltas[i] - mean_b) * (up_deltas[i] - mean_u) for i in range(n)) / n
            std_b = (sum((x - mean_b) ** 2 for x in btc_deltas) / n) ** 0.5
            std_u = (sum((x - mean_u) ** 2 for x in up_deltas) / n) ** 0.5
            corr = cov / (std_b * std_u) if std_b > 0 and std_u > 0 else 0

            results[f"offset_{offset}s"] = {
                "correlation": round(corr, 4),
                "samples": n,
                "btc_std": round(std_b, 2),
                "up_std": round(std_u, 4),
            }

    return results


def compute_edge_opportunities(binance: list[BinanceTick], poly_dedup: list[PolyUpdate],
                               model: dict, lag: float = 0.5,
                               threshold_pct: float = 0.01) -> list[dict]:
    """Find moments where BTC moved but Poly hasn't caught up.

    Uses regression model for fair price. Edge = fair_price - market_price.
    Noise filter: skip if BTC move < threshold.
    """
    up_lookup = SeriesLookup(
        sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0])
    )
    if not up_lookup._ts or len(binance) < 10 or "beta" not in model:
        return []

    beta = model["beta"]
    velocities, _ = compute_velocity(binance)
    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]
    window_start = btc_ts[0]

    edges = []
    for i in range(len(binance)):
        tick = binance[i]
        elapsed = tick.ts - window_start

        # BTC returns
        idx_2s = bisect_right(btc_ts, tick.ts - 2.0) - 1
        idx_5s = bisect_right(btc_ts, tick.ts - 5.0) - 1
        if idx_2s < 0 or idx_5s < 0:
            continue
        ret_2s = (tick.price - btc_prices[idx_2s]) / btc_prices[idx_2s] * 100
        ret_5s = (tick.price - btc_prices[idx_5s]) / btc_prices[idx_5s] * 100

        # Noise filter
        if abs(ret_2s) < threshold_pct and abs(ret_5s) < threshold_pct:
            continue

        vel = velocities[i]
        features = [ret_2s, ret_5s, vel, abs(vel)]
        predicted_delta = sum(beta[name] * feat for name, feat in zip(
            ["ret_2s", "ret_5s", "velocity", "abs_vel"], features))

        # Current UP price
        up_now = up_lookup.at(tick.ts)
        # UP price lag seconds later (what it became)
        up_after = up_lookup.at(tick.ts + lag)

        if up_now is None:
            continue

        fair_up = up_now + predicted_delta
        edge = fair_up - up_now  # model says UP should move this much

        # Only report if edge is significant AND market hasn't fully reacted yet
        if abs(edge) > 0.005:
            actual_move = (up_after - up_now) if up_after is not None else None
            edges.append({
                "ts": round(tick.ts, 3),
                "elapsed_s": round(elapsed, 1),
                "btc_price": tick.price,
                "ret_2s": round(ret_2s, 4),
                "ret_5s": round(ret_5s, 4),
                "up_now": round(up_now, 4),
                "fair_up": round(fair_up, 4),
                "edge": round(edge, 4),
                "actual_move": round(actual_move, 4) if actual_move is not None else None,
                "direction": "up" if edge > 0 else "down",
            })

    return edges


def analyze_snapshots(snapshots: list[Snapshot]):
    """Analyze snapshot data for latency, reaction, order flow, and expiry effects."""
    if not snapshots:
        print("  No snapshot data (old format file)")
        return

    # Detect format version
    has_v3 = any(s.trigger != "poll" for s in snapshots)
    has_flow = any(s.btc_flow for s in snapshots)
    has_vol = any(s.btc_volatility > 0 for s in snapshots)
    has_expiry = any(s.time_to_expiry > 0 for s in snapshots)
    has_spread = any(s.up_spread > 0 for s in snapshots)

    print(f"\n{'='*70}")
    trigger_counts = defaultdict(int)
    for s in snapshots:
        trigger_counts[s.trigger] += 1
    trigger_str = ", ".join(f"{k}={v}" for k, v in sorted(trigger_counts.items()))
    print(f"SNAPSHOT ANALYSIS ({len(snapshots)} snapshots, triggers: {trigger_str})")
    print(f"{'='*70}")

    ts_list = [s.ts for s in snapshots]
    btc_list = [s.btc_price for s in snapshots]
    up_list = [s.up_mid for s in snapshots]
    down_list = [s.down_mid for s in snapshots]

    duration = ts_list[-1] - ts_list[0]
    avg_interval = duration / max(len(snapshots) - 1, 1)
    print(f"  Duration: {duration:.1f}s, avg interval: {avg_interval*1000:.0f}ms")
    print(f"  BTC: {btc_list[0]:.2f} → {btc_list[-1]:.2f}")
    print(f"  UP:  {up_list[0]:.4f} → {up_list[-1]:.4f}")
    print(f"  DOWN: {down_list[0]:.4f} → {down_list[-1]:.4f}")

    # Freshness / data age
    if any(s.btc_age > 0 for s in snapshots):
        btc_ages = [s.btc_age * 1000 for s in snapshots if s.btc_age > 0]
        up_ages = [s.up_age * 1000 for s in snapshots if s.up_age > 0]
        print(f"\n  Data age:")
        if btc_ages:
            print(f"    BTC: mean={sum(btc_ages)/len(btc_ages):.0f}ms p99={sorted(btc_ages)[int(len(btc_ages)*0.99)]:.0f}ms")
        if up_ages:
            print(f"    UP:  mean={sum(up_ages)/len(up_ages):.0f}ms p99={sorted(up_ages)[int(len(up_ages)*0.99)]:.0f}ms")

    # Multi-scale order flow analysis
    if has_flow:
        flow_keys = set()
        for s in snapshots:
            flow_keys.update(s.btc_flow.keys())
        for fk in sorted(flow_keys):
            buy_total = sum(s.btc_flow.get(fk, {}).get("buy", 0) for s in snapshots)
            sell_total = sum(s.btc_flow.get(fk, {}).get("sell", 0) for s in snapshots)
            imbalances = [s.btc_flow.get(fk, {}).get("imbalance", 0)
                          for s in snapshots if s.btc_flow.get(fk, {}).get("imbalance", 0) != 0]
            print(f"\n  Order flow ({fk}):")
            print(f"    Total buy: {buy_total:.4f} BTC, sell: {sell_total:.4f} BTC")
            if imbalances:
                mean_ib = sum(imbalances) / len(imbalances)
                print(f"    Imbalance: mean={mean_ib:+.4f} [{min(imbalances):+.4f}, {max(imbalances):+.4f}]")

    # Spread analysis
    if has_spread:
        up_spreads = [s.up_spread for s in snapshots if s.up_spread > 0]
        down_spreads = [s.down_spread for s in snapshots if s.down_spread > 0]
        print(f"\n  Spreads:")
        if up_spreads:
            print(f"    UP:   mean={sum(up_spreads)/len(up_spreads):.4f} min={min(up_spreads):.4f} max={max(up_spreads):.4f}")
        if down_spreads:
            print(f"    DOWN: mean={sum(down_spreads)/len(down_spreads):.4f} min={min(down_spreads):.4f} max={max(down_spreads):.4f}")

    # Volatility analysis
    if has_vol:
        vols = [s.btc_volatility for s in snapshots if s.btc_volatility > 0]
        if vols:
            print(f"\n  BTC volatility (2s rolling):")
            print(f"    Mean: {sum(vols)/len(vols):.8f}")
            print(f"    Max:  {max(vols):.8f}")

    # Cross-correlation using snapshots (time-based, not index-based)
    print(f"\n  Snapshot cross-correlation (BTC ret vs UP delta):")
    best_offset = None
    best_corr = -2

    for offset_s in [0, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0]:
        btc_deltas = []
        up_deltas = []
        window_s = 2.0  # 2s lookback

        for i in range(len(snapshots)):
            ts_i = ts_list[i]
            # Find snapshot window_s ago
            j = i - 1
            while j >= 0 and ts_list[j] > ts_i - window_s:
                j -= 1
            if j < 0 or btc_list[j] == 0:
                continue
            btc_ret = (btc_list[i] - btc_list[j]) / btc_list[j]
            if abs(btc_ret) < 0.00005:
                continue

            # Find snapshot at ts_i + offset_s
            target_ts = ts_i + offset_s
            k = i
            while k < len(ts_list) - 1 and ts_list[k] < target_ts:
                k += 1
            if k >= len(ts_list):
                continue
            if abs(ts_list[k] - target_ts) > 0.3:
                continue

            btc_deltas.append(btc_ret)
            up_deltas.append(up_list[k] - up_list[i])

        if len(btc_deltas) > 10:
            n = len(btc_deltas)
            mean_b = sum(btc_deltas) / n
            mean_u = sum(up_deltas) / n
            cov = sum((btc_deltas[i] - mean_b) * (up_deltas[i] - mean_u) for i in range(n)) / n
            std_b = (sum((x - mean_b) ** 2 for x in btc_deltas) / n) ** 0.5
            std_u = (sum((x - mean_u) ** 2 for x in up_deltas) / n) ** 0.5
            corr = cov / (std_b * std_u) if std_b > 0 and std_u > 0 else 0
            if corr > best_corr:
                best_corr = corr
                best_offset = offset_s
            marker = " <-- PEAK" if best_offset == offset_s else ""
            print(f"    offset={offset_s:>5.2f}s: corr={corr:+.4f}  n={n}{marker}")

    if best_offset is not None:
        print(f"\n  → Peak correlation at offset={best_offset:.2f}s (corr={best_corr:+.4f})")

    # Time-to-expiry analysis: how does UP price behave near expiry?
    if has_expiry:
        print(f"\n  Expiry effect (UP price vs time remaining):")
        buckets = defaultdict(list)
        for s in snapshots:
            tte = s.time_to_expiry
            if tte <= 0:
                continue
            bucket = int(tte / 30) * 30  # 30s buckets
            buckets[bucket].append(s.up_mid)
        for bucket in sorted(buckets.keys()):
            vals = buckets[bucket]
            avg = sum(vals) / len(vals)
            print(f"    tte={bucket:>3}-{bucket+30:>3}s: avg_UP={avg:.4f} (n={len(vals)})")

    # Order flow as predictor
    if has_flow:
        flow_key = "100ms" if "100ms" in flow_keys else list(flow_keys)[0] if flow_keys else None
        if flow_key:
            print(f"\n  Order flow ({flow_key}) → UP price prediction:")
            flow_edges = []
            for i in range(10, len(snapshots) - 10):
                fi = snapshots[i].btc_flow.get(flow_key, {}).get("imbalance", 0)
                if abs(fi) < 0.1:
                    continue
                up_delta = up_list[min(i + 10, len(up_list) - 1)] - up_list[i]
                flow_edges.append((fi, up_delta))

            if len(flow_edges) > 5:
                correct = sum(1 for fi, ud in flow_edges if (fi > 0 and ud > 0) or (fi < 0 and ud < 0))
                print(f"    Flow imbalance → UP direction (next 10 snaps): {correct}/{len(flow_edges)} "
                      f"({correct/len(flow_edges)*100:.0f}%)")


def analyze_file(filepath: str):
    print(f"\n{'='*70}")
    print(f"ANALYZING: {filepath}")
    print(f"{'='*70}")

    binance, poly, snapshots, outcome = load_data(filepath)
    print(f"\nRaw: {len(binance)} binance, {len(poly)} poly, {len(snapshots)} snaps, outcome={bool(outcome)}")

    if outcome:
        print(f"Window: {outcome.get('window')}")
        print(f"Open: {outcome.get('open')}, Close: {outcome.get('close')}, Direction: {outcome.get('direction')}")

    # Dedup poly
    poly_dedup = dedup_poly(poly)
    print(f"Poly deduped: {len(poly_dedup)} (removed {len(poly) - len(poly_dedup)} duplicates)")

    up_changes = [p for p in poly_dedup if p.token == "up"]
    down_changes = [p for p in poly_dedup if p.token == "down"]
    print(f"  UP price changes: {len(up_changes)}")
    print(f"  DOWN price changes: {len(down_changes)}")

    # BTC summary
    if binance:
        btc_prices = [b.price for b in binance]
        print(f"\nBTC: {min(btc_prices):.2f} - {max(btc_prices):.2f} (range: {max(btc_prices)-min(btc_prices):.2f})")
        print(f"  Start: {binance[0].price:.2f} → End: {binance[-1].price:.2f}")
        delta = (binance[-1].price - binance[0].price) / binance[0].price * 100
        print(f"  Change: {delta:+.4f}%")

        # Trade side analysis
        buys = sum(1 for b in binance if b.side == "buy")
        sells = sum(1 for b in binance if b.side == "sell")
        buy_vol = sum(b.qty for b in binance if b.side == "buy")
        sell_vol = sum(b.qty for b in binance if b.side == "sell")
        print(f"  Trades: {buys} buys ({buy_vol:.3f} BTC) / {sells} sells ({sell_vol:.3f} BTC)")

    # UP/DOWN price summary
    if up_changes:
        up_mids = [p.mid for p in up_changes]
        print(f"\nUP token: {up_mids[0]:.4f} → {up_mids[-1]:.4f} (range: {min(up_mids):.4f}-{max(up_mids):.4f})")
    if down_changes:
        down_mids = [p.mid for p in down_changes]
        print(f"DOWN token: {down_mids[0]:.4f} → {down_mids[-1]:.4f} (range: {min(down_mids):.4f}-{max(down_mids):.4f})")

    # Velocity/acceleration
    velocities, accelerations = compute_velocity(binance)
    if velocities:
        mean_v = sum(velocities) / len(velocities)
        std_v = (sum((v - mean_v) ** 2 for v in velocities) / len(velocities)) ** 0.5
        print(f"\nBTC velocity (1s window):")
        print(f"  Mean: {mean_v:.2f} $/s, Std: {std_v:.2f} $/s")
        print(f"  Max: {max(velocities):.2f} $/s, Min: {min(velocities):.2f} $/s")
    if accelerations:
        abs_acc = [abs(a) for a in accelerations]
        print(f"BTC acceleration:")
        print(f"  Mean |acc|: {sum(abs_acc)/len(abs_acc):.2f} $/s²")
        print(f"  Max |acc|: {max(abs_acc):.2f} $/s²")

    # Cross-correlation (bisect-based)
    print(f"\n{'='*70}")
    print("CROSS-CORRELATION: BTC delta vs UP token delta at offsets")
    print(f"{'='*70}")
    corr_results = measure_latency(binance, poly_dedup)
    if "error" not in corr_results:
        best_offset = None
        best_corr = -2
        all_corrs = []
        for key, val in sorted(corr_results.items()):
            offset = key.replace("offset_", "").replace("s", "")
            all_corrs.append((offset, val["correlation"], val))
            if val["correlation"] > best_corr:
                best_corr = val["correlation"]
                best_offset = offset
        for offset, corr, val in all_corrs:
            marker = " <-- PEAK" if offset == best_offset else ""
            print(f"  offset={offset:>5}s: corr={corr:+.4f}  n={val['samples']}  btc_std={val['btc_std']:.2f}  up_std={val['up_std']:.4f}{marker}")
        print(f"\n  → Best correlation at offset={best_offset}s (corr={best_corr:+.4f})")

        # Use detected lag for model fitting
        detected_lag = float(best_offset)
    else:
        print(f"  {corr_results['error']}")
        detected_lag = 0.5

    # Snapshot analysis (if available)
    analyze_snapshots(snapshots)

    # Regression model
    print(f"\n{'='*70}")
    print(f"REACTION MODEL (linear regression, lag={detected_lag}s)")
    print(f"{'='*70}")
    model = fit_reaction_model(binance, poly_dedup, lag=detected_lag)
    if "beta" in model:
        print(f"  Features: ret_2s, ret_5s, velocity, |velocity|")
        print(f"  Coefficients:")
        for name, val in model["beta"].items():
            print(f"    {name:>10}: {val:+.6f}")
        print(f"  R²: {model['r2']:.4f}")
        print(f"  Samples: {model['samples']} (noise-filtered)")
        print(f"  Target mean: {model['y_mean']:.4f}, std: {model['y_std']:.4f}")
    else:
        print(f"  {model.get('error', 'unknown error')}")

    # Edge opportunities (model-based)
    print(f"\n{'='*70}")
    print("EDGE OPPORTUNITIES (model-based, lag-compensated)")
    print(f"{'='*70}")
    if "beta" in model:
        edges = compute_edge_opportunities(binance, poly_dedup, model, lag=detected_lag)
        if edges:
            print(f"  Found {len(edges)} potential edge moments")
            edges_sorted = sorted(edges, key=lambda x: abs(x["edge"]), reverse=True)
            # Validate: how often did predicted direction match actual?
            validated = [e for e in edges if e["actual_move"] is not None]
            if validated:
                correct = sum(1 for e in validated
                             if (e["edge"] > 0 and e["actual_move"] > 0) or
                                (e["edge"] < 0 and e["actual_move"] < 0))
                print(f"  Direction accuracy: {correct}/{len(validated)} ({correct/len(validated)*100:.0f}%)")
            for e in edges_sorted[:10]:
                actual = f" → actual={e['actual_move']:+.4f}" if e["actual_move"] is not None else ""
                print(f"  t={e['elapsed_s']:>5.1f}s  BTC={e['btc_price']:>10.2f} "
                      f"ret2s={e['ret_2s']:+.4f}% ret5s={e['ret_5s']:+.4f}%  "
                      f"UP={e['up_now']:.4f} fair={e['fair_up']:.4f}  edge={e['edge']:+.4f}{actual}")
        else:
            print("  No significant edge opportunities found")
    else:
        print("  Skipped (no model)")

    # Poly update rate
    print(f"\n{'='*70}")
    print("POLY UPDATE RATE (deduped, per second)")
    print(f"{'='*70}")
    if poly_dedup:
        sec_counts = defaultdict(int)
        for p in poly_dedup:
            sec_counts[int(p.ts)] += 1
        rates = list(sec_counts.values())
        print(f"  Mean: {sum(rates)/len(rates):.1f}/s, Max: {max(rates)}/s, Min: {min(rates)}/s")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        files = [sys.argv[1]]
    else:
        files = sorted([os.path.join(DATA_DIR, f) for f in os.listdir(DATA_DIR)
                       if f.startswith("collect_") and f.endswith(".jsonl")])

    if not files:
        print("No data files found.")
        sys.exit(1)

    for f in files:
        analyze_file(f)
