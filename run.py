#!/usr/bin/env python3.11
"""
Polybot — Polymarket Up/Down trading bot runner

Requirements:
  - Python 3.11+ (py-clob-client-v2 dependency)
  - polymarket CLI configured at ~/.config/polymarket/config.json
"""

import argparse
import asyncio
import datetime
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from polybot.config_loader import build_series, build_strategy, build_trade_config
from polybot.market.market import find_next_window
from polybot.market.series import MarketSeries
from polybot.core.log_formatter import ConsoleFormatter, JsonFormatter
from polybot.runtime_config import add_runtime_config_args, build_runtime_config
from polybot.trade_config import TradeConfig
from polybot.trading.monitor import monitor_window, MonitorState
LOG_DIR = Path("log")
LOG_DIR.mkdir(exist_ok=True)

root_log = logging.getLogger()
root_log.setLevel(logging.INFO)
for noisy_logger in ("httpx", "httpcore", "websockets", "urllib3"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)


class _BelowLevelFilter(logging.Filter):
    """Allow records below the configured level."""

    def __init__(self, max_level: int):
        super().__init__()
        self._max_level = max_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self._max_level


class _AtOrAboveLevelFilter(logging.Filter):
    """Allow records at or above the configured level."""

    def __init__(self, min_level: int):
        super().__init__()
        self._min_level = min_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self._min_level


def _console_formatter() -> ConsoleFormatter:
    return ConsoleFormatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )


# Console — split normal business output from abnormal output by stream.
stdout_console = logging.StreamHandler(sys.stdout)
stdout_console.setFormatter(_console_formatter())
stdout_console.addFilter(_BelowLevelFilter(logging.WARNING))
root_log.addHandler(stdout_console)

stderr_console = logging.StreamHandler(sys.stderr)
stderr_console.setFormatter(_console_formatter())
stderr_console.addFilter(_AtOrAboveLevelFilter(logging.WARNING))
root_log.addHandler(stderr_console)

log = logging.getLogger(__name__)
_LAST_DRY_RUN = False

# JSONL handlers — initialized lazily once we know the market series
_run_trade_jsonl_handler = None
_run_error_jsonl_handler = None


def _remove_historical_logs(run_dir: Path) -> None:
    """Remove previous runtime logs before creating the current run log."""
    run_dir_resolved = run_dir.resolve()
    if LOG_DIR.exists():
        for path in LOG_DIR.iterdir():
            try:
                path_resolved = path.resolve()
            except FileNotFoundError:
                continue
            if path_resolved == run_dir_resolved:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def _setup_file_logging(slug_prefix: str, run_id: str) -> None:
    """Set up the per-run structured JSONL log."""
    global _run_trade_jsonl_handler, _run_error_jsonl_handler
    if _run_trade_jsonl_handler is not None and _run_error_jsonl_handler is not None:
        return  # Already set up

    run_dir_override = os.environ.get("POLYBOT_RUN_DIR")
    run_dir = Path(run_dir_override) if run_dir_override else LOG_DIR / "runs" / run_id
    _remove_historical_logs(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    _run_trade_jsonl_handler = logging.FileHandler(
        run_dir / f"{slug_prefix}_trade.jsonl",
        encoding="utf-8",
    )
    _run_trade_jsonl_handler.setFormatter(JsonFormatter())
    _run_trade_jsonl_handler.addFilter(_BelowLevelFilter(logging.WARNING))
    root_log.addHandler(_run_trade_jsonl_handler)

    _run_error_jsonl_handler = logging.FileHandler(
        run_dir / f"{slug_prefix}_error.jsonl",
        encoding="utf-8",
    )
    _run_error_jsonl_handler.setFormatter(JsonFormatter())
    _run_error_jsonl_handler.addFilter(_AtOrAboveLevelFilter(logging.WARNING))
    root_log.addHandler(_run_error_jsonl_handler)


def _raise_if_fatal_state(state: MonitorState) -> None:
    if state.fatal_error is not None:
        raise RuntimeError(state.fatal_error)


def _log_strategy_params(strategy, trade_config: TradeConfig, series: MarketSeries) -> None:
    """Log startup parameters for the active strategy."""
    if hasattr(strategy, '_theta_pct'):
        window_sec = series.slug_step
        start_at = window_sec - strategy._entry_start_remaining_sec
        end_at = window_sec - strategy._entry_end_remaining_sec
        log.debug(
            "Params: theta=%.3f%% | entry_band=[%ds,%ds] into window | "
            "max_entry=%.2f | persistence=%ds | max_entries=%s",
            strategy._theta_pct,
            int(start_at),
            int(end_at),
            strategy._max_entry_price,
            strategy._persistence_sec,
            trade_config.max_entries_per_window,
        )
    if hasattr(strategy, '_min_leading_ask'):
        log.info(
            "Params: crowd_m1 entry_band=%ds-%ds | min_ask_gap=%.3f | min_leading_ask=%.3f | max_entry=%.2f | "
            "btc_confirm=%s strong_move=%.3f%% | "
            "max_slippage=%s | stop_loss=%s [remaining %.0f->%.0fs]",
            int(strategy._entry_elapsed_sec),
            int(strategy._entry_end_elapsed_sec),
            strategy._min_ask_gap,
            strategy._min_leading_ask,
            strategy._max_entry_price,
            strategy._btc_direction_confirm,
            strategy._strong_move_pct,
            (
                f"{trade_config.max_slippage_from_best_ask:.3f}"
                if trade_config.max_slippage_from_best_ask is not None
                else None
            ),
            trade_config.stop_loss_enabled,
            trade_config.stop_loss_start_remaining_sec,
            trade_config.stop_loss_end_remaining_sec,
        )


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
    run_id = os.environ.get("POLYBOT_RUN_ID") or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_dir = Path(os.environ.get("POLYBOT_RUN_DIR") or LOG_DIR / "runs" / run_id)

    # Set up file logging with market-specific names
    _setup_file_logging(series.slug_prefix, run_id)

    # Get display side from strategy for logging
    display_side = "DYNAMIC" if getattr(strategy, "dynamic_side", False) else (strategy.get_side() or "UP")

    rounds_desc = trade_config.rounds if trade_config.rounds is not None else "∞"
    mode = "DRY" if dry_run else "LIVE"
    log.info(
        "RUN_START: run_id=%s mode=%s strategy=%s side=%s amount=$%.1f rounds=%s exit=window_end log_dir=%s",
        run_id,
        mode,
        type(strategy).__name__,
        display_side.upper(),
        trade_config.amount,
        rounds_desc,
        log_dir,
    )

    _log_strategy_params(strategy, trade_config, series)

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
            _raise_if_fatal_state(shared_state)
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
                _raise_if_fatal_state(shared_state)
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
                    _raise_if_fatal_state(shared_state)
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
