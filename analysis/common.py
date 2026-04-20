"""Shared analysis helpers for latency-arb research scripts."""

from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass


@dataclass
class BinanceTick:
    ts: float
    price: float
    qty: float
    side: str  # "buy" or "sell"
    exchange_ts: float = 0


@dataclass
class PolyUpdate:
    ts: float
    token: str
    mid: float
    bid: float
    ask: float


@dataclass
class Snapshot:
    ts: float
    trigger: str  # "btc_move", "poly_move", "heartbeat"
    time_to_expiry: float
    btc_price: float
    btc_ts: float
    exchange_ts: float
    btc_age: float
    btc_volatility: float
    btc_flow: dict  # {"100ms": {"buy":.., "sell":.., "imbalance":..}, ...}
    up_mid: float
    up_bid: float
    up_ask: float
    up_ts: float
    up_age: float
    up_spread: float
    up_mid_change: float
    down_mid: float
    down_bid: float
    down_ask: float
    down_ts: float
    down_age: float
    down_spread: float
    down_mid_change: float


def load_data(filepath: str):
    """Parse JSONL into binance/poly/snap/outcome lists."""
    binance = []
    poly = []
    snapshots = []
    outcome = None

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            src = rec["src"]
            if src == "binance":
                binance.append(BinanceTick(
                    rec["ts"], rec["price"], rec["qty"],
                    rec.get("side", "unknown"), rec.get("exchange_ts", 0),
                ))
            elif src == "poly":
                poly.append(PolyUpdate(
                    rec["ts"], rec["token"], rec["mid"], rec["bid"], rec["ask"],
                ))
            elif src == "snap":
                btc = rec.get("btc", {})
                up = rec.get("up", {})
                down = rec.get("down", {})
                if isinstance(btc, dict) and "flow" in btc:
                    snapshots.append(Snapshot(
                        ts=rec["ts"],
                        trigger=rec.get("trigger", "unknown"),
                        time_to_expiry=rec.get("time_to_expiry", 0),
                        btc_price=btc.get("price", 0),
                        btc_ts=btc.get("ts", 0),
                        exchange_ts=btc.get("exchange_ts", 0),
                        btc_age=btc.get("age", 0),
                        btc_volatility=btc.get("volatility_2s", 0),
                        btc_flow=btc.get("flow", {}),
                        up_mid=up.get("mid", 0),
                        up_bid=up.get("bid", 0),
                        up_ask=up.get("ask", 0),
                        up_ts=up.get("ts", 0),
                        up_age=up.get("age", 0),
                        up_spread=up.get("spread", 0),
                        up_mid_change=up.get("mid_change", 0),
                        down_mid=down.get("mid", 0),
                        down_bid=down.get("bid", 0),
                        down_ask=down.get("ask", 0),
                        down_ts=down.get("ts", 0),
                        down_age=down.get("age", 0),
                        down_spread=down.get("spread", 0),
                        down_mid_change=down.get("mid_change", 0),
                    ))
                elif isinstance(btc, dict):
                    snapshots.append(Snapshot(
                        ts=rec["ts"], trigger="poll", time_to_expiry=0,
                        btc_price=btc.get("price", 0),
                        btc_ts=btc.get("ts", 0),
                        exchange_ts=btc.get("exchange_ts", 0),
                        btc_age=0, btc_volatility=0, btc_flow={},
                        up_mid=up.get("mid", 0), up_bid=up.get("bid", 0),
                        up_ask=up.get("ask", 0), up_ts=up.get("ts", 0),
                        up_age=0, up_spread=0, up_mid_change=0,
                        down_mid=down.get("mid", 0), down_bid=down.get("bid", 0),
                        down_ask=down.get("ask", 0), down_ts=down.get("ts", 0),
                        down_age=0, down_spread=0, down_mid_change=0,
                    ))
                else:
                    snapshots.append(Snapshot(
                        ts=rec["ts"], trigger="poll", time_to_expiry=0,
                        btc_price=rec.get("btc", 0), btc_ts=0,
                        exchange_ts=rec.get("exchange_ts", 0),
                        btc_age=0, btc_volatility=0, btc_flow={},
                        up_mid=rec.get("up_mid", 0), up_bid=rec.get("up_bid", 0),
                        up_ask=rec.get("up_ask", 0), up_ts=0,
                        up_age=0, up_spread=0, up_mid_change=0,
                        down_mid=rec.get("down_mid", 0), down_bid=rec.get("down_bid", 0),
                        down_ask=rec.get("down_ask", 0), down_ts=0,
                        down_age=0, down_spread=0, down_mid_change=0,
                    ))
            elif src == "outcome":
                outcome = rec

    return binance, poly, snapshots, outcome


def dedup_poly(poly: list[PolyUpdate]) -> list[PolyUpdate]:
    """Keep only poly updates where mid price actually changed for each token."""
    last = {}
    deduped = []
    for p in poly:
        if p.token not in last or last[p.token] != p.mid:
            deduped.append(p)
            last[p.token] = p.mid
    return deduped


