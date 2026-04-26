#!/usr/bin/env python3.11
"""
Polybot — Polymarket Up/Down trading bot runner

Requirements:
  - Python 3.11+ (py-clob-client dependency)
  - polymarket CLI configured at ~/.config/polymarket/config.json
"""

import argparse
import asyncio
import logging
import logging.handlers
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from polybot.config_loader import build_series, build_strategy, build_trade_config
from polybot.market.market import find_next_window
from polybot.core.log_formatter import ConsoleFormatter, JsonFormatter
from polybot.runtime_config import add_runtime_config_args, build_runtime_config
from polybot.trading.monitor import monitor_window, MonitorState
LOG_DIR = Path("log")
LOG_DIR.mkdir(exist_ok=True)

root_log = logging.getLogger()
root_log.setLevel(logging.INFO)
for noisy_logger in ("httpx", "httpcore", "websockets", "urllib3"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

# Console — human-readable with [EVENT_TYPE] prefix
console = logging.StreamHandler()
console.setFormatter(ConsoleFormatter(
    "%(asctime)s.%(msecs)03d %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
))
root_log.addHandler(console)

log = logging.getLogger(__name__)
_LAST_DRY_RUN = False

# File and JSONL handlers — initialized lazily once we know the market series
_file_handler = None
_jsonl_handler = None


def _setup_file_logging(slug_prefix: str) -> None:
    """Set up file and JSONL logging with market-specific filenames."""
    global _file_handler, _jsonl_handler
    if _file_handler is not None:
        return  # Already set up

    log_file = LOG_DIR / f"{slug_prefix}_trade.log"
    jsonl_file = LOG_DIR / f"{slug_prefix}_trade.jsonl"

    # File — human-readable (rotate at 10 MB, keep 5 backups)
    _file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _file_handler.setFormatter(ConsoleFormatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_log.addHandler(_file_handler)

    # JSONL — structured JSON Lines for frontend consumption
    _jsonl_handler = logging.handlers.RotatingFileHandler(
        jsonl_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _jsonl_handler.setFormatter(JsonFormatter())
    root_log.addHandler(_jsonl_handler)

async def main() -> None:
    global _LAST_DRY_RUN
    parser = argparse.ArgumentParser(
        description="Polybot — Polymarket Up/Down Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3.11 run.py --preset enhanced --dry
  python3.11 run.py --config strategy.yaml --amount 1.5 --rounds 24
        """,
    )
    add_runtime_config_args(parser)
    parser.add_argument(
        "--dry", action="store_true",
        help="Dry-run: log actions but do not place orders"
    )
    args = parser.parse_args()

    # ── Build TradeConfig, Strategy, and Series ─────────────────────────────
    try:
        runtime_cfg = build_runtime_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    series = build_series(runtime_cfg)
    strategy = build_strategy(runtime_cfg, series)
    trade_config = build_trade_config(runtime_cfg)

    dry_run = args.dry
    _LAST_DRY_RUN = dry_run

    # Set up file logging with market-specific names
    _setup_file_logging(series.slug_prefix)

    # Get display side from strategy for logging
    display_side = "DYNAMIC" if getattr(strategy, "dynamic_side", False) else (strategy.get_side() or "UP")

    rounds_desc = trade_config.rounds if trade_config.rounds is not None else "∞"
    mode = "DRY" if dry_run else "LIVE"
    log.info(
        "RUN_START: mode=%s strategy=%s side=%s amount=$%.1f rounds=%s exit=window_end",
        mode,
        type(strategy).__name__,
        display_side.upper(),
        trade_config.amount,
        rounds_desc,
    )

    # Print key strategy parameters for verification
    if hasattr(strategy, '_theta_pct'):
        window_sec = series.slug_step
        start_at = window_sec - strategy._entry_start_remaining_sec
        end_at = window_sec - strategy._entry_end_remaining_sec
        msg = (
            "Params: theta=%.3f%% | entry_band=[%ds,%ds] into window | "
            "max_entry=%.2f | persistence=%ds | max_entries=%s"
        )
        log.debug(msg,
            strategy._theta_pct,
            int(start_at), int(end_at),
            strategy._max_entry_price,
            strategy._persistence_sec,
            trade_config.max_entries_per_window,
        )

    ws = None
    completed = 0
    # Shared MonitorState for risk management tracking across all windows
    shared_state = MonitorState()

    # Start strategy lifecycle if the active strategy defines one.
    if hasattr(strategy, 'start'):
        await strategy.start()

    try:
        while True:
            window = find_next_window(series)

            if window is None:
                log.warning("No window found, retrying in 10s...")
                await asyncio.sleep(10)
                continue

            log.info("NEXT_WINDOW: %s | %s -> %s", window.short_label, window.start_time, window.end_time)

            should_prefetch_next = not (
                trade_config.rounds is not None and completed + 1 >= trade_config.rounds
            )
            next_win, ws, monitored = await monitor_window(
                window, dry_run=dry_run, existing_ws=ws,
                trade_config=trade_config, strategy=strategy, series=series,
                state=shared_state, prefetch_next_window=should_prefetch_next,
            )
            if monitored:
                completed += 1
                log.info("ROUND_COMPLETE: %d/%s", completed, trade_config.rounds if trade_config.rounds else "∞")
                if trade_config.rounds is not None and completed >= trade_config.rounds:
                    log.info("RUN_COMPLETE: completed=%d", completed)
                    break

            if next_win is not None:
                log.debug("Pre-opened window ready, monitoring immediately")
                should_prefetch_next = not (
                    trade_config.rounds is not None and completed + 1 >= trade_config.rounds
                )
                next_win, ws, monitored = await monitor_window(
                    next_win, dry_run=dry_run, preopened=True, existing_ws=ws,
                    trade_config=trade_config, strategy=strategy, series=series,
                    state=shared_state, prefetch_next_window=should_prefetch_next,
                )
                if monitored:
                    completed += 1
                    log.info("ROUND_COMPLETE: %d/%s", completed, trade_config.rounds if trade_config.rounds else "∞")
                    if trade_config.rounds is not None and completed >= trade_config.rounds:
                        log.info("RUN_COMPLETE: completed=%d", completed)
                        break

                while next_win is not None:
                    log.debug("Chained window ready: %s", next_win.short_label)
                    should_prefetch_next = not (
                        trade_config.rounds is not None and completed + 1 >= trade_config.rounds
                    )
                    next_win, ws, monitored = await monitor_window(
                        next_win, dry_run=dry_run, preopened=True, existing_ws=ws,
                        trade_config=trade_config, strategy=strategy, series=series,
                        state=shared_state, prefetch_next_window=should_prefetch_next,
                    )
                    if monitored:
                        completed += 1
                        log.info("ROUND_COMPLETE: %d/%s", completed, trade_config.rounds if trade_config.rounds else "∞")
                        if trade_config.rounds is not None and completed >= trade_config.rounds:
                            log.info("RUN_COMPLETE: completed=%d", completed)
                            break
                    if trade_config.rounds is not None and completed >= trade_config.rounds:
                        break

            if trade_config.rounds is not None and completed >= trade_config.rounds:
                break

            remaining_to_boundary = window.end_epoch - int(time.time())
            if next_win is None and monitored and remaining_to_boundary > 0:
                await asyncio.sleep(remaining_to_boundary)

            log.debug("Window pair complete, restarting search")
    finally:
        if ws:
            await ws.close()
            log.debug("WebSocket closed on exit")
        if hasattr(strategy, 'stop'):
            await strategy.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user — attempting cleanup...")
        if _LAST_DRY_RUN:
            log.info("Dry-run exit: skipping cancel-all cleanup")
        else:
            try:
                from polybot.core.client import get_client
                client = get_client()
                client.cancel_all()
                log.info("Cancelled all open orders on exit")
            except Exception as e:
                log.warning("Cleanup failed: %s — please check for open orders manually", e)
        log.info("Exiting.")
        sys.exit(0)
