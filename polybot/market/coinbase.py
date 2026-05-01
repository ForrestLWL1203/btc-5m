"""Coinbase BTC trade/ticker feed for runtime strategies."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from bisect import bisect_left, bisect_right
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx
import websockets

log = logging.getLogger(__name__)

COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
COINBASE_CANDLES_URL_TEMPLATE = "https://api.exchange.coinbase.com/products/{}/candles"


class CoinbasePriceFeed:
    """Keep a rolling stream of Coinbase ticker prices for one product."""

    def __init__(self, product_id: str = "BTC-USD", max_history_sec: float = 900.0):
        self._product_id = _normalize_product_id(product_id)
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

    async def fetch_open_at(self, epoch: float) -> Optional[float]:
        """Fetch BTC open price around epoch via Coinbase public candles API."""
        start = datetime.fromtimestamp(epoch, tz=timezone.utc)
        end = datetime.fromtimestamp(epoch + 60, tz=timezone.utc)
        params = {
            "granularity": 60,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        }
        url = COINBASE_CANDLES_URL_TEMPLATE.format(self._product_id)
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list):
                    return None
                best_row = None
                best_delta = float("inf")
                for row in data:
                    if not isinstance(row, list) or len(row) < 4:
                        continue
                    candle_ts = float(row[0])
                    delta = abs(candle_ts - epoch)
                    if delta <= 60 and delta < best_delta:
                        best_row = row
                        best_delta = delta
                if best_row is not None:
                    open_price = float(best_row[3])
                    self._inject(epoch, open_price)
                    log.debug(
                        "CoinbasePriceFeed REST fallback: epoch=%.0f open=%.2f",
                        epoch,
                        open_price,
                    )
                    return open_price
        except Exception as e:
            log.warning("CoinbasePriceFeed REST candles failed: %s", e)
        return None

    async def _recv_loop(self) -> None:
        while self._running:
            try:
                if self._ws is None:
                    self._ws = await websockets.connect(COINBASE_WS_URL)
                    await self._subscribe()
                    log.debug("CoinbasePriceFeed connected: %s", self._product_id)

                async for msg in self._ws:
                    self._handle_message(msg)
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                log.warning("CoinbasePriceFeed WS closed, reconnecting...")
                self._ws = None
            except Exception as e:
                log.warning("CoinbasePriceFeed error: %s", e)
                self._ws = None
            if self._running:
                await asyncio.sleep(1.0)

    async def _subscribe(self) -> None:
        if self._ws is None:
            return
        await self._ws.send(json.dumps({
            "type": "subscribe",
            "product_ids": [self._product_id],
            "channel": "ticker",
        }))

    def _handle_message(self, raw: str) -> None:
        if not raw:
            return
        data = json.loads(raw)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._handle_event(item)
            return
        if isinstance(data, dict):
            self._handle_event(data)

    def _handle_event(self, data: dict) -> None:
        parent_ts = data.get("time") or data.get("timestamp")
        if isinstance(data.get("events"), list):
            for event in data["events"]:
                if isinstance(event, dict):
                    if parent_ts is not None:
                        event = {**event}
                        event.setdefault("timestamp", parent_ts)
                    self._handle_event(event)
            return
        if isinstance(data.get("tickers"), list):
            for ticker in data["tickers"]:
                if isinstance(ticker, dict):
                    if parent_ts is not None:
                        ticker = {**ticker}
                        ticker.setdefault("timestamp", parent_ts)
                    self._record_ticker(ticker)
            return
        self._record_ticker(data)

    def _record_ticker(self, payload: dict) -> None:
        product_id = payload.get("product_id") or payload.get("product")
        if product_id is not None and _normalize_product_id(str(product_id)) != self._product_id:
            return
        value = payload.get("price")
        if value is None:
            return
        try:
            price = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(price):
            return

        ts = _parse_timestamp(payload.get("time") or payload.get("timestamp"))
        if ts is None:
            ts = time.time()
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


def _normalize_product_id(value: str) -> str:
    normalized = value.strip().upper().replace("_", "-")
    if normalized in {"BTCUSDT", "BTCUSD"}:
        return "BTC-USD"
    return normalized


def _parse_timestamp(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts if math.isfinite(ts) else None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).timestamp()
    except (TypeError, ValueError):
        return None
