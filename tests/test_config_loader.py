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

    def test_build_paired_window_strategy_with_current_entry_band(self):
        series = MarketSeries.from_known("btc-updown-5m")
        cfg = {
            "strategy": {
                "type": "paired_window",
                "theta_pct": 0.03,
                "max_entry_price": 0.72,
                "entry_start_remaining_sec": 255,
                "entry_end_remaining_sec": 120,
            }
        }
        strat = build_strategy(cfg, series)
        assert isinstance(strat, PairedWindowStrategy)
        assert strat._max_entry_price == pytest.approx(0.72)
        assert strat._entry_start_remaining_sec == pytest.approx(255.0)
        assert strat._entry_end_remaining_sec == pytest.approx(120.0)

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
        assert tc.max_entries_per_window is None
        assert tc.rounds is None

    def test_custom_params(self):
        tc = build_trade_config({
            "params": {
                "amount": 10.0,
                "entry_ask_level": 1,
                "low_price_threshold": 0.60,
                "low_price_entry_ask_level": 9,
                "amount_tiers": [
                    {"threshold": 2.0, "amount": 15.0},
                ],
                "stop_loss": {
                    "enabled": True,
                    "multiplier": 1.2,
                    "start_remaining_sec": 120,
                    "end_remaining_sec": 15,
                    "sell_bid_level": 9,
                    "retry_count": 3,
                    "min_sell_price": 0.20,
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
        assert tc.low_price_threshold == pytest.approx(0.60)
        assert tc.low_price_entry_ask_level == 9
        assert tc.base_entry_ask_level() == 1
        assert tc.amount_tiers == [(2.0, 15.0)]
        assert tc.amount_for_signal_strength(1.9) == pytest.approx(10.0)
        assert tc.amount_for_signal_strength(2.0) == pytest.approx(15.0)
        assert tc.stop_loss_enabled is True
        assert tc.stop_loss_multiplier == pytest.approx(1.2)
        assert tc.stop_loss_start_remaining_sec == pytest.approx(120)
        assert tc.stop_loss_end_remaining_sec == pytest.approx(15)
        assert tc.stop_loss_bid_level() == 9
        assert tc.stop_loss_retry_count == 3
        assert tc.stop_loss_min_sell_price == pytest.approx(0.20)
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
