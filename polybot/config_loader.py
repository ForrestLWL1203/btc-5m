"""Load trading configuration from YAML file or CLI arguments."""

from pathlib import Path
from typing import Optional

from polybot.market.series import MarketSeries, KNOWN_SERIES, TIMEFRAME_SECONDS, _default_buffer
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
    """Build MarketSeries from config dict."""
    market = cfg.get("market", {})
    asset = market.get("asset", "btc")
    timeframe = market.get("timeframe", "5m")
    key = f"{asset}-updown-{timeframe}"

    if key in KNOWN_SERIES:
        return MarketSeries.from_known(key)

    # Custom series — user must provide slug details
    slug_prefix = market.get("slug_prefix", key)
    slug_step = market.get("slug_step", TIMEFRAME_SECONDS.get(timeframe, 300))
    window_end_buffer = market.get("window_end_buffer", _default_buffer(slug_step))
    return MarketSeries(
        asset=asset,
        timeframe=timeframe,
        slug_prefix=slug_prefix,
        slug_step=slug_step,
        window_end_buffer=window_end_buffer,
    )

STRATEGY_REGISTRY: dict[str, type] = {
    "paired_window": PairedWindowStrategy,
}


def build_strategy(cfg: dict, series: Optional[MarketSeries] = None):
    """Build Strategy from config dict."""
    strat_cfg = cfg.get("strategy", {})
    strat_type = strat_cfg.get("type")
    if strat_type == "paired_window":
        if series is None:
            raise ValueError("PairedWindowStrategy requires a market series")
        return PairedWindowStrategy(
            series=series,
            theta_pct=strat_cfg.get("theta_pct", 0.02),
            entry_start_remaining_sec=strat_cfg.get("entry_start_remaining_sec", 270.0),
            entry_end_remaining_sec=strat_cfg.get("entry_end_remaining_sec", 120.0),
            persistence_sec=strat_cfg.get("persistence_sec", 10.0),
            min_entry_price=strat_cfg.get("min_entry_price", 0.60),
            max_entry_price=strat_cfg.get("max_entry_price", 0.70),
            min_move_ratio=strat_cfg.get("min_move_ratio", 0.7),
            open_price_max_wait_sec=strat_cfg.get("open_price_max_wait_sec", 30.0),
        )

    available = ", ".join(sorted(STRATEGY_REGISTRY)) or "none"
    if strat_type:
        raise ValueError(
            f"Unknown strategy type: {strat_type}. "
            f"Available: {available}"
        )
    raise ValueError(
        f"Strategy type is required. Available strategies: {available}"
    )


def build_trade_config(cfg: dict) -> TradeConfig:
    """Build runtime execution config for the active strategy."""
    params = cfg.get("params", {})

    rounds_val = cfg.get("rounds")
    if rounds_val is not None and int(rounds_val) <= 0:
        rounds_val = None

    return TradeConfig(
        amount=params.get("amount", 5.0),
        max_entries_per_window=params.get("max_entries_per_window"),
        rounds=int(rounds_val) if rounds_val is not None else None,
    )