class SeriesLookup:
    """O(log n) lookup for sorted (ts, value) series via bisect."""

    def __init__(self, pairs: list[tuple[float, float]]):
        self._ts = [p[0] for p in pairs]
        self._vals = [p[1] for p in pairs]

    def at(self, ts: float, max_lookback: float = 2.0) -> float | None:
        idx = bisect_right(self._ts, ts) - 1
        if idx < 0:
            return None
        if ts - self._ts[idx] > max_lookback:
            return None
        return self._vals[idx]

    def after(self, ts: float, max_forward: float = 2.0) -> float | None:
        idx = bisect_left(self._ts, ts)
        if idx >= len(self._ts):
            return None
        if self._ts[idx] - ts > max_forward:
            return None
        return self._vals[idx]


def compute_velocity(binance: list[BinanceTick], window_sec: float = 1.0):
    """Compute BTC velocity ($/s) and acceleration ($/s²) per tick."""
    velocities = []
    accelerations = []

    for i, tick in enumerate(binance):
        j = i
        while j > 0 and tick.ts - binance[j].ts < window_sec:
            j -= 1
        dt = tick.ts - binance[j].ts
        vel = (tick.price - binance[j].price) / dt if dt > 0 else 0.0
        velocities.append(vel)

    for i in range(len(velocities)):
        if i > 0:
            dt = binance[i].ts - binance[i - 1].ts
            acc = (velocities[i] - velocities[i - 1]) / dt if dt > 0 else 0.0
        else:
            acc = 0.0
        accelerations.append(acc)

    return velocities, accelerations


def fit_reaction_model(binance: list[BinanceTick], poly_dedup: list[PolyUpdate], lag: float = 0.5) -> dict:
    """Fit linear regression: BTC features -> UP price change."""
    up_lookup = SeriesLookup(
        sorted([(p.ts, p.mid) for p in poly_dedup if p.token == "up"], key=lambda x: x[0])
    )
    if not up_lookup._ts or len(binance) < 20:
        return {"error": "insufficient data"}

    btc_ts = [b.ts for b in binance]
    btc_prices = [b.price for b in binance]
    velocities, _ = compute_velocity(binance)

    X = []
    y = []
    start_ts = max(btc_ts[0] + 5.0, up_lookup._ts[0])
    end_ts = min(btc_ts[-1] - 2.0, up_lookup._ts[-1] - lag)

    sample_ts = start_ts
    while sample_ts < end_ts:
        idx_now = bisect_right(btc_ts, sample_ts) - 1
        idx_2s = bisect_right(btc_ts, sample_ts - 2.0) - 1
        idx_5s = bisect_right(btc_ts, sample_ts - 5.0) - 1
        if idx_now < 0 or idx_2s < 0 or idx_5s < 0:
            sample_ts += 0.5
            continue

        btc_now = btc_prices[idx_now]
        btc_2s = btc_prices[idx_2s]
        btc_5s = btc_prices[idx_5s]
        ret_2s = (btc_now - btc_2s) / btc_2s * 100
        ret_5s = (btc_now - btc_5s) / btc_5s * 100
        if abs(ret_2s) < 0.005 and abs(ret_5s) < 0.005:
            sample_ts += 0.5
            continue

        vel = velocities[idx_now]
        up_now = up_lookup.at(sample_ts)
        up_future = up_lookup.at(sample_ts + lag)
        if up_now is not None and up_future is not None:
            X.append([ret_2s, ret_5s, vel, abs(vel)])
            y.append(up_future - up_now)
        sample_ts += 0.5

    if len(X) < 10:
        return {"error": f"insufficient samples ({len(X)})"}

    n_features = len(X[0])
    n = len(X)
    xtx = [[0.0] * n_features for _ in range(n_features)]
    xty = [0.0] * n_features
    for i in range(n):
        for j in range(n_features):
            for k in range(n_features):
                xtx[j][k] += X[i][j] * X[i][k]
            xty[j] += X[i][j] * y[i]

    for j in range(n_features):
        xtx[j][j] += 0.01

    beta = _solve_linear(xtx, xty, n_features)
    y_mean = sum(y) / n
    ss_tot = sum((yi - y_mean) ** 2 for yi in y)
    ss_res = sum((y[i] - sum(beta[j] * X[i][j] for j in range(n_features))) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    return {
        "beta": {name: round(b, 6) for name, b in zip(
            ["ret_2s", "ret_5s", "velocity", "abs_vel"], beta)},
        "r2": round(r2, 4),
        "samples": n,
        "lag": lag,
        "y_mean": round(y_mean, 4),
        "y_std": round((sum((yi - y_mean) ** 2 for yi in y) / n) ** 0.5, 4),
    }


def _solve_linear(a: list[list[float]], b: list[float], n: int) -> list[float]:
    """Gaussian elimination for Ax = b."""
    matrix = [a[i][:] + [b[i]] for i in range(n)]
    for col in range(n):
        max_row = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        matrix[col], matrix[max_row] = matrix[max_row], matrix[col]
        pivot = matrix[col][col]
        if abs(pivot) < 1e-12:
            continue
        for row in range(col + 1, n):
            factor = matrix[row][col] / pivot
            for j in range(col, n + 1):
                matrix[row][j] -= factor * matrix[col][j]

    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        if abs(matrix[i][i]) < 1e-12:
            continue
        x[i] = (matrix[i][n] - sum(matrix[i][j] * x[j] for j in range(i + 1, n))) / matrix[i][i]
    return x
