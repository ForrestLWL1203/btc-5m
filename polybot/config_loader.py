"""Load trading configuration from YAML file or CLI arguments."""

from pathlib import Path
from typing import Optional

from polybot.market.series import MarketSeries, KNOWN_SERIES, TIMEFRAME_SECONDS, _default_buffer
from polybot.strategies.latency_arb import LatencyArbStrategy
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
    "latency_arb": LatencyArbStrategy,
}


def build_strategy(cfg: dict, series: Optional[MarketSeries] = None):
    """Build Strategy from config dict. Strategy handles direction + buy decision."""
    strat_cfg = cfg.get("strategy", {})
    strat_type = strat_cfg.get("type", "latency_arb")

    if strat_type == "latency_arb":
        if series is None:
            raise ValueError("LatencyArbStrategy requires a market series")
        return LatencyArbStrategy(
            series=series,
            coefficients=strat_cfg.get("coefficients"),
            edge_threshold=strat_cfg.get("edge_threshold", 0.01),
            noise_threshold=strat_cfg.get("noise_threshold", 0.005),
            max_data_age_ms=strat_cfg.get("max_data_age_ms", 500.0),
            min_entry_price=strat_cfg.get("min_entry_price", 0.0),
            max_entry_price=strat_cfg.get("max_entry_price", 0.90),
            entry_window_sec=strat_cfg.get("entry_window_sec", 240.0),
            edge_exit_fraction=strat_cfg.get("edge_exit_fraction", 0.5),
            max_hold_sec=strat_cfg.get("max_hold_sec", 2.0),
            edge_decay_grace_ms=strat_cfg.get("edge_decay_grace_ms", 0.0),
            persistence_ms=strat_cfg.get("persistence_ms", 200.0),
            cooldown_sec=strat_cfg.get("cooldown_sec", 0.5),
            min_reentry_gap_sec=strat_cfg.get("min_reentry_gap_sec", 0.0),
            edge_rearm_threshold=strat_cfg.get("edge_rearm_threshold", 0.0),
            phase_one_sec=strat_cfg.get("phase_one_sec", 0.0),
            max_entries_phase_one=strat_cfg.get("max_entries_phase_one"),
            phase_two_sec=strat_cfg.get("phase_two_sec", 0.0),
            max_entries_phase_two=strat_cfg.get("max_entries_phase_two"),
            disable_after_sec=strat_cfg.get("disable_after_sec", 0.0),
        )

    raise ValueError(
        f"Unknown strategy type: {strat_type}. "
        f"Available: {', '.join(STRATEGY_REGISTRY.keys())}"
    )


def build_trade_config(cfg: dict) -> TradeConfig:
    """Build TradeConfig from config dict. Contains all common trading parameters."""
    params = cfg.get("params", {})

    rounds_val = cfg.get("rounds")
    if rounds_val is not None and int(rounds_val) <= 0:
        rounds_val = None

    return TradeConfig(
        amount=params.get("amount", 5.0),
        tp_pct=params.get("tp_pct", 0.50),
        sl_pct=params.get("sl_pct", 0.30),
        tp_price=params.get("tp_price"),
        sl_price=params.get("sl_price"),
        max_sl_reentry=params.get("max_sl_reentry", 0),
        max_tp_reentry=params.get("max_tp_reentry", 0),
        max_edge_reentry=params.get("max_edge_reentry", 0),
        max_entries_per_window=params.get("max_entries_per_window"),
        rounds=int(rounds_val) if rounds_val is not None else None,
    )
