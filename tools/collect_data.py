"""Collect paired BTC tick + Polymarket price data via dual WebSocket.

Records timestamp-aligned Binance trades and Polymarket UP/DOWN prices
to JSONL for building a latency-arbitrage reaction function.

Features:
  - Event-driven snapshots (triggered by BTC price/flow changes, not fixed interval)
  - Multi-scale order flow aggregation (100ms, 500ms, 2s)
  - Orderbook structure: spread, mid_change, update_interval
  - Time-to-expiry for binary option gamma modeling
  - Rolling volatility (BTC 2s std)
  - Data age tracking (btc_age, poly_age) per snapshot
  - Periodic flush (every 1s) to prevent data loss

Usage: python3.11 tools/collect_data.py [--market btc-updown-5m] [--windows 5]
"""

import argparse
import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
DATA_DIR = "data"
MAX_STALENESS = 0.5
# Flow aggregation windows (seconds)
FLOW_WINDOWS = [0.1, 0.5, 2.0]
# BTC price change threshold to trigger event-driven snapshot
BTC_SNAP_THRESHOLD = 0.5  # $0.5 price change
# Minimum time between event-driven snapshots
MIN_SNAP_INTERVAL = 0.05  # 50ms
# Periodic heartbeat snapshot interval
HEARTBEAT_INTERVAL = 0.2  # 200ms fallback


