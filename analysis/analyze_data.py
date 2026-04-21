"""Analyze collected JSONL data for paired BTC + Polymarket behavior.

Reads data/collect_*.jsonl. Supports both raw tick data and event-driven
snapshots. Summarizes BTC movement, token movement, snapshot timing,
cross-correlation, volatility, and expiry behavior for the current strategy
research workflow.

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
        detect_reaction_lag,
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
        detect_reaction_lag,
        dedup_poly,
        fit_reaction_model,
        load_data,
    )


DATA_DIR = "data"


def analyze_snapshots(snapshots: list[Snapshot]):
    """Analyze snapshot data for reaction timing, order flow, and expiry effects."""
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
    print(f"\nRaw: {len(binance)} btc_ticks, {len(poly)} poly, {len(snapshots)} snaps, outcome={bool(outcome)}")
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
    detected_lag, corr_results = detect_reaction_lag(binance, poly_dedup)
    if "error" not in corr_results:
        all_corrs = []
        for key, val in sorted(corr_results.items()):
            offset = key.replace("offset_", "").replace("s", "")
            all_corrs.append((offset, val["correlation"], val))
        for offset, corr, val in all_corrs:
            marker = " <-- PEAK" if float(offset) == detected_lag else ""
            print(f"  offset={offset:>5}s: corr={corr:+.4f}  n={val['samples']}  btc_std={val['btc_std']:.2f}  up_std={val['up_std']:.4f}{marker}")
        best_key = f"offset_{detected_lag}s"
        best_corr = corr_results.get(best_key, {}).get("correlation")
        print(f"\n  → Best correlation at offset={detected_lag}s (corr={best_corr:+.4f})")
    else:
        print(f"  {corr_results['error']}")

    # Snapshot analysis (if available)
    analyze_snapshots(snapshots)

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
