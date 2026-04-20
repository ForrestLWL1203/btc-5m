"""Binance trade WebSocket feed with rolling history and feature extraction."""

import asyncio
import json
import logging
import time
from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from typing import Optional

import websockets

log = logging.getLogger(__name__)

BINANCE_WS_TEMPLATE = "wss://stream.binance.com:9443/ws/{}@trade"
_HISTORY_RETENTION_SEC = 10.0
_FLOW_RETENTION_SEC = 3.0
_COMPACT_THRESHOLD = 1024


@dataclass
class BtcFeatures:
    ret_2s: float       # BTC % return over 2s
    ret_5s: float       # BTC % return over 5s
    velocity: float     # $/s over 1s window
    abs_vel: float      # |velocity|
    btc_price: float
    data_age_ms: float  # ms since last tick
    flow_imbalance: float  # buy-sell volume imbalance over 500ms


class BinanceTradeFeed:
    """Real-time Binance trade WS with rolling price history for feature computation."""

    def __init__(self, symbol: str = "btcusdt"):
        self._ws_url = BINANCE_WS_TEMPLATE.format(symbol)
        self._history_ts: list[float] = []
        self._history_prices: list[float] = []
        self._history_start: int = 0
        # Flow: (ts, qty, is_buy) — keep only the recent few seconds.
        self._flow: deque[tuple[float, float, bool]] = deque()
        self._latest_price: Optional[float] = None
        self._latest_ts: float = 0.0
        self._running: bool = False
        self._ws = None
        self._recv_task: Optional[asyncio.Task] = None

    @property
    def latest_price(self) -> Optional[float]:
        return self._latest_price

    @property
    def latest_ts(self) -> float:
        return self._latest_ts

    async def start(self) -> None:
        self._running = True
        self._ws = await websockets.connect(self._ws_url)
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("BinanceTradeFeed connected: %s", self._ws_url)

    async def stop(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    def compute_features(self) -> Optional[BtcFeatures]:
        """Compute ret_2s, ret_5s, velocity from rolling history."""
        if self._latest_price is None or self._history_start >= len(self._history_ts):
            return None

        now = time.time()
        age_ms = (now - self._latest_ts) * 1000

        ts_list = self._history_ts
        prices = self._history_prices
        start = self._history_start

        idx_now = len(ts_list) - 1
        idx_2s = bisect_right(ts_list, now - 2.0, lo=start) - 1
        idx_5s = bisect_right(ts_list, now - 5.0, lo=start) - 1

        if idx_now < start or idx_2s < start or idx_5s < start:
            return None

        btc_now = prices[idx_now]
        btc_2s = prices[idx_2s]
        btc_5s = prices[idx_5s]

        ret_2s = (btc_now - btc_2s) / btc_2s * 100
        ret_5s = (btc_now - btc_5s) / btc_5s * 100

        # Velocity: $/s over 1s window
        idx_1s = bisect_right(ts_list, now - 1.0, lo=start) - 1
        if idx_1s >= start:
            dt = now - ts_list[idx_1s]
            velocity = (btc_now - prices[idx_1s]) / dt if dt > 0 else 0.0
        else:
            velocity = 0.0

        # Flow imbalance over 500ms
        flow_cutoff = now - 0.5
        buy_vol, sell_vol = 0.0, 0.0
        for ft, fq, is_buy in reversed(self._flow):
            if ft < flow_cutoff:
                break
            if is_buy:
                buy_vol += fq
            else:
                sell_vol += fq
        total_flow = buy_vol + sell_vol
        flow_imbalance = (buy_vol - sell_vol) / total_flow if total_flow > 0 else 0.0

        return BtcFeatures(
            ret_2s=ret_2s,
            ret_5s=ret_5s,
            velocity=velocity,
            abs_vel=abs(velocity),
            btc_price=btc_now,
            data_age_ms=age_ms,
            flow_imbalance=flow_imbalance,
        )

    async def _recv_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    self._ws = await websockets.connect(self._ws_url)
                    log.info("BinanceTradeFeed reconnected")
                async for msg in self._ws:
                    data = json.loads(msg)
                    price = float(data["p"])
                    qty = float(data["q"])
                    is_buy = not data.get("m", False)
                    local_ts = time.time()
                    self._latest_price = price
                    self._latest_ts = local_ts
                    self._history_ts.append(local_ts)
                    self._history_prices.append(price)
                    self._prune_history(local_ts)
                    self._flow.append((local_ts, qty, is_buy))
                    self._prune_flow(local_ts)
            except websockets.ConnectionClosed:
                log.warning("BinanceTradeFeed WS closed, reconnecting...")
                self._ws = None
            except Exception as e:
                log.warning("BinanceTradeFeed error: %s", e)
                self._ws = None
            if self._running:
                await asyncio.sleep(1)

    def _prune_history(self, now: float) -> None:
        """Drop stale history while keeping enough lookback for feature windows."""
        cutoff = now - _HISTORY_RETENTION_SEC
        new_start = bisect_right(self._history_ts, cutoff, lo=self._history_start)
        self._history_start = min(new_start, len(self._history_ts))

        if self._history_start >= _COMPACT_THRESHOLD:
            self._history_ts = self._history_ts[self._history_start:]
            self._history_prices = self._history_prices[self._history_start:]
            self._history_start = 0

    def _prune_flow(self, now: float) -> None:
        """Keep only a recent slice of trade flow for imbalance features."""
        cutoff = now - _FLOW_RETENTION_SEC
        while self._flow and self._flow[0][0] < cutoff:
            self._flow.popleft()
