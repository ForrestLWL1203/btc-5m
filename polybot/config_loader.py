"""Load trading configuration from YAML file or CLI arguments."""

from pathlib import Path
from typing import Optional

from polybot.market.series import ACTIVE_SERIES_KEY, MarketSeries
from polybot.strategies.crowd_m1 import CrowdM1Strategy
from polybot.strategies.paired_window import PairedWindowStrategy
from .trade_config import TradeConfig

try:
    import yaml
except ImportError:
    yaml = None


def load_config(config_path: Optional[str] = None) -> dict:
    """Load YAML config, or return empty dict if no path given."""
    if config_path is None:
        return {}
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    if yaml is None:
        raise ImportError("pyyaml is required for --config. Install with: pip install pyyaml")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def build_series(cfg: dict) -> MarketSeries:
    """Build the active BTC 5-minute MarketSeries from config dict."""
    market = cfg.get("market", {})
    asset = market.get("asset", "btc")
    timeframe = market.get("timeframe", "5m")
    key = f"{asset}-updown-{timeframe}"
    if key != ACTIVE_SERIES_KEY:
        raise ValueError(f"Only {ACTIVE_SERIES_KEY} is supported")
    return MarketSeries.from_known(ACTIVE_SERIES_KEY)


def build_strategy(cfg: dict, series: Optional[MarketSeries] = None):
    """Build Strategy from config dict."""
    strat_cfg = cfg.get("strategy", {})
    strat_type = strat_cfg.get("type")
    if strat_type == "paired_window":
        if series is None:
            raise ValueError("PairedWindowStrategy requires a market series")

        max_entry_price = strat_cfg.get("max_entry_price", 0.70)

        return PairedWindowStrategy(
            series=series,
            theta_pct=strat_cfg.get("theta_pct", 0.02),
            theta_start_pct=(
                float(strat_cfg["theta_start_pct"])
                if strat_cfg.get("theta_start_pct") is not None
                else None
            ),
            theta_end_pct=(
                float(strat_cfg["theta_end_pct"])
                if strat_cfg.get("theta_end_pct") is not None
                else None
            ),
            entry_start_remaining_sec=strat_cfg.get("entry_start_remaining_sec", 255.0),
            entry_end_remaining_sec=strat_cfg.get("entry_end_remaining_sec", 120.0),
            persistence_sec=strat_cfg.get("persistence_sec", 10.0),
            max_entry_price=max_entry_price,
            min_move_ratio=strat_cfg.get("min_move_ratio", 0.7),
            open_price_max_wait_sec=strat_cfg.get("open_price_max_wait_sec", 30.0),
        )

    if strat_type == "crowd_m1":
        if series is None:
            raise ValueError("CrowdM1Strategy requires a market series")
        btc_reverse_filter = strat_cfg.get("btc_reverse_filter") or {}
        return CrowdM1Strategy(
            series=series,
            entry_elapsed_sec=strat_cfg.get("entry_elapsed_sec", 120.0),
            entry_timeout_sec=strat_cfg.get("entry_timeout_sec", 60.0),
            min_leading_ask=strat_cfg.get("min_leading_ask", 0.0),
            min_ask_gap=strat_cfg.get("min_ask_gap", 0.16),
            max_entry_price=strat_cfg.get("max_entry_price", 0.75),
            btc_direction_confirm=bool(strat_cfg.get("btc_direction_confirm", True)),
            btc_price_feed_source=strat_cfg.get("btc_price_feed_source", "binance"),
            btc_reverse_filter_enabled=bool(btc_reverse_filter.get("enabled", False)),
            btc_reverse_lookback_sec=float(btc_reverse_filter.get("lookback_sec", 20.0)),
            btc_reverse_min_move_pct=float(btc_reverse_filter.get("min_reverse_move_pct", 0.02)),
            open_price_max_wait_sec=strat_cfg.get("open_price_max_wait_sec", 30.0),
        )

    if strat_type:
        raise ValueError(f"Unknown strategy type: {strat_type}. Available: paired_window, crowd_m1")
    raise ValueError("Strategy type is required. Available strategies: paired_window, crowd_m1")


