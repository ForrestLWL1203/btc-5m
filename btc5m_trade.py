#!/usr/bin/env python3.11
"""
BTC 5-minute Up/Down Trader — Python rewrite of btc5m_trade.sh

Strategy:
  - Find the next active btc-updown-5m-{N} window
  - At window start: buy $5 of Up if 45¢ < price < 55¢
  - WebSocket real-time price updates trigger stop-loss / take-profit immediately
  - Use FOK market orders with 10× retry before falling back to GTC limit

Usage:
  python3.11 btc5m_trade.py          # interactive mode — prompts for all settings
  python3.11 btc5m_trade.py --dry   # dry-run with defaults
  python3.11 btc5m_trade.py --side down --amount 10 --dry

Requirements:
  - Python 3.11+ (py-clob-client dependency)
  - polymarket CLI configured at ~/.config/polymarket/config.json
"""

import argparse
import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path

from btc5m import config
from btc5m.market import find_next_window
from btc5m.monitor import monitor_window

LOG_FILE = Path("log/btc5m_trade.log")
LOG_FILE.parent.mkdir(exist_ok=True)

root_log = logging.getLogger()
root_log.setLevel(logging.INFO)

# Console
console = logging.StreamHandler()
console.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
))
root_log.addHandler(console)

# File (rotate at 10 MB, keep 5 backups)
file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s.%(msecs)03d %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
root_log.addHandler(file_handler)

log = logging.getLogger(__name__)


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
    print("\n=== BTC 5-Min Trading Setup ===")
    print()

    side = _prompt_choice("Buy UP or DOWN", ["up", "down"], "up")
    amount = _prompt_amount("USD amount per trade", 5.0)
    print()

    print("Buy range — only place order when price is in this range:")
    buy_low = _prompt_float("  Lower bound (e.g. 0.45 = 45¢)", 0.45)
    buy_high = _prompt_float("  Upper bound (e.g. 0.55 = 55¢)", 0.55)
    if buy_low >= buy_high:
        print("  Lower must be < upper, using defaults")
        buy_low, buy_high = 0.45, 0.55
    print()

    print("Exit triggers — sell position when price crosses these levels:")
    stop_loss = _prompt_float("  Stop-loss (price drops below)", 0.30)
    take_profit = _prompt_float("  Take-profit (price rises above)", 0.80)
    if stop_loss >= take_profit:
        print("  Stop-loss must be < take-profit, using defaults")
        stop_loss, take_profit = 0.30, 0.80
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
        "buy_low": buy_low,
        "buy_high": buy_high,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "max_reentry": int(max_reentry),
        "max_tp_reentry": int(max_tp_reentry),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="BTC 5-min Polymarket Trader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3.11 btc5m_trade.py                    # interactive: prompts for all settings
  python3.11 btc5m_trade.py --dry             # dry-run with interactive settings
  python3.11 btc5m_trade.py --side down --amount 10 --dry  # non-interactive override
        """,
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
        "--buy-low", type=float,
        help="Lower bound of buy range (e.g. 0.45)"
    )
    parser.add_argument(
        "--buy-high", type=float,
        help="Upper bound of buy range (e.g. 0.55)"
    )
    parser.add_argument(
        "--stop-loss", type=float,
        help="Exit when price drops below this (e.g. 0.30)"
    )
    parser.add_argument(
        "--take-profit", type=float,
        help="Exit when price rises above this (e.g. 0.80)"
    )
    parser.add_argument(
        "--max-reentry", type=int,
        help="Max re-entry buys after stop-loss (0=disabled, 1=allow one, etc.)"
    )
    parser.add_argument(
        "--max-tp-reentry", type=int,
        help="Max re-entry buys after take-profit (0=disabled, 1=allow one, etc.)"
    )
    parser.add_argument(
        "--dry", action="store_true",
        help="Dry-run: log actions but do not place orders"
    )
    args = parser.parse_args()

    # Interactive if no trading args provided
    if any(getattr(args, field) is not None
           for field in ["side", "amount", "buy_low", "buy_high", "stop_loss", "take_profit", "max_reentry", "max_tp_reentry"]):
        cfg = {
            "side": args.side or "up",
            "amount": args.amount or 5.0,
            "buy_low": args.buy_low or 0.45,
            "buy_high": args.buy_high or 0.55,
            "stop_loss": args.stop_loss or 0.30,
            "take_profit": args.take_profit or 0.80,
            "max_reentry": args.max_reentry if args.max_reentry is not None else 0,
            "max_tp_reentry": args.max_tp_reentry if args.max_tp_reentry is not None else 0,
        }
    else:
        cfg = _interactive_config()

    # Validate
    if cfg["buy_low"] >= cfg["buy_high"]:
        log.error("--buy-low must be less than --buy-high")
        sys.exit(1)
    if cfg["stop_loss"] >= cfg["take_profit"]:
        log.error("--stop-loss must be less than --take-profit")
        sys.exit(1)

    # Apply to config
    config.BUY_SIDE = cfg["side"]
    config.BUY_AMOUNT = cfg["amount"]
    config.BUY_THRESHOLD_LOW = cfg["buy_low"]
    config.BUY_THRESHOLD_HIGH = cfg["buy_high"]
    config.STOP_LOSS = cfg["stop_loss"]
    config.TAKE_PROFIT = cfg["take_profit"]
    config.MAX_STOP_LOSS_REENTRY = cfg["max_reentry"]
    config.MAX_TP_REENTRY = cfg["max_tp_reentry"]
    dry_run = args.dry

    log.info("=== BTC 5-Min Up/Down Trader Started ===")
    log.info(
        "Side: %s | Buy: $%s if %s¢ < %s < %s¢ | Stop-loss: <%s¢ | Take-profit: >%s¢",
        config.BUY_SIDE.upper(),
        config.BUY_AMOUNT,
        int(config.BUY_THRESHOLD_LOW * 100),
        config.BUY_SIDE.upper(),
        int(config.BUY_THRESHOLD_HIGH * 100),
        int(config.STOP_LOSS * 100),
        int(config.TAKE_PROFIT * 100),
    )
    if dry_run:
        log.info("[DRY-RUN MODE — no orders will be placed]")

    while True:
        window = find_next_window()

        if window is None:
            log.warning("No window found, retrying in 60s...")
            await asyncio.sleep(60)
            continue

        log.info("Next window: %s", window.short_label)
        log.info("  Window: %s → %s", window.start_time, window.end_time)

        # monitor_window returns the next window if it was pre-opened and is ready
        # to monitor immediately (skip path). Otherwise None.
        next_win = await monitor_window(window, dry_run=dry_run)

        if next_win is not None:
            # Pre-opened window is ready — monitor it immediately without extra sleep
            log.info("=== Pre-opened window ready, monitoring immediately ===")
            next_win = await monitor_window(next_win, dry_run=dry_run, preopened=True)

            # The pre-opened window may also return a next window; keep chaining
            # to avoid re-fetching from Gamma API.
            while next_win is not None:
                log.info("=== Chained window ready: %s ===", next_win.short_label)
                next_win = await monitor_window(next_win, dry_run=dry_run, preopened=True)

        log.info("=== Window pair complete, restarting search ===")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Interrupted by user, exiting.")
        sys.exit(0)
