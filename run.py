#!/usr/bin/env python3.11
"""
Polybot — Polymarket Up/Down trading bot

Usage:
  python3.11 run.py --config strategy.yaml --dry    # YAML config + dry-run
  python3.11 run.py --side up --amount 5 --dry      # CLI args
  python3.11 run.py                                  # interactive mode

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
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config, build_direction_config
from polybot.market.market import find_next_window
from polybot.core.log_formatter import ConsoleFormatter, JsonFormatter
from polybot.trading.monitor import monitor_window
from polybot.market.series import MarketSeries, KNOWN_SERIES
from polybot.predict.history import WindowHistory
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


def _prompt_choice(prompt_text: str, options: list[str], default: str) -> str:
    """Prompt user to choose from options (case-insensitive match)."""
    options_str = "/".join(o.upper() for o in options)
    while True:
        raw = input(f"{prompt_text} [{options_str}] (default: {default}): ").strip()
        if not raw:
            return default
        if raw.lower() in [o.lower() for o in options]:
            return raw.lower()
        print(f"  Invalid — please choose from {options_str}")


def _prompt_float(prompt_text: str, default: float, min_val: float = 0.0, max_val: float = 1.0) -> float:
    """Prompt user for a float value within [min_val, max_val]."""
    while True:
        raw = input(f"{prompt_text} (default: {default}): ").strip()
        if not raw:
            return default
        try:
            val = float(raw)
            if min_val <= val <= max_val:
                return val
            print(f"  Must be between {min_val} and {max_val}")
        except ValueError:
            print("  Invalid number")


def _prompt_amount(prompt_text: str, default: float) -> float:
    """Prompt user for a USD amount (must be positive)."""
    while True:
        raw = input(f"{prompt_text} (default: ${default}): ").strip()
        if not raw:
            return default
        try:
            val = float(raw)
            if val > 0:
                return val
            print("  Must be a positive number")
        except ValueError:
            print("  Invalid number")


def _interactive_config() -> dict:
    """Collect trading parameters interactively. Returns dict of resolved values."""
    print("\n=== Polymarket Up/Down Trading Setup ===")
    print()

    side = _prompt_choice("Buy UP or DOWN", ["up", "down"], "up")
    amount = _prompt_amount("USD amount per trade", 5.0)
    print()

    print("Exit triggers — percentage from entry price:")
    tp_pct = _prompt_float("  Take-profit +% (e.g. 0.50 = +50%)", 0.50, 0.01, 5.0)
    sl_pct = _prompt_float("  Stop-loss -% (e.g. 0.30 = -30%)", 0.30, 0.01, 0.99)
    print()

    print("Re-entry after exit:")
    max_reentry = _prompt_choice(
        "  Max re-entry after stop-loss? (0=no, 1=allow one, etc.)",
        ["0", "1"], "0",
    )
    max_tp_reentry = _prompt_choice(
        "  Max re-entry after take-profit? (0=no, 1=allow one, etc.)",
        ["0", "1"], "0",
    )
    print()

    return {
        "side": side,
        "amount": amount,
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "max_reentry": int(max_reentry),
        "max_tp_reentry": int(max_tp_reentry),
    }


def _build_trade_config_from_cli(cfg: dict, rounds: int = None) -> TradeConfig:
    """Build TradeConfig from CLI/interactive config."""
    return TradeConfig(
        side=cfg["side"],
        amount=cfg["amount"],
        tp_pct=cfg["tp_pct"],
        sl_pct=cfg["sl_pct"],
        max_sl_reentry=cfg["max_reentry"],
        max_tp_reentry=cfg["max_tp_reentry"],
        rounds=rounds,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Polybot — Polymarket Up/Down Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3.11 run.py --config strategy.yaml --dry     # YAML config + dry-run
  python3.11 run.py --market eth-updown-5m --side up --amount 5 --dry
  python3.11 run.py                                   # interactive mode
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
        "--side", choices=["up", "down"],
        help="Which side to buy (up/down)"
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
        "--max-reentry", type=int,
        help="Max re-entry buys after stop-loss (0=disabled)"
    )
    parser.add_argument(
        "--max-tp-reentry", type=int,
        help="Max re-entry buys after take-profit (0=disabled)"
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
        trade_config = build_trade_config(yaml_cfg)
        strategy = build_strategy(yaml_cfg)
        series = build_series(yaml_cfg)
        dir_cfg = build_direction_config(yaml_cfg, series)
    elif any(getattr(args, field) is not None
             for field in ["side", "amount", "tp_pct", "sl_pct",
                           "max_reentry", "max_tp_reentry"]):
        # CLI args mode
        cfg = {
            "side": args.side or "up",
            "amount": args.amount or 5.0,
            "tp_pct": args.tp_pct or 0.50,
            "sl_pct": args.sl_pct or 0.30,
            "max_reentry": args.max_reentry if args.max_reentry is not None else 0,
            "max_tp_reentry": args.max_tp_reentry if args.max_tp_reentry is not None else 0,
        }
        trade_config = _build_trade_config_from_cli(cfg, rounds=args.rounds)
        strategy = build_strategy({})
        series = MarketSeries.from_known(args.market or "btc-updown-5m")
        dir_cfg = {"predictor": None, "fallback_side": None}
    else:
        # Interactive mode
        cfg = _interactive_config()
        trade_config = _build_trade_config_from_cli(cfg, rounds=args.rounds)
        strategy = build_strategy({})
        series = MarketSeries.from_known(args.market or "btc-updown-5m")
        dir_cfg = {"predictor": None, "fallback_side": None}

    dry_run = args.dry

    # Direction prediction setup
    predictor = dir_cfg.get("predictor")
    fallback_side = dir_cfg.get("fallback_side")
    history = None
    if predictor is not None:
        import time as _time
        history = WindowHistory.for_timeframe(series.timeframe)
        log.info("Backfilling %s history (%d windows)...", series.timeframe, history._buf.maxlen)
        history.backfill(
            slug_prefix=series.slug_prefix,
            slug_step=series.slug_step,
            count=history._buf.maxlen,
            current_epoch=int(_time.time()),
        )
        log.info("History backfilled: %d windows", len(history))
    if fallback_side and predictor is None:
        trade_config.side = fallback_side

    # Set up file logging with market-specific names
    _setup_file_logging(series.slug_prefix)

    log.info("=== %s %s Up/Down Trader Started ===", series.asset.upper(), series.timeframe)
    log.info(
        "Strategy: %s | Side: %s | Amount: $%.1f | TP: +%.0f%% | SL: -%.0f%%",
        type(strategy).__name__,
        trade_config.side.upper(),
        trade_config.amount,
        trade_config.tp_pct * 100,
        trade_config.sl_pct * 100,
    )
    if trade_config.rounds is not None:
        log.info("Rounds: %d", trade_config.rounds)
    else:
        log.info("Rounds: infinite")
    if dry_run:
        log.info("[DRY-RUN MODE — no orders will be placed]")

    ws = None
    completed = 0
    try:
        while True:
            window = find_next_window()

            if window is None:
                log.warning("No window found, retrying in 10s...")
                await asyncio.sleep(10)
                continue

            log.info("Next window: %s", window.short_label)
            log.info("  Window: %s → %s", window.start_time, window.end_time)

            next_win, ws, monitored = await monitor_window(
                window, dry_run=dry_run, existing_ws=ws,
                trade_config=trade_config, strategy=strategy, series=series,
                predictor=predictor, history=history,
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
                    predictor=predictor, history=history,
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
                        predictor=predictor, history=history,
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

            log.info("=== Window pair complete, restarting search ===")
    finally:
        if ws:
            await ws.close()
            log.info("WebSocket closed on exit")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user — attempting cleanup...")
        try:
            from polybot.core.client import get_client
            client = get_client()
            client.cancel_all()
            log.info("Cancelled all open orders on exit")
        except Exception as e:
            log.warning("Cleanup failed: %s — please check for open orders manually", e)
        log.info("Exiting.")
        sys.exit(0)