def build_trade_config(cfg: dict) -> TradeConfig:
    """Build runtime execution config for the active strategy."""
    params = cfg.get("params", {})
    risk = cfg.get("risk", {})

    rounds_val = cfg.get("rounds")
    if rounds_val is not None and int(rounds_val) <= 0:
        rounds_val = None

    return TradeConfig(
        amount=params.get("amount", 5.0),
        entry_ask_level=max(1, int(params.get("entry_ask_level", 1))),
        low_price_threshold=(
            float(params["low_price_threshold"])
            if params.get("low_price_threshold") is not None
            else None
        ),
        low_price_entry_ask_level=(
            max(1, int(params["low_price_entry_ask_level"]))
            if params.get("low_price_entry_ask_level") is not None
            else None
        ),
        dynamic_entry_levels=_build_dynamic_entry_levels(params.get("dynamic_entry_levels")),
        max_slippage_from_best_ask=(
            float(params["max_slippage_from_best_ask"])
            if params.get("max_slippage_from_best_ask") is not None
            else None
        ),
        max_entries_per_window=params.get("max_entries_per_window"),
        rounds=int(rounds_val) if rounds_val is not None else None,
        amount_tiers=_build_amount_tiers(params.get("amount_tiers")),
        **_build_stop_loss(params.get("stop_loss")),
        consecutive_loss_amount_limit=risk.get("consecutive_loss_amount"),
        daily_loss_amount_limit=risk.get("daily_loss_amount"),
        consecutive_loss_pause_windows=int(risk.get("consecutive_loss_pause_windows", 2)),
        daily_loss_pause_windows=int(risk.get("daily_loss_pause_windows", 5)),
    )

def _build_amount_tiers(raw: Optional[list[dict]]) -> list[tuple[float, float]]:
    """Build sorted signal-strength amount tiers from YAML."""
    tiers: list[tuple[float, float]] = []
    if not raw:
        return tiers
    for item in raw:
        if not isinstance(item, dict):
            continue
        threshold = item.get("threshold")
        amount = item.get("amount")
        if threshold is None or amount is None:
            continue
        tiers.append((float(threshold), float(amount)))
    tiers.sort(key=lambda pair: pair[0])
    return tiers


def _build_dynamic_entry_levels(raw: Optional[list[dict]]) -> list[tuple[float, int]]:
    """Build sorted best-ask threshold to ask-book level rules."""
    levels: list[tuple[float, int]] = []
    if not raw:
        return levels
    for item in raw:
        if not isinstance(item, dict):
            continue
        threshold = item.get("leading_ask_max")
        level = item.get("entry_ask_level")
        if threshold is None or level is None:
            continue
        levels.append((float(threshold), max(1, int(level))))
    levels.sort(key=lambda pair: pair[0])
    return levels


def _build_stop_loss(raw: Optional[dict]) -> dict:
    """Build optional stop-loss config."""
    if not raw:
        return {}
    return {
        "stop_loss_enabled": bool(raw.get("enabled", False)),
        "stop_loss_trigger_price": float(raw.get("trigger_price", 0.38)),
        "stop_loss_disable_below_entry_price": float(raw.get("disable_below_entry_price", 0.45)),
        "stop_loss_start_remaining_sec": float(raw.get("start_remaining_sec", 120.0)),
        "stop_loss_end_remaining_sec": float(raw.get("end_remaining_sec", 15.0)),
        "stop_loss_sell_bid_level": max(1, int(raw.get("sell_bid_level", 10))),
        "stop_loss_retry_count": max(1, int(raw.get("retry_count", 3))),
        "stop_loss_min_sell_price": float(raw.get("min_sell_price", 0.20)),
    }
