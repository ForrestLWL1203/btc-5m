"""Collect paired Binance BTC tick + Polymarket price data via WebSocket.

Features:
  - Event-driven snapshots (triggered by BTC price/flow changes, not fixed interval)
  - Multi-scale order flow aggregation (100ms, 500ms, 2s)
  - Orderbook structure: spread and mid_change
  - Time-to-expiry snapshots inside each Polymarket window
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
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BINANCE_WS_TEMPLATE = "wss://stream.binance.com:9443/ws/{}@trade"
BINANCE_KLINES_TEMPLATE = "https://api.binance.com/api/v3/klines"
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
# BTC tick throttle: minimum price move (USD) to write a tick record
BTC_TICK_MIN_MOVE = 1.0  # $1 default; 0 = write every trade
POLY_MIN_INTERVAL_MS = 0  # 0 = write every qualifying update


class BinanceTradeStream:
    """Minimal Binance trade WebSocket client."""

    def __init__(
        self,
        symbol: str,
        on_trade: Callable[[float, float, float, float, bool], Awaitable[None]],
    ):
        self._ws_url = BINANCE_WS_TEMPLATE.format(symbol)
        self._on_trade = on_trade
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(self._ws_url)
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
                    self._ws = await websockets.connect(self._ws_url)
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
    btc_ticks: int = 0
    poly_updates: int = 0
    actual_direction: Optional[str] = None


class DataCollector:
    """Coordinates Binance BTC plus Polymarket and writes JSONL."""

    def __init__(self, series_key: str, max_windows: int, slim: bool = False,
                 no_snap: bool = False, btc_min_move: float = BTC_TICK_MIN_MOVE,
                 poly_min_interval_ms: int = POLY_MIN_INTERVAL_MS):
        from polybot.market.series import MarketSeries
        self.series = MarketSeries.from_known(series_key)
        self.max_windows = max_windows
        self._slim = slim
        self._no_snap = no_snap
        self._btc_min_move = btc_min_move
        self._poly_min_interval = max(0.0, poly_min_interval_ms / 1000.0)
        self._last_written_btc_price: Optional[float] = None

        self._buffer: list[str] = []
        self._outfile = None
        self._summary = WindowSummary(window_label="")

        self._btc_state = {
            "price": None,
            "ts": 0.0,
            "exchange_ts": 0.0,
            "prev_price": None,
            "history": deque(maxlen=5000),
            "flow": deque(maxlen=10000),
        }

        # Poly state: direction → {mid, bid, ask, ts, prev_mid, update_count}
        self._poly_state: dict[str, dict] = {}
        self._last_written_poly_quote: dict[str, tuple[float, float, float]] = {}
        self._last_written_poly_ts: dict[str, float] = {}
        self._pending_poly_line: dict[str, str] = {}
        self._pending_poly_ts: dict[str, float] = {}

        # Snapshot state
        self._last_snap_ts: float = 0
        self._snap_trigger: str = ""  # what triggered last snap
        self._window_end: float = 0  # for time_to_expiry

        self._btc_feed: Optional[BinanceTradeStream] = None
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

        await self._connect_btc_feeds()

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
            self._last_written_poly_quote.clear()
            self._last_written_poly_ts.clear()
            self._pending_poly_line.clear()
            self._pending_poly_ts.clear()
            self._reset_btc_state()

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
                f"ticks={self._summary.btc_ticks} poly={self._summary.poly_updates}"
            )
            self._print_summary_line()

            await asyncio.sleep(2)

        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._flush_task:
            self._flush_task.cancel()
        if self._btc_feed is not None:
            await self._btc_feed.close()
        await self._poly.close()
        self._flush()
        self._outfile.close()
        log.info(f"\nData saved to {filename}")

    async def _connect_btc_feeds(self) -> None:
        self._btc_feed = BinanceTradeStream(
            symbol="btcusdt",
            on_trade=lambda local_ts, exchange_ts, price, qty, is_seller: (
                self._on_btc_trade(local_ts, exchange_ts, price, qty, is_seller)
            ),
        )
        await self._btc_feed.connect()

    def _reset_btc_state(self) -> None:
        self._btc_state["price"] = None
        self._btc_state["ts"] = 0.0
        self._btc_state["exchange_ts"] = 0.0
        self._btc_state["prev_price"] = None
        self._btc_state["history"].clear()
        self._btc_state["flow"].clear()
        self._last_written_btc_price = None

    def _maybe_write_poly(
        self, direction: str, ts: float, mid: float, bid: float, ask: float
    ) -> None:
        quote = (mid, bid, ask)
        last_quote = self._last_written_poly_quote.get(direction)
        last_ts = self._last_written_poly_ts.get(direction, 0.0)
        line = json.dumps({
            "ts": round(ts, 3), "src": "poly", "token": direction,
            "mid": mid, "bid": bid, "ask": ask,
        })

        if not self._slim:
            self._buffer.append(line)
            self._last_written_poly_quote[direction] = quote
            self._last_written_poly_ts[direction] = ts
            return

        # Lossless dedup for replay-critical quote data: only emit when the
        # observed best quote actually changes.
        if last_quote == quote:
            return

        # Optional micro-batching: keep only the latest quote inside a very
        # small interval and flush it once the interval elapses. This cuts
        # message bursts while preserving the final best quote in each bucket.
        if self._poly_min_interval > 0 and ts - last_ts < self._poly_min_interval:
            self._pending_poly_line[direction] = line
            self._pending_poly_ts[direction] = ts
            return

        pending_line = self._pending_poly_line.pop(direction, None)
        pending_ts = self._pending_poly_ts.pop(direction, None)
        if pending_line is not None:
            if pending_line != line:
                self._buffer.append(pending_line)
            self._last_written_poly_ts[direction] = pending_ts or ts
        self._buffer.append(line)
        self._last_written_poly_quote[direction] = quote
        self._last_written_poly_ts[direction] = ts

    async def _on_btc_trade(
        self,
        local_ts: float,
        exchange_ts: float,
        price: float,
        qty: float,
        is_seller: bool,
    ) -> None:
        state = self._btc_state
        prev_price = state["price"]
        state["price"] = price
        state["ts"] = local_ts
        state["exchange_ts"] = exchange_ts
        state["history"].append((local_ts, price))

        s = self._summary
        if s.btc_start is None:
            s.btc_start = price
        s.btc_end = price
        s.btc_min = min(s.btc_min, price)
        s.btc_max = max(s.btc_max, price)
        s.btc_ticks += 1

        side = "sell" if is_seller else "buy"
        state["flow"].append((local_ts, qty, side))

        # Throttle: only write tick when price moves enough from last written price
        write_tick = (
            self._btc_min_move <= 0
            or self._last_written_btc_price is None
            or abs(price - self._last_written_btc_price) >= self._btc_min_move
        )
        if write_tick:
            line = json.dumps({
                "ts": round(local_ts, 3), "exchange_ts": round(exchange_ts, 3),
                "src": "binance", "price": price, "qty": qty, "side": side,
            })
            self._buffer.append(line)
            self._last_written_btc_price = price

        if not self._no_snap and prev_price is not None and abs(price - prev_price) >= BTC_SNAP_THRESHOLD:
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

        self._maybe_write_poly(direction, ts, mid, bid, ask)

        # Event-driven: trigger snapshot on significant poly price change
        if not self._no_snap and prev_mid is not None and abs(mid - prev_mid) >= 0.01:
            self._emit_snapshot(ts, trigger="poly_move")

        if len(self._buffer) >= 500:
            self._flush()

    def _emit_snapshot(self, ts: float, trigger: str) -> None:
        """Build and buffer a snapshot if conditions are met."""
        # Rate limit: minimum interval between snapshots
        if ts - self._last_snap_ts < MIN_SNAP_INTERVAL:
            return

        btc = self._btc_state
        btc_price = btc["price"]
        btc_ts = btc["ts"]
        btc_exchange_ts = btc["exchange_ts"]
        btc_history = btc["history"]
        btc_flow_queue = btc["flow"]

        if btc_price is None or ts - btc_ts > MAX_STALENESS:
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
            for ft, fq, fs in btc_flow_queue:
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
        vol_prices = [p for t, p in btc_history if t >= cutoff_vol]
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
        btc_age = round(ts - btc_ts, 4)
        up_age = round(ts - up["ts"], 4)
        down_age = round(ts - down["ts"], 4)

        snap = {
            "ts": round(ts, 3),
            "src": "snap",
            "trigger": trigger,
            "time_to_expiry": round(time_to_expiry, 2),
            "btc": {
                "price": btc_price,
                "ts": round(btc_ts, 3),
                "exchange_ts": round(btc_exchange_ts, 3),
                "age": btc_age,
                "volatility_2s": round(btc_vol, 8),
                "flow": flow_data,
                "source": "binance",
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
            if not self._no_snap and self._btc_state["price"] is not None:
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

            def fetch():
                o, c = None, None
                try:
                    r = req.get(
                        BINANCE_KLINES_TEMPLATE,
                        params={
                            "symbol": "BTCUSDT",
                            "interval": "1m",
                            "startTime": int(window.start_epoch) * 1000,
                            "endTime": (int(window.start_epoch) + 60) * 1000,
                            "limit": 1,
                        },
                        timeout=10,
                    )
                    d = r.json()
                    o = float(d[0][1]) if d else None
                except Exception:
                    pass
                try:
                    r = req.get(
                        BINANCE_KLINES_TEMPLATE,
                        params={
                            "symbol": symbol,
                            "interval": "1m",
                            "startTime": (int(window.end_epoch) - 60) * 1000,
                            "endTime": int(window.end_epoch) * 1000,
                            "limit": 1,
                        },
                        timeout=10,
                    )
                    d = r.json()
                    c = float(d[0][4]) if d else None
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
            "source": "binance_ticks" if self._summary.btc_start else "binance_api",
        }
        self._outfile.write(json.dumps(outcome) + "\n")
        self._outfile.flush()

    def _flush(self) -> None:
        if self._pending_poly_line:
            for direction in ("up", "down"):
                pending_line = self._pending_poly_line.pop(direction, None)
                pending_ts = self._pending_poly_ts.pop(direction, None)
                if pending_line is None:
                    continue
                state = self._poly_state.get(direction)
                if state:
                    self._last_written_poly_quote[direction] = (
                        state["mid"],
                        state["bid"],
                        state["ask"],
                    )
                if pending_ts is not None:
                    self._last_written_poly_ts[direction] = pending_ts
                self._buffer.append(pending_line)
        if self._buffer and self._outfile:
            self._outfile.write("\n".join(self._buffer) + "\n")
            self._outfile.flush()
            self._buffer.clear()

    def _print_summary_line(self) -> None:
        s = self._summary
        btc_chg = (
            ((s.btc_end - s.btc_start) / s.btc_start * 100)
            if s.btc_start and s.btc_end is not None
            else None
        )
        up_chg = (
            (s.up_end - s.up_start)
            if s.up_start is not None and s.up_end is not None
            else None
        )
        print(f"  {s.window_label}  BTC={self._fmt_pct(btc_chg)}  "
              f"UP={self._fmt_price(s.up_start)}->{self._fmt_price(s.up_end)} ({self._fmt_delta(up_chg)})  "
              f"DOWN={self._fmt_price(s.down_start)}->{self._fmt_price(s.down_end)}  "
              f"actual={s.actual_direction}  "
              f"ticks={s.btc_ticks} poly={s.poly_updates}")

    @staticmethod
    def _fmt_price(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:.3f}"

    @staticmethod
    def _fmt_delta(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:+.3f}"

    @staticmethod
    def _fmt_pct(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:+.3f}%"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--market", default="btc-updown-5m", choices=("btc-updown-5m",))
    p.add_argument("--windows", type=int, default=5)
    p.add_argument("--slim", action="store_true",
                   help="Deduplicate poly writes (only when best quote changes)")
    p.add_argument("--no-snap", action="store_true", help="Skip all snap records (heartbeat + event-driven)")
    p.add_argument("--btc-min-move", type=float, default=BTC_TICK_MIN_MOVE,
                   help=f"Min BTC price move (USD) to write a tick (default {BTC_TICK_MIN_MOVE}, 0=all)")
    p.add_argument("--poly-min-interval-ms", type=int, default=POLY_MIN_INTERVAL_MS,
                   help="When --slim is enabled, coalesce poly quote writes within this interval; 0 disables time coalescing")
    args = p.parse_args()
    collector = DataCollector(
        args.market, args.windows,
        slim=args.slim,
        no_snap=args.no_snap,
        btc_min_move=args.btc_min_move,
        poly_min_interval_ms=args.poly_min_interval_ms,
    )
    asyncio.run(collector.run())
