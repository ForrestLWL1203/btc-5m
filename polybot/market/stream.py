"""
WebSocket real-time price stream for the Polymarket CLOB.

Subscribes to a set of token IDs and emits price updates via async callback.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import websockets

from polybot.core import config
from polybot.core.log_formatter import WS, log_event

log = logging.getLogger(__name__)

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10  # seconds


@dataclass
class PriceUpdate:
    """A single price update from the WebSocket."""

    token_id: str
    best_bid: Optional[float]
    best_ask: Optional[float]
    midpoint: Optional[float]
    spread: Optional[float]
    source: str  # 'best_bid_ask' | 'price_change' | 'last_trade_price'

    @property
    def is_trade(self) -> bool:
        """True if this update reflects an actual trade (more timely for SL/TP)."""
        return self.source == "last_trade_price"


class PriceStream:
    """
    Manages a WebSocket connection to the Polymarket CLOB for real-time prices.

    Features:
      - Correct ping format: ``{}`` (empty JSON per official API spec)
      - Handles ``price_changes`` array from ``price_change`` events
      - Automatic reconnection with exponential backoff on disconnect

    Usage:
        async def on_price(update: PriceUpdate):
            print(f"Price update: {update.midpoint}")

        stream = PriceStream(on_price=on_price)
        await stream.connect(["<up_token_id>", "<down_token_id>"])
        # ... stream runs in background ...
        await stream.close()
    """

    def __init__(
        self,
        on_price: Callable[[PriceUpdate], Awaitable[None]],
    ):
        self._on_price = on_price
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._connected_tokens: list[str] = []

        # Price cache: token_id -> PriceUpdate
        self._prices: dict[str, PriceUpdate] = {}

        # Background tasks
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._connection_lock = asyncio.Lock()

    def get_latest_price(self, token_id: str) -> Optional[float]:
        """Get the latest cached midpoint for a token (sync read)."""
        return self._prices.get(token_id, PriceUpdate("", None, None, None, None, "")).midpoint

    def get_latest_best_ask(self, token_id: str) -> Optional[float]:
        """Get the latest cached best ask for a token (sync read)."""
        return self._prices.get(token_id, PriceUpdate("", None, None, None, None, "")).best_ask

    def set_on_price(self, callback: Callable[[PriceUpdate], Awaitable[None]]) -> None:
        """Update the price callback (used when reusing WS for a new window)."""
        self._on_price = callback

    async def connect(self, token_ids: list[str]) -> None:
        """
        Connect to the WebSocket and subscribe to the given token IDs.
        Run this once; call switch_tokens() for window changes.
        """
        self._connected_tokens = list(token_ids)
        self._running = True
        async with self._connection_lock:
            await self._reconnect_locked(log_reconnect=False)
        self._ping_task = asyncio.create_task(self._ping_loop())
        self._recv_task = asyncio.create_task(self._recv_loop())

    async def switch_tokens(self, new_token_ids: list[str]) -> None:
        """
        Unsubscribe from the old tokens and subscribe to new ones.
        Used when the trading window changes.
        """
        if not self._running:
            return

        # Clear stale cached prices from previous window
        self._prices.clear()

        old_token_ids = list(self._connected_tokens)
        # Subscribe to new tokens
        self._connected_tokens = list(new_token_ids)
        async with self._connection_lock:
            if self._ws is None:
                await self._reconnect_locked()
                return

            try:
                # Unsubscribe from old tokens
                if old_token_ids:
                    unsub = {
                        "assets_ids": old_token_ids,
                        "operation": "unsubscribe",
                    }
                    await self._ws.send(json.dumps(unsub))
                    log.debug("Unsubscribed from %s", old_token_ids)

                await self._subscribe(new_token_ids)
            except websockets.ConnectionClosed as e:
                log.warning("WS switch_tokens failed on closed connection: %s", e)
                await self._reconnect_locked()

    async def close(self) -> None:
        """Gracefully close the WebSocket connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            self._ping_task = None
        if self._recv_task:
            self._recv_task.cancel()
            self._recv_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        log.debug("WebSocket connection closed")

    # ─── Internal ────────────────────────────────────────────────────────────

    async def _subscribe(self, token_ids: list[str]) -> None:
        """Send a subscribe message for the given token IDs."""
        msg = {
            "type": "market",
            "assets_ids": token_ids,
            "operation": "subscribe",
            "custom_feature_enabled": True,
        }
        await self._ws.send(json.dumps(msg))
        log_event(log, logging.INFO, WS, {
            "action": "SUBSCRIBED",
            "tokens": [t[:20] + "..." for t in token_ids],
        })

    async def _reconnect_locked(self, log_reconnect: bool = True) -> None:
        """Recreate the WS connection and subscribe to the current token set.

        Caller must hold ``_connection_lock``.
        """
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

        self._ws = await websockets.connect(WS_URL)
        await self._subscribe(self._connected_tokens)
        self._prices.clear()
        if log_reconnect:
            log_event(log, logging.INFO, WS, {"action": "RECONNECTED"})

    async def _ping_loop(self) -> None:
        """Send ``{}`` every PING_INTERVAL seconds to keep the connection alive."""
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            if self._ws and self._running:
                try:
                    await self._ws.send("{}")
                    log.debug("Sent WS ping {}")
                except Exception as e:
                    log.warning("WS ping failed: %s", e)
                    break

    async def _recv_loop(self) -> None:
        """Continuously receive and dispatch WebSocket messages, with reconnection."""
        reconnect_delay = config.WS_RECONNECT_DELAY
        consecutive_failures = 0

        while self._running:
            try:
                if self._ws is None:
                    async with self._connection_lock:
                        if self._ws is None and self._running:
                            await self._reconnect_locked()

                async for msg in self._ws:
                    self._dispatch(msg)
                    consecutive_failures = 0
                    reconnect_delay = config.WS_RECONNECT_DELAY

            except websockets.ConnectionClosed:
                log.debug("WebSocket connection closed")
            except Exception as e:
                log.warning("WebSocket error: %s", e)

            if not self._running:
                break

            consecutive_failures += 1
            if consecutive_failures > config.WS_RECONNECT_MAX_RETRIES:
                log_event(log, logging.ERROR, WS, {
                    "action": "RECONNECT_FAILED",
                    "attempts": consecutive_failures,
                })
                self._running = False
                break

            log.debug(
                "Reconnecting in %.1fs (attempt %d/%d)...",
                reconnect_delay,
                consecutive_failures,
                config.WS_RECONNECT_MAX_RETRIES,
            )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, config.WS_RECONNECT_MAX_DELAY)
            self._ws = None

    def _dispatch(self, raw: str) -> None:
        """Parse a WebSocket message and call the price callback."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        # Messages can be a list or a dict
        if isinstance(data, list):
            events = data
        else:
            events = [data]

        for ev in events:
            self._handle_event(ev)

    def _handle_event(self, ev: dict) -> None:
        """Handle a single WebSocket event — dispatches async callback via schedule."""
        event_type = ev.get("event_type", "")
        asset_id = ev.get("asset_id", "")

        log.debug(
            "WS event | type=%s asset_id=%s price=%s side=%s bid=%s ask=%s",
            event_type,
            asset_id[:20] if asset_id else None,
            ev.get("price"),
            ev.get("side"),
            ev.get("best_bid"),
            ev.get("best_ask"),
        )

        if event_type == "best_bid_ask":
            self._handle_best_bid_ask(ev)
        elif event_type == "price_change":
            self._handle_price_change(ev)
        elif event_type == "last_trade_price":
            self._handle_last_trade(ev)
        elif event_type == "tick_size_change":
            log.debug(
                "Tick size changed for %s: %s",
                asset_id[:20], ev.get("new_tick_size"),
            )

    def _handle_best_bid_ask(self, ev: dict) -> None:
        """Handle best_bid_ask event: update cache and schedule async callback."""
        asset_id = ev.get("asset_id", "")
        bid_str = ev.get("best_bid")
        ask_str = ev.get("best_ask")
        spread_str = ev.get("spread", "")

        try:
            bid = float(bid_str) if bid_str else None
            ask = float(ask_str) if ask_str else None
            spread = float(spread_str) if spread_str else None
            midpoint = (bid + ask) / 2 if bid is not None and ask is not None else None
        except (ValueError, TypeError):
            return

        update = PriceUpdate(
            token_id=asset_id,
            best_bid=bid,
            best_ask=ask,
            midpoint=midpoint,
            spread=spread,
            source="best_bid_ask",
        )
        self._prices[asset_id] = update
        self._schedule_callback(update)
        log.debug(
            "best_bid_ask %s: bid=%.3f ask=%.3f mid=%.3f",
            asset_id[:20], bid, ask, midpoint,
        )

    def _handle_price_change(self, ev: dict) -> None:
        """
        Handle price_change event: iterate over the ``price_changes`` array.

        Official API format:
            {"event_type": "price_change", "price_changes": [{...}, ...]}
        Each item: {"asset_id", "price", "size", "side", "hash", "best_bid", "best_ask"}
        """
        changes = ev.get("price_changes", [])
        if not changes:
            # Fallback: some events may use flat format
            if ev.get("price"):
                changes = [ev]
            else:
                return

        for change in changes:
            asset_id = change.get("asset_id", "")
            price_str = change.get("price")
            if not asset_id or not price_str:
                continue

            try:
                price = float(price_str)
            except (ValueError, TypeError):
                continue

            side = change.get("side", "")

            # Use best_bid / best_ask from the change if available
            bid_str = change.get("best_bid")
            ask_str = change.get("best_ask")
            try:
                bid = float(bid_str) if bid_str else None
                ask = float(ask_str) if ask_str else None
            except (ValueError, TypeError):
                bid = ask = None

            # Merge with existing cached bid/ask
            existing = self._prices.get(asset_id)
            if existing:
                if bid is None:
                    bid = existing.best_bid
                if ask is None:
                    ask = existing.best_ask

            midpoint = (bid + ask) / 2 if bid is not None and ask is not None else price
            spread = abs(ask - bid) if ask is not None and bid is not None else None

            update = PriceUpdate(
                token_id=asset_id,
                best_bid=bid,
                best_ask=ask,
                midpoint=midpoint,
                spread=spread,
                source="price_change",
            )
            self._prices[asset_id] = update
            self._schedule_callback(update)

    def _handle_last_trade(self, ev: dict) -> None:
        """Handle last_trade_price event: use actual trade price as midpoint."""
        asset_id = ev.get("asset_id", "")
        price_str = ev.get("price")
        if not asset_id or not price_str:
            return
        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        existing = self._prices.get(asset_id)
        update = PriceUpdate(
            token_id=asset_id,
            best_bid=existing.best_bid if existing else None,
            best_ask=existing.best_ask if existing else None,
            midpoint=price,
            spread=existing.spread if existing else None,
            source="last_trade_price",
        )
        self._prices[asset_id] = update
        self._schedule_callback(update)

    def _schedule_callback(self, update: PriceUpdate) -> None:
        """Schedule the async callback on the running event loop."""
        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._on_price(update))
            task.add_done_callback(self._on_callback_done)
        except RuntimeError:
            log.warning("No running event loop — price update dropped")

    @staticmethod
    def _on_callback_done(task: asyncio.Task) -> None:
        """Log any exception from a scheduled callback."""
        exc = task.exception()
        if exc is not None:
            log.error("Price callback raised exception: %s", exc)
