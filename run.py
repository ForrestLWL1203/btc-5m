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

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config
from polybot.market.market import find_next_window
from polybot.core.log_formatter import ConsoleFormatter, JsonFormatter
from polybot.trading.monitor import monitor_window, MonitorState
LOG_DIR = Path("log")
LOG_DIR.mkdir(exist_ok=True)

root_log = logging.getLogger()
root_log.setLevel(logging.INFO)

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


def _apply_cli_overrides(cfg: dict, args: argparse.Namespace) -> None:
    """Merge explicit CLI args into the loaded YAML config dict in-place."""
    strat = cfg.setdefault("strategy", {})
    params = cfg.setdefault("params", {})

    if args.rounds is not None:
        cfg["rounds"] = args.rounds
    if args.theta is not None:
        strat["theta_pct"] = args.theta
    if args.persistence is not None:
        strat["persistence_sec"] = args.persistence
    if args.max_entry_price is not None:
        strat["max_entry_price"] = args.max_entry_price
    if args.min_entry_price is not None:
        strat["min_entry_price"] = args.min_entry_price
    if args.entry_start is not None:
        strat["entry_start_remaining_sec"] = args.entry_start
    if args.entry_end is not None:
        strat["entry_end_remaining_sec"] = args.entry_end
    if args.min_move_ratio is not None:
        strat["min_move_ratio"] = args.min_move_ratio
    if args.amount is not None:
        params["amount"] = args.amount
    if args.max_entries is not None:
        params["max_entries_per_window"] = args.max_entries


async def main() -> None:
    global _LAST_DRY_RUN
    parser = argparse.ArgumentParser(
        description="Polybot — Polymarket Up/Down Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3.11 run.py --config strategy.yaml --dry
        """,
    )
    parser.add_argument(
        "--config", type=str,
        help="Path to YAML config file (base config; CLI args override individual fields)"
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="Dry-run: log actions but do not place orders"
    )
    parser.add_argument("--rounds", type=int, help="Number of windows to run (omit for infinite)")
    # Strategy overrides (all optional — override YAML when provided)
    parser.add_argument("--theta", type=float, metavar="PCT", help="BTC move threshold %% (e.g. 0.03)")
    parser.add_argument("--persistence", type=float, metavar="SEC", help="BTC move persistence seconds")
    parser.add_argument("--max-entry-price", type=float, metavar="PRICE", help="Entry price cap")
    parser.add_argument("--min-entry-price", type=float, metavar="PRICE", help="Entry price floor (default: cap×0.88)")
    parser.add_argument("--entry-start", type=float, metavar="SEC", help="Entry band start remaining seconds")
    parser.add_argument("--entry-end", type=float, metavar="SEC", help="Entry band end remaining seconds")
    parser.add_argument("--min-move-ratio", type=float, metavar="RATIO", help="Min ratio of current to past BTC move")
    # Execution overrides
    parser.add_argument("--amount", type=float, metavar="USD", help="Trade size in USD per window")
    parser.add_argument("--max-entries", type=int, metavar="N", help="Max entries per window")
    args = parser.parse_args()

    # ── Build TradeConfig, Strategy, and Series ─────────────────────────────

    if not args.config:
        parser.error(
            "No runtime trading strategies are currently configured. "
            "Use the analysis scripts instead, or add a new strategy before running run.py."
        )

    yaml_cfg = load_config(args.config)
    _apply_cli_overrides(yaml_cfg, args)
    series = build_series(yaml_cfg)
    strategy = build_strategy(yaml_cfg, series)
    trade_config = build_trade_config(yaml_cfg)

    dry_run = args.dry
    _LAST_DRY_RUN = dry_run

    # Set up file logging with market-specific names
    _setup_file_logging(series.slug_prefix)

    # Get display side from strategy for logging
    display_side = "DYNAMIC" if getattr(strategy, "dynamic_side", False) else (strategy.get_side() or "UP")

    log.info("=== %s %s Up/Down Trader Started ===", series.asset.upper(), series.timeframe)
    log.info(
        "Strategy: %s | Side: %s | Amount: $%.1f | Exit: hold-to-window-end",
        type(strategy).__name__,
        display_side.upper(),
        trade_config.amount,
    )
    if trade_config.rounds is not None:
        log.info("Rounds: %d", trade_config.rounds)
    else:
        log.info("Rounds: infinite")
    if dry_run:
        log.info("[DRY-RUN MODE — no orders will be placed]")

    # Print key strategy parameters for verification
    if hasattr(strategy, '_theta_pct'):
        window_sec = series.slug_step
        start_at = window_sec - strategy._entry_start_remaining_sec
        end_at = window_sec - strategy._entry_end_remaining_sec
        msg = (
            "Params: theta=%.3f%% | entry_band=[%ds,%ds] into window | "
            "price=[%.2f,%.2f] | persistence=%ds | max_entries=%s"
        )
        log.info(msg,
            strategy._theta_pct,
            int(start_at), int(end_at),
            strategy._min_entry_price, strategy._max_entry_price,
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

            log.info("Next window: %s", window.short_label)
            log.info("  Window: %s → %s", window.start_time, window.end_time)

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
                log.info("Round %d/%s complete", completed, trade_config.rounds if trade_config.rounds else "∞")
                if trade_config.rounds is not None and completed >= trade_config.rounds:
                    log.info("=== All %d rounds complete, exiting ===", completed)
                    break

            if next_win is not None:
                log.info("=== Pre-opened window ready, monitoring immediately ===")
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
                    log.info("Round %d/%s complete", completed, trade_config.rounds if trade_config.rounds else "∞")
                    if trade_config.rounds is not None and completed >= trade_config.rounds:
                        log.info("=== All %d rounds complete, exiting ===", completed)
                        break

                while next_win is not None:
                    log.info("=== Chained window ready: %s ===", next_win.short_label)
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
                        log.info("Round %d/%s complete", completed, trade_config.rounds if trade_config.rounds else "∞")
                        if trade_config.rounds is not None and completed >= trade_config.rounds:
                            log.info("=== All %d rounds complete, exiting ===", completed)
                            break
                    if trade_config.rounds is not None and completed >= trade_config.rounds:
                        break

            if trade_config.rounds is not None and completed >= trade_config.rounds:
                break

            remaining_to_boundary = window.end_epoch - int(time.time())
            if next_win is None and monitored and remaining_to_boundary > 0:
                await asyncio.sleep(remaining_to_boundary)

            log.info("=== Window pair complete, restarting search ===")
    finally:
        if ws:
            await ws.close()
            log.info("WebSocket closed on exit")
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
