"""Load trading configuration from YAML file or CLI arguments."""

from pathlib import Path
from typing import Optional

from polybot.market.series import MarketSeries, KNOWN_SERIES, TIMEFRAME_SECONDS, _default_buffer
from polybot.strategies.immediate import ImmediateStrategy
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
    "immediate": ImmediateStrategy,
}


def build_strategy(cfg: dict) -> ImmediateStrategy:
    """Build Strategy from config dict. Strategy only contains buy decision logic."""
    strat_cfg = cfg.get("strategy", {})
    strat_type = strat_cfg.get("type", "immediate")

    cls = STRATEGY_REGISTRY.get(strat_type)
    if cls is None:
        raise ValueError(
            f"Unknown strategy type: {strat_type}. "
            f"Available: {', '.join(STRATEGY_REGISTRY.keys())}"
        )

    return cls()


def build_trade_config(cfg: dict) -> TradeConfig:
    """Build TradeConfig from config dict. Contains all common trading parameters."""
    params = cfg.get("params", {})

    rounds_val = cfg.get("rounds")
    if rounds_val is not None and int(rounds_val) <= 0:
        rounds_val = None

    return TradeConfig(
        side=params.get("side", "up"),
        amount=params.get("amount", 5.0),
        tp_pct=params.get("tp_pct", 0.50),
        sl_pct=params.get("sl_pct", 0.30),
        max_sl_reentry=params.get("max_sl_reentry", 0),
        max_tp_reentry=params.get("max_tp_reentry", 0),
        rounds=int(rounds_val) if rounds_val is not None else None,
    )
