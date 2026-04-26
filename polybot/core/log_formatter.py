"""Structured logging — dual output: human-readable console + JSON Lines file."""

import json
import logging
from datetime import datetime, timezone

# ─── Event type constants ────────────────────────────────────────────────────

SYSTEM = "SYSTEM"
WINDOW = "WINDOW"
MARKET = "MARKET"
SIGNAL = "SIGNAL"
TRADE = "TRADE"
WS = "WS"


# ─── JSON Formatter ──────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON for .jsonl files."""

    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
        }
        # If the record was created via log_event(), use structured fields
        if hasattr(record, "event_type"):
            obj["event"] = record.event_type
        if hasattr(record, "event_data"):
            obj["data"] = record.event_data

        # Fallback: plain text message
        if "event" not in obj:
            obj["event"] = "LOG"
            obj["data"] = {"message": record.getMessage()}

        return json.dumps(obj, ensure_ascii=False, default=str)


# ─── Console Formatter ───────────────────────────────────────────────────────

class ConsoleFormatter(logging.Formatter):
    """Human-readable format with [EVENT_TYPE] prefix."""

    def format(self, record: logging.LogRecord) -> str:
        # If structured, build readable message from event_data
        if hasattr(record, "event_type") and hasattr(record, "event_data"):
            data = record.event_data
            action = data.get("action", "")
            parts = [f"[{record.event_type}]", action]

            # Add key fields in a consistent order
            for key in (
                "side", "window", "result", "price", "avg_price", "amount",
                "shares", "filled_size", "entries", "entry_latency_ms",
                "post_order_ms", "total_ms", "best_ask_age_ms",
                "max_entry_price", "windows_remaining", "threshold", "source",
                "count", "reason", "message", "slug",
            ):
                if key in data:
                    val = data[key]
                    if isinstance(val, float):
                        parts.append(f"{key}={val:.4f}")
                    else:
                        parts.append(f"{key}={val}")

            record.msg = " ".join(parts)
            record.args = ()

        return super().format(record)


# ─── log_event helper ────────────────────────────────────────────────────────

def log_event(
    logger: logging.Logger,
    level: int,
    event_type: str,
    data: dict,
) -> None:
    """
    Log a structured event.

    Creates a LogRecord with extra fields that JsonFormatter and
    ConsoleFormatter both understand.  This is the single entry point
    for all structured logging in the bot.
    """
    record = logger.makeRecord(
        name=logger.name,
        level=level,
        fn="",
        lno=0,
        msg="",  # filled by formatters
        args=(),
        exc_info=None,
    )
    record.event_type = event_type
    record.event_data = data
    logger.handle(record)
