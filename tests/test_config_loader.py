"""Tests for polybot.config_loader — YAML config loading, series/strategy/trade_config building."""

import pytest
import yaml
from pathlib import Path

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config, STRATEGY_REGISTRY
from polybot.market.series import MarketSeries
from polybot.strategies.immediate import ImmediateStrategy
from polybot.trade_config import TradeConfig


# ── load_config ──────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_load_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {"type": "immediate"},
            "params": {"side": "up"},
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["market"]["asset"] == "btc"
        assert cfg["strategy"]["type"] == "immediate"

    def test_load_empty_yaml(self, tmp_path):
        cfg_file = tmp_path / "empty.yaml"
        cfg_file.write_text("")
        cfg = load_config(str(cfg_file))
        assert cfg == {}

    def test_load_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path.yaml")

    def test_load_none_returns_empty(self):
        cfg = load_config(None)
        assert cfg == {}


# ── build_series ─────────────────────────────────────────────────────────────


class TestBuildSeries:
    def test_btc_5m_known_series(self):
        cfg = {"market": {"asset": "btc", "timeframe": "5m"}}
        series = build_series(cfg)
        assert isinstance(series, MarketSeries)
        assert series.asset == "btc"
        assert series.timeframe == "5m"
        assert series.slug_step == 300

    def test_defaults_when_market_missing(self):
        series = build_series({})
        assert series.asset == "btc"
        assert series.timeframe == "5m"

    def test_custom_series_with_slug_prefix(self):
        cfg = {
            "market": {
                "asset": "eth",
                "timeframe": "1d",
                "slug_prefix": "eth-updown-1d",
            },
        }
        series = build_series(cfg)
        assert series.asset == "eth"
        assert series.timeframe == "1d"
        assert series.slug_step == 86400
        assert series.slug_prefix == "eth-updown-1d"


# ── build_strategy ───────────────────────────────────────────────────────────


class TestBuildStrategy:
    def test_default_immediate(self):
        cfg = {"strategy": {"type": "immediate"}}
        strat = build_strategy(cfg)
        assert isinstance(strat, ImmediateStrategy)

    def test_empty_strategy_uses_defaults(self):
        strat = build_strategy({})
        assert isinstance(strat, ImmediateStrategy)

    def test_unknown_strategy_raises(self):
        cfg = {"strategy": {"type": "nonexistent"}}
        with pytest.raises(ValueError, match="Unknown strategy type"):
            build_strategy(cfg)

    def test_registry_has_immediate(self):
        assert "immediate" in STRATEGY_REGISTRY
        assert STRATEGY_REGISTRY["immediate"] is ImmediateStrategy


# ── build_trade_config ───────────────────────────────────────────────────────


class TestBuildTradeConfig:
    def test_defaults(self):
        tc = build_trade_config({})
        assert tc.side == "up"
        assert tc.amount == 5.0
        assert tc.tp_pct == 0.50
        assert tc.sl_pct == 0.30
        assert tc.max_sl_reentry == 0
        assert tc.max_tp_reentry == 0
        assert tc.rounds is None

    def test_custom_params(self):
        tc = build_trade_config({
            "params": {
                "side": "down",
                "amount": 10.0,
                "tp_pct": 0.60,
                "sl_pct": 0.40,
                "max_sl_reentry": 2,
                "max_tp_reentry": 1,
            },
            "rounds": 3,
        })
        assert tc.side == "down"
        assert tc.amount == 10.0
        assert tc.tp_pct == 0.60
        assert tc.sl_pct == 0.40
        assert tc.max_sl_reentry == 2
        assert tc.max_tp_reentry == 1
        assert tc.rounds == 3

    def test_rounds_zero_means_infinite(self):
        tc = build_trade_config({"rounds": 0})
        assert tc.rounds is None

    def test_rounds_negative_means_infinite(self):
        tc = build_trade_config({"rounds": -1})
        assert tc.rounds is None


# ── Integration: full YAML round-trip ────────────────────────────────────────


class TestYamlRoundTrip:
    def test_full_config_builds_all(self, tmp_path):
        cfg_file = tmp_path / "full.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {"type": "immediate"},
            "params": {
                "side": "down",
                "amount": 3.0,
                "tp_pct": 0.40,
                "sl_pct": 0.25,
                "max_sl_reentry": 1,
                "max_tp_reentry": 0,
            },
            "rounds": 2,
        }))
        cfg = load_config(str(cfg_file))
        series = build_series(cfg)
        strat = build_strategy(cfg)
        tc = build_trade_config(cfg)

        assert series.asset == "btc"
        assert series.timeframe == "5m"
        assert isinstance(strat, ImmediateStrategy)
        assert tc.side == "down"
        assert tc.amount == 3.0
        assert tc.max_sl_reentry == 1
        assert tc.max_tp_reentry == 0
        assert tc.rounds == 2
