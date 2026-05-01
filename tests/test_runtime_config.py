"""Tests for runtime config assembly from presets and CLI-style overrides."""

import argparse

import pytest

from polybot.runtime_config import (
    advanced_runtime_input_schema,
    build_runtime_config,
    preset_config,
    public_runtime_input_schema,
)
from polybot.runtime_inputs import validate_runtime_inputs


def _args(**overrides) -> argparse.Namespace:
    defaults = {
        "config": None,
        "preset": None,
        "market": None,
        "timeframe": None,
        "rounds": None,
        "theta": None,
        "theta_start": None,
        "theta_end": None,
        "persistence": None,
        "max_entry_price": None,
        "entry_start": None,
        "entry_end": None,
        "min_move_ratio": None,
        "amount": None,
        "entry_ask_level": None,
        "low_price_threshold": None,
        "low_price_entry_ask_level": None,
        "max_entries": None,
        "stop_loss_enabled": None,
        "stop_loss_trigger_price": None,
        "stop_loss_trigger_drop_pct": None,
        "stop_loss_disable_below_entry_price": None,
        "stop_loss_start_remaining": None,
        "stop_loss_end_remaining": None,
        "stop_loss_sell_bid_level": None,
        "stop_loss_retry_count": None,
        "stop_loss_min_sell_price": None,
        "consecutive_loss_amount": None,
        "daily_loss_amount": None,
        "dry": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_preset_config_loads_enhanced_yaml():
    cfg = preset_config("enhanced")
    assert cfg["market"]["asset"] == "btc"
    assert cfg["strategy"]["theta_start_pct"] == pytest.approx(0.03)
    assert cfg["strategy"]["theta_end_pct"] == pytest.approx(0.048)
    assert cfg["strategy"]["max_entry_price"] == pytest.approx(0.75)
    assert cfg["params"]["entry_ask_level"] == 9
    assert cfg["params"]["low_price_threshold"] == pytest.approx(0.60)
    assert cfg["params"]["low_price_entry_ask_level"] == 11
    assert cfg["params"]["stop_loss"]["enabled"] is False


def test_preset_config_loads_crowd_m1_yaml():
    cfg = preset_config("crowd_m1")
    assert cfg["market"]["asset"] == "btc"
    assert cfg["strategy"]["type"] == "crowd_m1"
    assert cfg["strategy"]["entry_elapsed_sec"] == pytest.approx(170)
    assert cfg["strategy"]["entry_timeout_sec"] == pytest.approx(5)
    assert cfg["strategy"]["min_ask_gap"] == pytest.approx(0.0)
    assert cfg["strategy"]["min_leading_ask"] == pytest.approx(0.65)
    assert cfg["strategy"]["max_entry_price"] == pytest.approx(0.76)
    assert cfg["strategy"]["btc_direction_confirm"] is True
    assert cfg["strategy"]["btc_direction_deadband_pct"] == pytest.approx(0.015)
    assert cfg["strategy"]["btc_price_feed_source"] == "coinbase"
    assert "btc_reverse_filter" not in cfg["strategy"]
    assert cfg["params"]["entry_ask_level"] == 10
    assert "low_price_threshold" not in cfg["params"]
    assert "low_price_entry_ask_level" not in cfg["params"]
    assert "dynamic_entry_levels" not in cfg["params"]
    assert cfg["params"]["stop_loss"]["enabled"] is True
    assert cfg["params"]["stop_loss"]["trigger_drop_pct"] == pytest.approx(0.35)
    assert "trigger_price" not in cfg["params"]["stop_loss"]
    assert cfg["params"]["stop_loss"]["start_remaining_sec"] == pytest.approx(60)
    assert cfg["params"]["stop_loss"]["end_remaining_sec"] == pytest.approx(45)


def test_build_runtime_config_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one of --config or --preset"):
        build_runtime_config(_args())

    with pytest.raises(ValueError, match="exactly one of --config or --preset"):
        build_runtime_config(_args(config="a.yaml", preset="enhanced"))


def test_build_runtime_config_from_preset_applies_common_overrides():
    cfg = build_runtime_config(_args(
        preset="enhanced",
        market="btc",
        rounds=24,
        amount=2.0,
        theta_start=0.021,
        theta_end=0.041,
        entry_ask_level=4,
        low_price_threshold=0.58,
        low_price_entry_ask_level=8,
        max_entry_price=0.69,
        entry_start=250,
        entry_end=175,
        stop_loss_enabled=True,
        stop_loss_trigger_price=0.34,
        stop_loss_disable_below_entry_price=0.46,
        stop_loss_start_remaining=110,
        stop_loss_end_remaining=20,
        stop_loss_sell_bid_level=8,
        stop_loss_retry_count=2,
        stop_loss_min_sell_price=0.22,
    ))
    assert cfg["market"]["asset"] == "btc"
    assert cfg["rounds"] == 24
    assert cfg["params"]["amount"] == pytest.approx(2.0)
    assert cfg["params"]["entry_ask_level"] == 4
    assert cfg["params"]["low_price_threshold"] == pytest.approx(0.58)
    assert cfg["params"]["low_price_entry_ask_level"] == 8
    assert cfg["strategy"]["theta_start_pct"] == pytest.approx(0.021)
    assert cfg["strategy"]["theta_end_pct"] == pytest.approx(0.041)
    assert cfg["strategy"]["max_entry_price"] == pytest.approx(0.69)
    assert cfg["strategy"]["entry_start_remaining_sec"] == pytest.approx(250)
    assert cfg["strategy"]["entry_end_remaining_sec"] == pytest.approx(175)
    stop = cfg["params"]["stop_loss"]
    assert stop["enabled"] is True
    assert stop["trigger_price"] == pytest.approx(0.34)
    assert stop["disable_below_entry_price"] == pytest.approx(0.46)
    assert stop["start_remaining_sec"] == pytest.approx(110)
    assert stop["end_remaining_sec"] == pytest.approx(20)
    assert stop["sell_bid_level"] == 8
    assert stop["retry_count"] == 2
    assert stop["min_sell_price"] == pytest.approx(0.22)


def test_public_runtime_input_schema_only_exposes_frontend_safe_fields():
    schema = public_runtime_input_schema()
    names = {item["name"] for item in schema}
    assert {"market", "timeframe", "rounds", "amount", "max_entry_price", "entry_start", "entry_end", "max_entries"} <= names
    assert "theta" not in names
    assert "persistence" not in names


def test_advanced_runtime_input_schema_includes_engineering_fields():
    schema = advanced_runtime_input_schema()
    names = {item["name"] for item in schema}
    assert "theta" in names
    assert "theta_start" in names
    assert "theta_end" in names
    assert "entry_ask_level" in names
    assert "low_price_entry_ask_level" in names
    assert "stop_loss_enabled" in names
    assert "stop_loss_trigger_price" in names
    assert "stop_loss_trigger_drop_pct" in names


def test_validate_runtime_inputs_rejects_bad_ranges_and_relationships():
    with pytest.raises(ValueError, match="max_entry_price must be <= 0.99"):
        validate_runtime_inputs({"max_entry_price": 1.2})

    with pytest.raises(ValueError, match="entry_start must be greater than entry_end"):
        validate_runtime_inputs({"entry_start": 180, "entry_end": 210})

    with pytest.raises(ValueError, match="stop_loss_start_remaining must be greater"):
        validate_runtime_inputs({"stop_loss_start_remaining": 10, "stop_loss_end_remaining": 20})


def test_validate_runtime_inputs_rejects_unknown_public_field():
    with pytest.raises(ValueError, match="Unknown runtime input: theta"):
        validate_runtime_inputs({"theta": 0.03}, include_advanced=False)
