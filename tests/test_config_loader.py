"""Tests for polybot.config_loader — YAML config loading, series/strategy/trade_config building."""

import pytest
import yaml
from pathlib import Path

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config, STRATEGY_REGISTRY
from polybot.market.series import MarketSeries
from polybot.strategies.latency_arb import LatencyArbStrategy
from polybot.trade_config import TradeConfig


# ── load_config ──────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_load_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {"type": "latency_arb"},
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["market"]["asset"] == "btc"
        assert cfg["strategy"]["type"] == "latency_arb"

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
    def test_latency_arb_default(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {"strategy": {"type": "latency_arb"}}
        strat = build_strategy(cfg, series)
        assert isinstance(strat, LatencyArbStrategy)

    def test_latency_arb_with_coefficients(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "latency_arb",
                "coefficients": {"ret_2s": 1.0, "ret_5s": -0.2},
                "edge_threshold": 0.03,
            },
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, LatencyArbStrategy)

    def test_latency_arb_forwards_extended_tuning_fields(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "latency_arb",
                "min_entry_price": 0.2,
                "max_hold_sec": 1.2,
                "edge_decay_grace_ms": 300.0,
                "persistence_ms": 350.0,
                "cooldown_sec": 0.8,
                "min_reentry_gap_sec": 3.0,
                "edge_rearm_threshold": 0.01,
                "phase_one_sec": 90.0,
                "max_entries_phase_one": 2,
                "phase_two_sec": 180.0,
                "max_entries_phase_two": 3,
                "disable_after_sec": 180.0,
            },
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, LatencyArbStrategy)
        assert strat._min_entry_price == pytest.approx(0.2)
        assert strat._max_hold_sec == pytest.approx(1.2)
        assert strat._edge_decay_grace_ms == pytest.approx(300.0)
        assert strat._persistence_ms == pytest.approx(350.0)
        assert strat._cooldown_sec == pytest.approx(0.8)
        assert strat._min_reentry_gap_sec == pytest.approx(3.0)
        assert strat._edge_rearm_threshold == pytest.approx(0.01)
        assert strat._phase_one_sec == pytest.approx(90.0)
        assert strat._max_entries_phase_one == 2
        assert strat._phase_two_sec == pytest.approx(180.0)
        assert strat._max_entries_phase_two == 3
        assert strat._disable_after_sec == pytest.approx(180.0)

    def test_latency_arb_without_series_raises(self):
        cfg = {"strategy": {"type": "latency_arb"}}
        with pytest.raises(ValueError, match="requires a market series"):
            build_strategy(cfg, series=None)

    def test_default_type_is_latency_arb(self):
        series = MarketSeries.from_known("btc-updown-5m")
        strat = build_strategy({}, series)
        assert isinstance(strat, LatencyArbStrategy)

    def test_unknown_strategy_raises(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {"strategy": {"type": "nonexistent"}}
        with pytest.raises(ValueError, match="Unknown strategy type"):
            build_strategy(cfg, series)

    def test_registry_has_latency_arb(self):
        assert "latency_arb" in STRATEGY_REGISTRY


# ── build_trade_config ───────────────────────────────────────────────────────


class TestBuildTradeConfig:
    def test_defaults(self):
        tc = build_trade_config({})
        assert tc.amount == 5.0
        assert tc.tp_pct == 0.50
        assert tc.sl_pct == 0.30
        assert tc.max_sl_reentry == 0
        assert tc.max_tp_reentry == 0
        assert tc.max_edge_reentry == 0
        assert tc.max_entries_per_window is None
        assert tc.rounds is None

    def test_custom_params(self):
        tc = build_trade_config({
            "params": {
                "amount": 10.0,
                "tp_pct": 0.60,
                "sl_pct": 0.40,
                "max_sl_reentry": 2,
                "max_tp_reentry": 1,
                "max_edge_reentry": 3,
                "max_entries_per_window": 5,
            },
            "rounds": 3,
        })
        assert tc.amount == 10.0
        assert tc.tp_pct == 0.60
        assert tc.sl_pct == 0.40
        assert tc.max_sl_reentry == 2
        assert tc.max_tp_reentry == 1
        assert tc.max_edge_reentry == 3
        assert tc.max_entries_per_window == 5
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
            "strategy": {
                "type": "latency_arb",
                "coefficients": {"ret_2s": 0.985},
                "edge_threshold": 0.02,
            },
            "params": {
                "amount": 3.0,
                "tp_pct": 0.40,
                "sl_pct": 0.25,
                "max_sl_reentry": 0,
                "max_tp_reentry": 0,
                "max_edge_reentry": 4,
                "max_entries_per_window": 5,
            },
            "rounds": 2,
        }))
        cfg = load_config(str(cfg_file))
        series = build_series(cfg)
        strat = build_strategy(cfg, series)
        tc = build_trade_config(cfg)

        assert series.asset == "btc"
        assert series.timeframe == "5m"
        assert isinstance(strat, LatencyArbStrategy)
        assert tc.amount == 3.0
        assert tc.max_edge_reentry == 4
        assert tc.max_entries_per_window == 5
        assert tc.rounds == 2
