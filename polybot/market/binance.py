"""Minimal Binance trade feed for runtime strategies."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import Optional

import websockets

log = logging.getLogger(__name__)

BINANCE_WS_TEMPLATE = "wss://stream.binance.com:9443/ws/{}@trade"


class BinancePriceFeed:
    """Keep a rolling stream of Binance trade prices for one symbol."""

    def __init__(self, symbol: str, max_history_sec: float = 900.0):
        self._symbol = symbol.lower()
        self._ws_url = BINANCE_WS_TEMPLATE.format(self._symbol)
        self._max_history_sec = max_history_sec
        self._history: deque[tuple[float, float]] = deque()
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    @property
    def latest_price(self) -> Optional[float]:
        return self._history[-1][1] if self._history else None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def stop(self) -> None:
        self._running = False
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    def price_at_or_before(self, ts: float) -> Optional[float]:
        if not self._history:
            return None
        ts_values = [t for t, _ in self._history]
        idx = bisect_right(ts_values, ts) - 1
        if idx < 0:
            return None
        return self._history[idx][1]

    def first_price_at_or_after(self, ts: float, max_forward_sec: float = 30.0) -> Optional[float]:
        if not self._history:
            return None
        ts_values = [t for t, _ in self._history]
        idx = bisect_left(ts_values, ts)
        if idx >= len(self._history):
            return None
        first_ts, first_price = self._history[idx]
        if first_ts - ts > max_forward_sec:
            return None
        return first_price

    async def _recv_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    self._ws = await websockets.connect(self._ws_url)
                    log.info("BinancePriceFeed connected: %s", self._ws_url)

                async for msg in self._ws:
                    data = json.loads(msg)
                    now = time.time()
                    price = float(data["p"])
                    self._history.append((now, price))
                    self._prune(now)
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                log.warning("BinancePriceFeed WS closed, reconnecting...")
                self._ws = None
            except Exception as e:
                log.warning("BinancePriceFeed error: %s", e)
                self._ws = None
            if self._running:
                await asyncio.sleep(1.0)

    def _prune(self, now: float) -> None:
        cutoff = now - self._max_history_sec
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
