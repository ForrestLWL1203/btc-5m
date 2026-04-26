"""Tests for polybot.config_loader — YAML config loading, series, and trade_config building."""

import pytest
import yaml

from polybot.config_loader import load_config, build_series, build_strategy, build_trade_config, STRATEGY_REGISTRY
from polybot.market.series import MarketSeries
from polybot.strategies.paired_window import PairedWindowStrategy


# ── load_config ──────────────────────────────────────────────────────────────


class TestLoadConfig:
    def test_load_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "test.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {"type": "retired_strategy"},
        }))
        cfg = load_config(str(cfg_file))
        assert cfg["market"]["asset"] == "btc"
        assert cfg["strategy"]["type"] == "retired_strategy"

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
    def test_build_paired_window_strategy(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {"strategy": {"type": "paired_window", "theta_pct": 0.03}}
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
        assert strat._theta_pct == pytest.approx(0.03)

    def test_build_paired_window_strategy_with_dynamic_cap(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "paired_window",
                "theta_pct": 0.03,
                "max_entry_price": 0.65,
                "strong_signal_threshold": 1.5,
                "strong_signal_max_entry_price": 0.67,
            }
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
        assert strat._max_entry_price == pytest.approx(0.65)
        assert strat._strong_signal_threshold == pytest.approx(1.5)
        assert strat._strong_signal_max_entry_price == pytest.approx(0.67)

    def test_build_paired_window_strategy_with_strength_caps(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "paired_window",
                "theta_pct": 0.03,
                "max_entry_price": 0.65,
                "strength_caps": [
                    {"threshold": 1.5, "max_entry_price": 0.70},
                    {"threshold": 3.5, "max_entry_price": 0.75},
                ],
            }
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
        assert strat._max_entry_price == pytest.approx(0.65)
        assert strat._strength_caps == [(1.5, 0.70), (3.5, 0.75)]

    def test_build_paired_window_strategy_with_optional_early_entry(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "paired_window",
                "theta_pct": 0.03,
                "entry_start_remaining_sec": 240,
                "early_entry_start_remaining_sec": 270,
                "early_entry_strength_threshold": 2.5,
                "early_entry_past_strength_threshold": 1.5,
            }
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
        assert strat._entry_start_remaining_sec == pytest.approx(240.0)
        assert strat._early_entry_start_remaining_sec == pytest.approx(270.0)
        assert strat._early_entry_strength_threshold == pytest.approx(2.5)
        assert strat._early_entry_past_strength_threshold == pytest.approx(1.5)

    def test_missing_strategy_raises(self):
        series = MarketSeries.from_known("btc-updown-5m")
        with pytest.raises(ValueError, match="Strategy type is required"):
            build_strategy({}, series)

    def test_unknown_strategy_raises(self):
        series = MarketSeries.from_known("btc-updown-5m")
        with pytest.raises(ValueError, match="Unknown strategy type"):
            build_strategy({"strategy": {"type": "nope"}}, series)

    def test_registry_has_paired_window(self):
        assert "paired_window" in STRATEGY_REGISTRY


# ── build_trade_config ───────────────────────────────────────────────────────


class TestBuildTradeConfig:
    def test_defaults(self):
        tc = build_trade_config({})
        assert tc.amount == 5.0
        assert tc.entry_ask_level == 1
        assert tc.ask_level_tiers == []
        assert tc.max_entries_per_window is None
        assert tc.rounds is None

    def test_custom_params(self):
        tc = build_trade_config({
            "params": {
                "amount": 10.0,
                "entry_ask_level": 1,
                "ask_level_tiers": [
                    {"threshold": 2.0, "level": 2},
                    {"threshold": 3.5, "level": 4},
                ],
                "amount_tiers": [
                    {"threshold": 2.0, "amount": 15.0},
                ],
                "normal_full_cap_guard": {
                    "enabled": True,
                    "min_signal_strength": 1.05,
                    "min_remaining_sec": 210,
                },
                "uncapped_depth_price_hint": {
                    "enabled": True,
                },
                "max_entries_per_window": 5,
            },
            "risk": {
                "consecutive_loss_amount": 30.0,
                "daily_loss_amount": 50.0,
                "consecutive_loss_pause_windows": 2,
                "daily_loss_pause_windows": 5,
            },
            "rounds": 3,
        })
        assert tc.amount == 10.0
        assert tc.entry_ask_level == 1
        assert tc.ask_level_tiers == [(2.0, 2), (3.5, 4)]
        assert tc.ask_level_for_signal_strength(1.5) == 1
        assert tc.ask_level_for_signal_strength(2.0) == 2
        assert tc.ask_level_for_signal_strength(3.5) == 4
        assert tc.amount_tiers == [(2.0, 15.0)]
        assert tc.amount_for_signal_strength(1.9) == pytest.approx(10.0)
        assert tc.amount_for_signal_strength(2.0) == pytest.approx(15.0)
        assert tc.normal_full_cap_guard_enabled is True
        assert tc.normal_full_cap_min_signal_strength == pytest.approx(1.05)
        assert tc.normal_full_cap_min_remaining_sec == pytest.approx(210.0)
        assert tc.uncapped_depth_price_hint_enabled is True
        assert tc.max_entries_per_window == 5
        assert tc.rounds == 3
        assert tc.consecutive_loss_amount_limit == pytest.approx(30.0)
        assert tc.daily_loss_amount_limit == pytest.approx(50.0)

    def test_rounds_zero_means_infinite(self):
        tc = build_trade_config({"rounds": 0})
        assert tc.rounds is None

    def test_rounds_negative_means_infinite(self):
        tc = build_trade_config({"rounds": -1})
        assert tc.rounds is None


# ── Integration: full YAML round-trip ────────────────────────────────────────


class TestYamlRoundTrip:
    def test_full_config_loads_series_and_trade_config(self, tmp_path):
        cfg_file = tmp_path / "full.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {
                "type": "paired_window",
            },
            "params": {
                "amount": 3.0,
                "max_entries_per_window": 5,
            },
            "rounds": 2,
        }))
        cfg = load_config(str(cfg_file))
        series = build_series(cfg)
        tc = build_trade_config(cfg)

        assert series.asset == "btc"
        assert series.timeframe == "5m"
        assert tc.amount == 3.0
        assert tc.max_entries_per_window == 5
        assert tc.rounds == 2

    def test_build_strategy_from_loaded_yaml(self, tmp_path):
        cfg_file = tmp_path / "paired.yaml"
        cfg_file.write_text(yaml.dump({
            "market": {"asset": "btc", "timeframe": "5m"},
            "strategy": {"type": "paired_window"},
        }))
        cfg = load_config(str(cfg_file))
        series = build_series(cfg)
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
