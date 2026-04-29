"""Polymarket RTDS crypto price feed."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from bisect import bisect_left, bisect_right
from collections import deque
from typing import Optional

import websockets

log = logging.getLogger(__name__)

POLYMARKET_RTDS_WS = "wss://ws-live-data.polymarket.com"


class PolymarketRTDSPriceFeed:
    """Keep a rolling BTC price history from Polymarket RTDS crypto_prices."""

    def __init__(self, symbol: str = "btcusdt", max_history_sec: float = 900.0):
        self._symbol = symbol.lower()
        self._max_history_sec = max_history_sec
        self._history: deque[tuple[float, float]] = deque()
        self._running = False
        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
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
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None
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

    async def fetch_open_at(self, epoch: float) -> Optional[float]:
        """Compatibility hook for strategies that may ask for a window open."""
        return self.first_price_at_or_after(epoch)

    async def _recv_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    self._ws = await websockets.connect(POLYMARKET_RTDS_WS)
                    await self._subscribe()
                    self._ping_task = asyncio.create_task(self._ping_loop())
                    log.debug("PolymarketRTDSPriceFeed connected: %s", self._symbol)

                async for msg in self._ws:
                    if msg == "PING":
                        await self._ws.send("PONG")
                        continue
                    self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                log.warning("PolymarketRTDSPriceFeed WS closed, reconnecting...")
                self._cancel_ping_task()
                self._ws = None
            except Exception as e:
                log.warning("PolymarketRTDSPriceFeed error: %s", e)
                self._cancel_ping_task()
                self._ws = None
            if self._running:
                await asyncio.sleep(1.0)

    async def _subscribe(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "crypto_prices",
                    "type": "update",
                }
            ],
        }))

    async def _ping_loop(self) -> None:
        while self._running and self._ws is not None:
            await asyncio.sleep(5.0)
            if self._ws is not None:
                await self._ws.send("PING")

    def _cancel_ping_task(self) -> None:
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None

    def _handle_message(self, raw: str) -> None:
        if raw in ("", "PING", "PONG"):
            return
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                self._handle_event(item)
            return
        if isinstance(data, dict):
            self._handle_event(data)

    def _handle_event(self, data: dict) -> None:
        if data.get("topic") != "crypto_prices":
            return
        payload = data.get("payload") or {}
        if isinstance(payload.get("data"), list):
            for item in payload["data"]:
                item_payload = item
                if isinstance(item_payload, dict) and payload.get("symbol") is not None:
                    item_payload = {**item_payload}
                    item_payload.setdefault("symbol", payload["symbol"])
                self._record_payload(item_payload)
            return
        self._record_payload(payload)

    def _record_payload(self, payload: dict) -> None:
        if str(payload.get("symbol", "")).lower() != self._symbol:
            return
        value = payload.get("value")
        if value is None:
            return
        ts_ms = payload.get("timestamp") or int(time.time() * 1000)
        try:
            ts = float(ts_ms) / 1000.0
            price = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(ts) or not math.isfinite(price):
            return
        self._inject(ts, price)
        self._prune(ts)

    def _inject(self, ts: float, price: float) -> None:
        if not self._history or ts >= self._history[-1][0]:
            self._history.append((ts, price))
            return
        ts_values = [t for t, _ in self._history]
        idx = bisect_left(ts_values, ts)
        self._history.insert(idx, (ts, price))

    def _prune(self, now: float) -> None:
        cutoff = now - self._max_history_sec
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()