class BinanceTradeStream:
    """Minimal Binance trade WebSocket client."""

    def __init__(self, on_trade: Callable[[float, float, float, float, bool], Awaitable[None]]):
        self._on_trade = on_trade
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(BINANCE_WS)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("Binance WS connected")

    async def close(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _recv_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    self._ws = await websockets.connect(BINANCE_WS)
                    log.info("Binance reconnected")
                async for msg in self._ws:
                    data = json.loads(msg)
                    price = float(data["p"])
                    qty = float(data["q"])
                    local_ts = time.time()
                    exchange_ts = data["E"] / 1000.0
                    is_seller = data.get("m", False)
                    await self._on_trade(local_ts, exchange_ts, price, qty, is_seller)
            except websockets.ConnectionClosed:
                log.warning("Binance WS closed, reconnecting...")
                self._ws = None
            except Exception as e:
                log.warning("Binance WS error: %s", e)
                self._ws = None
            if self._running:
                await asyncio.sleep(1)


@dataclass
class WindowSummary:
    window_label: str
    btc_start: Optional[float] = None
    btc_end: Optional[float] = None
    btc_min: float = 1e18
    btc_max: float = 0
    up_start: Optional[float] = None
    up_end: Optional[float] = None
    down_start: Optional[float] = None
    down_end: Optional[float] = None
    binance_ticks: int = 0
    poly_updates: int = 0
    actual_direction: Optional[str] = None


class DataCollector:
    """Coordinates Binance + Polymarket streams, writes JSONL."""

    def __init__(self, series_key: str, max_windows: int, slim: bool = False):
        from polybot.market.series import MarketSeries
        self.series = MarketSeries.from_known(series_key)
        self.max_windows = max_windows
        self._slim = slim

        self._buffer: list[str] = []
        self._outfile = None
        self._summary = WindowSummary(window_label="")

        # BTC state
        self._btc_price: Optional[float] = None
        self._btc_ts: float = 0
        self._btc_exchange_ts: float = 0
        self._btc_prev_price: Optional[float] = None
        # Rolling BTC prices for volatility: (ts, price)
        self._btc_history: deque[tuple[float, float]] = deque(maxlen=5000)

        # Order flow: (ts, qty, side) — keep 3s worth
        self._flow: deque[tuple[float, float, str]] = deque(maxlen=10000)

        # Poly state: direction → {mid, bid, ask, ts, prev_mid, update_count}
        self._poly_state: dict[str, dict] = {}

        # Snapshot state
        self._last_snap_ts: float = 0
        self._snap_trigger: str = ""  # what triggered last snap
        self._window_end: float = 0  # for time_to_expiry

        self._binance: Optional[BinanceTradeStream] = None
        self._poly: Optional[object] = None
        self._token_map: dict[str, str] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._running = False

    async def run(self) -> None:
        from polybot.market.market import find_next_window, find_window_after
        from polybot.market.stream import PriceStream

        os.makedirs(DATA_DIR, exist_ok=True)
        ts = int(time.time())
        filename = f"{DATA_DIR}/collect_{self.series.slug_prefix}_{ts}.jsonl"
        self._outfile = open(filename, "a")
        log.info("Output: %s", filename)

        self._binance = BinanceTradeStream(self._on_binance_trade)
        await self._binance.connect()

        self._poly = PriceStream(on_price=self._on_poly_price)
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._flush_task = asyncio.create_task(self._periodic_flush())

        prev_end = 0

        for i in range(self.max_windows):
            if i == 0:
                window = find_next_window(self.series)
            else:
                window = find_window_after(prev_end, self.series)

            if not window:
                log.warning("No window found, stopping")
                break

            log.info(f"\n{'='*60}")
            log.info(f"Window {i+1}/{self.max_windows}: {window.short_label}")
            self._summary = WindowSummary(window_label=window.short_label)
            prev_end = window.end_epoch
            self._window_end = window.end_epoch

            self._token_map = {window.up_token: "up", window.down_token: "down"}
            self._poly_state.clear()
            self._flow.clear()
            self._btc_history.clear()
            self._btc_prev_price = None

            if i == 0:
                await self._poly.connect([window.up_token, window.down_token])
            else:
                await self._poly.switch_tokens([window.up_token, window.down_token])

            now = time.time()
            wait = window.start_epoch - now
            if wait > 0:
                log.info(f"  Waiting {wait:.0f}s for window to start...")
                await asyncio.sleep(wait)

            remaining = window.end_epoch - time.time()
            if remaining > 0:
                log.info(f"  Collecting data for {remaining:.0f}s...")
                await asyncio.sleep(remaining)

            self._flush()
            await self._record_outcome(window)

            log.info(
                f"  Window done: BTC {self._summary.btc_start:.1f} → {self._summary.btc_end:.1f} "
                f"({self._summary.actual_direction}), "
                f"ticks={self._summary.binance_ticks} poly={self._summary.poly_updates}"
            )
            self._print_summary_line()

            await asyncio.sleep(2)

        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._flush_task:
            self._flush_task.cancel()
        await self._binance.close()
        await self._poly.close()
        self._flush()
        self._outfile.close()
        log.info(f"\nData saved to {filename}")

    async def _on_binance_trade(self, local_ts: float, exchange_ts: float,
                                price: float, qty: float, is_seller: bool) -> None:
        prev_price = self._btc_price
        self._btc_price = price
        self._btc_ts = local_ts
        self._btc_exchange_ts = exchange_ts
        self._btc_history.append((local_ts, price))

        s = self._summary
        if s.btc_start is None:
            s.btc_start = price
        s.btc_end = price
        s.btc_min = min(s.btc_min, price)
        s.btc_max = max(s.btc_max, price)
        s.binance_ticks += 1

        side = "sell" if is_seller else "buy"
        self._flow.append((local_ts, qty, side))

        line = json.dumps({
            "ts": round(local_ts, 3), "exchange_ts": round(exchange_ts, 3),
            "src": "binance", "price": price, "qty": qty, "side": side,
        })
        self._buffer.append(line)

        # Event-driven: trigger snapshot on BTC price change
        if prev_price is not None and abs(price - prev_price) >= BTC_SNAP_THRESHOLD:
            self._emit_snapshot(local_ts, trigger="btc_move")

        if len(self._buffer) >= 500:
            self._flush()

    async def _on_poly_price(self, update) -> None:
        direction = self._token_map.get(update.token_id)
        if direction is None:
            return

        ts = time.time()
        mid = update.midpoint
        bid = update.best_bid
        ask = update.best_ask

        prev_mid = None
        if direction in self._poly_state:
            prev_mid = self._poly_state[direction].get("mid")

        self._poly_state[direction] = {
            "mid": mid, "bid": bid, "ask": ask, "ts": ts,
            "prev_mid": prev_mid,
            "update_count": self._poly_state.get(direction, {}).get("update_count", 0) + 1,
        }

        s = self._summary
        s.poly_updates += 1
        if direction == "up":
            if s.up_start is None:
                s.up_start = mid
            s.up_end = mid
        else:
            if s.down_start is None:
                s.down_start = mid
            s.down_end = mid

        # In slim mode, only write poly when mid actually changes (dedup)
        if not self._slim or prev_mid is None or mid != prev_mid:
            line = json.dumps({
                "ts": round(ts, 3), "src": "poly", "token": direction,
                "mid": mid, "bid": bid, "ask": ask,
            })
            self._buffer.append(line)

        # Event-driven: trigger snapshot on significant poly price change
        if prev_mid is not None and abs(mid - prev_mid) >= 0.01:
            self._emit_snapshot(ts, trigger="poly_move")

        if len(self._buffer) >= 500:
            self._flush()

    def _emit_snapshot(self, ts: float, trigger: str) -> None:
        """Build and buffer a snapshot if conditions are met."""
        # Rate limit: minimum interval between snapshots
        if ts - self._last_snap_ts < MIN_SNAP_INTERVAL:
            return

        if self._btc_price is None or ts - self._btc_ts > MAX_STALENESS:
            return

        up = self._poly_state.get("up")
        down = self._poly_state.get("down")
        if not up or not down:
            return
        if ts - up["ts"] > MAX_STALENESS or ts - down["ts"] > MAX_STALENESS:
            return

        self._last_snap_ts = ts

        # Multi-scale order flow
        flow_data = {}
        for window in FLOW_WINDOWS:
            cutoff = ts - window
            buy_vol, sell_vol = 0.0, 0.0
            for ft, fq, fs in self._flow:
                if ft >= cutoff:
                    if fs == "buy":
                        buy_vol += fq
                    else:
                        sell_vol += fq
            total = buy_vol + sell_vol
            imbalance = (buy_vol - sell_vol) / total if total > 0 else 0
            key = f"{int(window * 1000)}ms"
            flow_data[key] = {
                "buy": round(buy_vol, 6),
                "sell": round(sell_vol, 6),
                "imbalance": round(imbalance, 4),
            }

        # BTC volatility: rolling std of returns over 2s
        btc_vol = 0.0
        cutoff_vol = ts - 2.0
        vol_prices = [p for t, p in self._btc_history if t >= cutoff_vol]
        if len(vol_prices) >= 3:
            returns = [(vol_prices[i] - vol_prices[i-1]) / vol_prices[i-1]
                       for i in range(1, len(vol_prices))]
            mean_r = sum(returns) / len(returns)
            btc_vol = (sum((r - mean_r) ** 2 for r in returns) / len(returns)) ** 0.5

        # Orderbook features
        up_spread = up["ask"] - up["bid"]
        down_spread = down["ask"] - down["bid"]
        up_mid_change = (up["mid"] - up["prev_mid"]) if up.get("prev_mid") is not None else 0
        down_mid_change = (down["mid"] - down["prev_mid"]) if down.get("prev_mid") is not None else 0

        # Time-to-expiry
        time_to_expiry = max(0, self._window_end - ts)

        # Data age
        btc_age = round(ts - self._btc_ts, 4)
        up_age = round(ts - up["ts"], 4)
        down_age = round(ts - down["ts"], 4)

        snap = {
            "ts": round(ts, 3),
            "src": "snap",
            "trigger": trigger,
            "time_to_expiry": round(time_to_expiry, 2),
            "btc": {
                "price": self._btc_price,
                "ts": round(self._btc_ts, 3),
                "exchange_ts": round(self._btc_exchange_ts, 3),
                "age": btc_age,
                "volatility_2s": round(btc_vol, 8),
                "flow": flow_data,
            },
            "up": {
                "mid": up["mid"], "bid": up["bid"], "ask": up["ask"],
                "ts": round(up["ts"], 3), "age": up_age,
                "spread": round(up_spread, 4),
                "mid_change": round(up_mid_change, 4),
            },
            "down": {
                "mid": down["mid"], "bid": down["bid"], "ask": down["ask"],
                "ts": round(down["ts"], 3), "age": down_age,
                "spread": round(down_spread, 4),
                "mid_change": round(down_mid_change, 4),
            },
        }
        self._buffer.append(json.dumps(snap))
        if len(self._buffer) >= 500:
            self._flush()

    async def _heartbeat_loop(self) -> None:
        """Periodic heartbeat snapshots (fallback for quiet periods)."""
        while self._running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self._btc_price is not None:
                self._emit_snapshot(time.time(), trigger="heartbeat")

    async def _periodic_flush(self) -> None:
        """Force flush every 1s to prevent data loss on crash."""
        while self._running:
            await asyncio.sleep(1.0)
            self._flush()

    async def _record_outcome(self, window) -> None:
        """Record outcome using collected tick data (no API call)."""
        open_price = self._summary.btc_start
        close_price = self._summary.btc_end

        if not open_price or not close_price:
            import requests as req
            symbol = "BTCUSDT" if self.series.asset == "btc" else "ETHUSDT"

            def fetch():
                o, c = None, None
                try:
                    r = req.get("https://api.binance.com/api/v3/klines",
                        params={"symbol": symbol, "interval": "1m",
                                "startTime": int(window.start_epoch * 1000), "limit": 1}, timeout=10)
                    d = r.json()
                    o = float(d[0][1]) if d else None
                except Exception:
                    pass
                try:
                    r = req.get("https://api.binance.com/api/v3/aggTrades",
                        params={"symbol": symbol, "endTime": int(window.end_epoch * 1000),
                                "limit": 1}, timeout=10)
                    d = r.json()
                    c = float(d[-1]["p"]) if d else None
                except Exception:
                    pass
                return o, c

            api_open, api_close = await asyncio.to_thread(fetch)
            open_price = open_price or api_open
            close_price = close_price or api_close

        if open_price and close_price:
            self._summary.actual_direction = "up" if close_price > open_price else "down"

        outcome = {
            "ts": round(time.time(), 3),
            "src": "outcome",
            "window": window.short_label,
            "open": open_price,
            "close": close_price,
            "direction": self._summary.actual_direction,
            "source": "ticks" if self._summary.btc_start else "api",
        }
        self._outfile.write(json.dumps(outcome) + "\n")
        self._outfile.flush()

    def _flush(self) -> None:
        if self._buffer and self._outfile:
            self._outfile.write("\n".join(self._buffer) + "\n")
            self._outfile.flush()
            self._buffer.clear()

    def _print_summary_line(self) -> None:
        s = self._summary
        btc_chg = ((s.btc_end or 0) - (s.btc_start or 0)) / (s.btc_start or 1) * 100
        up_chg = ((s.up_end or 0) - (s.up_start or 0)) if s.up_start else 0
        print(f"  {s.window_label}  BTC={btc_chg:+.3f}%  "
              f"UP={s.up_start:.3f}→{s.up_end:.3f} ({up_chg:+.3f})  "
              f"DOWN={s.down_start:.3f}→{s.down_end:.3f}  "
              f"actual={s.actual_direction}  "
              f"ticks={s.binance_ticks} poly={s.poly_updates}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--market", default="btc-updown-5m")
    p.add_argument("--windows", type=int, default=5)
    p.add_argument("--slim", action="store_true", help="Only write snap+binance, skip raw poly stream")
    args = p.parse_args()
    collector = DataCollector(args.market, args.windows, slim=args.slim)
    asyncio.run(collector.run())
