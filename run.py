#!/usr/bin/env python3.11
"""
Polybot — Polymarket Up/Down trading bot (Latency Arbitrage Strategy)

Usage:
  python3.11 run.py --config latency_arb.yaml --dry    # YAML config + dry-run
  python3.11 run.py --market btc-updown-5m --dry       # CLI args

Requirements:
  - Python 3.11+ (py-clob-client dependency)
  - polymarket CLI configured at ~/.config/polymarket/config.json
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config
from polybot.market.market import find_next_window
from polybot.core.log_formatter import ConsoleFormatter, JsonFormatter
from polybot.trading.monitor import monitor_window
from polybot.market.series import MarketSeries, KNOWN_SERIES
from polybot.strategies.base import Strategy
from polybot.trade_config import TradeConfig

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


def _build_trade_config_from_cli(cfg: dict, rounds: int = None) -> TradeConfig:
    """Build TradeConfig from CLI/interactive config."""
    kwargs = {
        "amount": cfg["amount"],
        "max_sl_reentry": cfg.get("max_sl_reentry", cfg.get("max_reentry", 0)),
        "max_tp_reentry": cfg.get("max_tp_reentry", cfg.get("max_tp_reentry", 0)),
        "max_edge_reentry": cfg.get("max_edge_reentry", 0),
        "max_entries_per_window": cfg.get("max_entries_per_window"),
    }
    if "tp_pct" in cfg:
        kwargs["tp_pct"] = cfg["tp_pct"]
    if "sl_pct" in cfg:
        kwargs["sl_pct"] = cfg["sl_pct"]
    if "tp_price" in cfg:
        kwargs["tp_price"] = cfg["tp_price"]
    if "sl_price" in cfg:
        kwargs["sl_price"] = cfg["sl_price"]
    if rounds is not None:
        kwargs["rounds"] = rounds
    return TradeConfig(**kwargs)


async def main() -> None:
    global _LAST_DRY_RUN
    parser = argparse.ArgumentParser(
        description="Polybot — Polymarket Up/Down Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3.11 run.py --config latency_arb.yaml --dry     # YAML config + dry-run
  python3.11 run.py --market btc-updown-5m --dry        # CLI args with defaults
        """,
    )
    parser.add_argument(
        "--config", type=str,
        help="Path to YAML config file (overrides all other args)"
    )
    parser.add_argument(
        "--market", type=str,
        choices=list(KNOWN_SERIES.keys()),
        help="Market series preset (e.g. btc-updown-5m, eth-updown-4h)"
    )
    parser.add_argument(
        "--amount", type=float,
        help="USD amount to spend per trade"
    )
    parser.add_argument(
        "--tp-pct", type=float,
        help="Take-profit +%% from entry price (e.g. 0.50 = +50%%)"
    )
    parser.add_argument(
        "--sl-pct", type=float,
        help="Stop-loss -%% from entry price (e.g. 0.30 = -30%%)"
    )
    parser.add_argument(
        "--tp-price", type=float,
        help="Take-profit at absolute price (e.g. 0.80 = sell at $0.80)"
    )
    parser.add_argument(
        "--sl-price", type=float,
        help="Stop-loss at absolute price (e.g. 0.35 = sell at $0.35)"
    )
    parser.add_argument(
        "--max-reentry", type=int,
        help="Max re-entry buys after stop-loss (0=disabled)"
    )
    parser.add_argument(
        "--max-tp-reentry", type=int,
        help="Max re-entry buys after take-profit (0=disabled)"
    )
    parser.add_argument(
        "--max-edge-reentry", type=int,
        help="Max re-entry buys after edge-based fast exits (0=disabled)"
    )
    parser.add_argument(
        "--max-entries-per-window", type=int,
        help="Hard cap on total entries inside one market window"
    )
    parser.add_argument(
        "--strategy", choices=["latency_arb"],
        help="Trading strategy (default: latency_arb)"
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="Dry-run: log actions but do not place orders"
    )
    parser.add_argument(
        "--rounds", type=int,
        help="Number of complete windows to run (omit for infinite)"
    )
    args = parser.parse_args()

    # ── Build TradeConfig, Strategy, and Series ─────────────────────────────

    if args.config:
        # YAML config mode — all settings from file
        yaml_cfg = load_config(args.config)
        series = build_series(yaml_cfg)
        strategy = build_strategy(yaml_cfg, series)
        trade_config = build_trade_config(yaml_cfg)
    else:
        # CLI/interactive mode — latency_arb only
        series = MarketSeries.from_known(args.market or "btc-updown-5m")
        params = {
            "amount": args.amount or 5.0,
            "max_sl_reentry": args.max_reentry if args.max_reentry is not None else 0,
            "max_tp_reentry": args.max_tp_reentry if args.max_tp_reentry is not None else 0,
            "max_edge_reentry": args.max_edge_reentry if args.max_edge_reentry is not None else 0,
            "max_entries_per_window": args.max_entries_per_window,
        }
        if args.tp_price is not None:
            params["tp_price"] = args.tp_price
        else:
            params["tp_pct"] = args.tp_pct or 0.50
        if args.sl_price is not None:
            params["sl_price"] = args.sl_price
        else:
            params["sl_pct"] = args.sl_pct or 0.30
        cfg = {
            "strategy": {"type": "latency_arb"},
            "params": params,
        }
        strategy = build_strategy(cfg, series)
        trade_config = _build_trade_config_from_cli(cfg["params"], rounds=args.rounds)

    dry_run = args.dry
    _LAST_DRY_RUN = dry_run

    # Set up file logging with market-specific names
    _setup_file_logging(series.slug_prefix)

    # Get display side from strategy for logging
    display_side = strategy.get_side() or "up"

    log.info("=== %s %s Up/Down Trader Started ===", series.asset.upper(), series.timeframe)
    tp_desc = f"${trade_config.tp_price:.2f}" if trade_config.tp_price else f"+{trade_config.tp_pct * 100:.0f}%%"
    sl_desc = f"${trade_config.sl_price:.2f}" if trade_config.sl_price else f"-{trade_config.sl_pct * 100:.0f}%%"
    log.info(
        "Strategy: %s | Side: %s | Amount: $%.1f | TP: %s | SL: %s",
        type(strategy).__name__,
        display_side.upper(),
        trade_config.amount,
        tp_desc,
        sl_desc,
    )
    if trade_config.rounds is not None:
        log.info("Rounds: %d", trade_config.rounds)
    else:
        log.info("Rounds: infinite")
    if dry_run:
        log.info("[DRY-RUN MODE — no orders will be placed]")

    ws = None
    completed = 0

    # Start strategy lifecycle (e.g. Binance WS for LatencyArbStrategy)
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

            next_win, ws, monitored = await monitor_window(
                window, dry_run=dry_run, existing_ws=ws,
                trade_config=trade_config, strategy=strategy, series=series,
            )
            if monitored:
                completed += 1
                log.info("Round %d/%s complete", completed, trade_config.rounds if trade_config.rounds else "∞")
                if trade_config.rounds is not None and completed >= trade_config.rounds:
                    log.info("=== All %d rounds complete, exiting ===", completed)
                    break

            if next_win is not None:
                log.info("=== Pre-opened window ready, monitoring immediately ===")
                next_win, ws, monitored = await monitor_window(
                    next_win, dry_run=dry_run, preopened=True, existing_ws=ws,
                    trade_config=trade_config, strategy=strategy, series=series,
                )
                if monitored:
                    completed += 1
                    log.info("Round %d/%s complete", completed, trade_config.rounds if trade_config.rounds else "∞")
                    if trade_config.rounds is not None and completed >= trade_config.rounds:
                        log.info("=== All %d rounds complete, exiting ===", completed)
                        break

                while next_win is not None:
                    log.info("=== Chained window ready: %s ===", next_win.short_label)
                    next_win, ws, monitored = await monitor_window(
                        next_win, dry_run=dry_run, preopened=True, existing_ws=ws,
                        trade_config=trade_config, strategy=strategy, series=series,
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
