"""
WebSocket real-time price stream for the Polymarket CLOB.

Subscribes to a set of token IDs and emits price updates via async callback.
"""

import asyncio
import json
import logging
import time
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
    source: str  # 'best_bid_ask' | 'price_change' | 'last_trade_price' | 'book'
    received_at: float = 0.0  # local monotonic timestamp when this update was processed
    best_ask_received_at: float = 0.0  # local monotonic timestamp when best_ask was last updated

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
        # Order book cache: token_id -> {"bids": [(price, size)], "asks": [(price, size)], "received_at": mono}
        self._books: dict[str, dict[str, object]] = {}

        # Background tasks
        self._ping_task: Optional[asyncio.Task] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._connection_lock = asyncio.Lock()

    def get_latest_price(self, token_id: str) -> Optional[float]:
        """Get the latest cached midpoint for a token (sync read)."""
        return self._prices.get(token_id, PriceUpdate("", None, None, None, None, "")).midpoint

    def get_latest_best_ask(
        self,
        token_id: str,
        max_age_sec: Optional[float] = None,
        level: int = 1,
    ) -> Optional[float]:
        """Get the latest cached best ask for a token (sync read)."""
        if level > 1:
            asks = self.get_latest_ask_levels(token_id, max_age_sec=max_age_sec)
            if len(asks) < level:
                return None
            return asks[level - 1]

        asks = self.get_latest_ask_levels(token_id, max_age_sec=max_age_sec)
        if asks:
            return asks[0]
        update = self._prices.get(token_id)
        if update is None:
            return None
        ask_received_at = update.best_ask_received_at or update.received_at
        if max_age_sec is not None and ask_received_at > 0:
            if time.monotonic() - ask_received_at > max_age_sec:
                return None
        return update.best_ask

    def get_latest_best_ask_age(self, token_id: str, level: int = 1) -> Optional[float]:
        """Return age in seconds for the cached best ask, if known."""
        if level >= 1:
            book = self._books.get(token_id)
            if book is not None:
                book_received_at = float(book.get("received_at", 0.0) or 0.0)
                if book_received_at > 0:
                    return time.monotonic() - book_received_at
        update = self._prices.get(token_id)
        if update is None:
            return None
        ask_received_at = update.best_ask_received_at or update.received_at
        if ask_received_at <= 0:
            return None
        return time.monotonic() - ask_received_at

    def get_latest_ask_levels(
        self,
        token_id: str,
        max_age_sec: Optional[float] = None,
    ) -> list[float]:
        """Return cached ask prices sorted best-to-worse, if fresh enough."""
        return [price for price, _size in self.get_latest_ask_levels_with_size(token_id, max_age_sec)]

    def get_latest_ask_levels_with_size(
        self,
        token_id: str,
        max_age_sec: Optional[float] = None,
    ) -> list[tuple[float, float]]:
        """Return cached ask (price, size) levels sorted best-to-worse, if fresh enough."""
        book = self._books.get(token_id)
        if book is None:
            return []
        book_received_at = float(book.get("received_at", 0.0) or 0.0)
        if max_age_sec is not None and book_received_at > 0:
            if time.monotonic() - book_received_at > max_age_sec:
                return []
        asks = book.get("asks", [])
        return [(float(price), float(size)) for price, size in asks]

    def get_ask_price_for_notional(
        self,
        token_id: str,
        amount: float,
        max_age_sec: Optional[float] = None,
    ) -> Optional[tuple[float, int, float]]:
        """Return the ask price level needed to spend at least ``amount`` USDC."""
        if amount <= 0:
            return None
        cumulative = 0.0
        for idx, (price, size) in enumerate(
            self.get_latest_ask_levels_with_size(token_id, max_age_sec=max_age_sec),
            start=1,
        ):
            cumulative += price * size
            if cumulative >= amount:
                return price, idx, cumulative
        return None

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
        self._books.clear()

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
        log.debug("WS subscribed to %d token(s)", len(token_ids))

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
        self._books.clear()
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
                    log.debug("WS ping failed: %s", e)
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
        elif event_type == "book":
            self._handle_book(ev)
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

        received_at = time.monotonic()
        update = PriceUpdate(
            token_id=asset_id,
            best_bid=bid,
            best_ask=ask,
            midpoint=midpoint,
            spread=spread,
            source="best_bid_ask",
            received_at=received_at,
            best_ask_received_at=received_at if ask is not None else 0.0,
        )
        self._prices[asset_id] = update
        self._schedule_callback(update)
        log.debug(
            "best_bid_ask %s: bid=%.3f ask=%.3f mid=%.3f",
            asset_id[:20], bid, ask, midpoint,
        )

    def _handle_book(self, ev: dict) -> None:
        """Handle book snapshot/update: cache full L2 depth and schedule callback."""
        asset_id = ev.get("asset_id", "")
        if not asset_id:
            return

        bids = self._parse_book_side(ev.get("bids", []), reverse=True)
        asks = self._parse_book_side(ev.get("asks", []), reverse=False)
        received_at = time.monotonic()
        self._books[asset_id] = {
            "bids": bids,
            "asks": asks,
            "received_at": received_at,
        }

        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        spread = (best_ask - best_bid) if best_ask is not None and best_bid is not None else None
        midpoint = (best_bid + best_ask) / 2 if best_ask is not None and best_bid is not None else None
        update = PriceUpdate(
            token_id=asset_id,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint=midpoint,
            spread=spread,
            source="book",
            received_at=received_at,
            best_ask_received_at=received_at if best_ask is not None else 0.0,
        )
        self._prices[asset_id] = update
        self._schedule_callback(update)

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
            ask_received_at = 0.0
            if existing:
                if bid is None:
                    bid = existing.best_bid
                if ask is None:
                    ask = existing.best_ask
                    ask_received_at = existing.best_ask_received_at
            if ask is not None and ask_str:
                ask_received_at = time.monotonic()

            midpoint = (bid + ask) / 2 if bid is not None and ask is not None else price
            spread = abs(ask - bid) if ask is not None and bid is not None else None

            received_at = time.monotonic()
            update = PriceUpdate(
                token_id=asset_id,
                best_bid=bid,
                best_ask=ask,
                midpoint=midpoint,
                spread=spread,
                source="price_change",
                received_at=received_at,
                best_ask_received_at=ask_received_at,
            )
            self._prices[asset_id] = update
            self._apply_price_change_to_book(asset_id, side, price, change.get("size"))
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
        received_at = time.monotonic()
        update = PriceUpdate(
            token_id=asset_id,
            best_bid=existing.best_bid if existing else None,
            best_ask=existing.best_ask if existing else None,
            midpoint=price,
            spread=existing.spread if existing else None,
            source="last_trade_price",
            received_at=received_at,
            best_ask_received_at=existing.best_ask_received_at if existing else 0.0,
        )
        self._prices[asset_id] = update
        self._schedule_callback(update)

    @staticmethod
    def _parse_book_side(levels: list[dict], reverse: bool) -> list[tuple[float, float]]:
        parsed: list[tuple[float, float]] = []
        for level in levels:
            try:
                price = float(level.get("price"))
                size = float(level.get("size", 0))
            except (TypeError, ValueError, AttributeError):
                continue
            if size <= 0:
                continue
            parsed.append((price, size))
        parsed.sort(key=lambda pair: pair[0], reverse=reverse)
        return parsed

    def _apply_price_change_to_book(
        self,
        asset_id: str,
        side: str,
        price: float,
        size_raw,
    ) -> None:
        book = self._books.get(asset_id)
        if book is None:
            return
        try:
            size = float(size_raw)
        except (TypeError, ValueError):
            return
        side_key = "bids" if side == "BUY" else "asks" if side == "SELL" else None
        if side_key is None:
            return
        levels = list(book.get(side_key, []))
        updated = False
        kept: list[tuple[float, float]] = []
        for level_price, level_size in levels:
            if level_price == price:
                updated = True
                if size > 0:
                    kept.append((price, size))
            else:
                kept.append((level_price, level_size))
        if not updated and size > 0:
            kept.append((price, size))
        kept.sort(key=lambda pair: pair[0], reverse=(side_key == "bids"))
        book[side_key] = kept
        book["received_at"] = time.monotonic()

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
