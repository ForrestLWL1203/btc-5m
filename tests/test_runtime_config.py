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
        "consecutive_loss_amount": None,
        "daily_loss_amount": None,
        "dry": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_preset_config_loads_enhanced_yaml():
    cfg = preset_config("enhanced")
    assert cfg["market"]["asset"] == "btc"
    assert cfg["strategy"]["max_entry_price"] == pytest.approx(0.72)
    assert cfg["params"]["entry_ask_level"] == 7
    assert cfg["params"]["low_price_threshold"] == pytest.approx(0.60)
    assert cfg["params"]["low_price_entry_ask_level"] == 9


def test_build_runtime_config_requires_exactly_one_source():
    with pytest.raises(ValueError, match="exactly one of --config or --preset"):
        build_runtime_config(_args())

    with pytest.raises(ValueError, match="exactly one of --config or --preset"):
        build_runtime_config(_args(config="a.yaml", preset="enhanced"))


def test_build_runtime_config_from_preset_applies_common_overrides():
    cfg = build_runtime_config(_args(
        preset="enhanced",
        market="eth",
        rounds=24,
        amount=2.0,
        entry_ask_level=4,
        low_price_threshold=0.58,
        low_price_entry_ask_level=8,
        max_entry_price=0.69,
        entry_start=250,
        entry_end=175,
    ))
    assert cfg["market"]["asset"] == "eth"
    assert cfg["rounds"] == 24
    assert cfg["params"]["amount"] == pytest.approx(2.0)
    assert cfg["params"]["entry_ask_level"] == 4
    assert cfg["params"]["low_price_threshold"] == pytest.approx(0.58)
    assert cfg["params"]["low_price_entry_ask_level"] == 8
    assert cfg["strategy"]["max_entry_price"] == pytest.approx(0.69)
    assert cfg["strategy"]["entry_start_remaining_sec"] == pytest.approx(250)
    assert cfg["strategy"]["entry_end_remaining_sec"] == pytest.approx(175)


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
    assert "entry_ask_level" in names
    assert "low_price_entry_ask_level" in names


def test_validate_runtime_inputs_rejects_bad_ranges_and_relationships():
    with pytest.raises(ValueError, match="max_entry_price must be <= 0.99"):
        validate_runtime_inputs({"max_entry_price": 1.2})

    with pytest.raises(ValueError, match="entry_start must be greater than entry_end"):
        validate_runtime_inputs({"entry_start": 180, "entry_end": 210})


def test_validate_runtime_inputs_rejects_unknown_public_field():
    with pytest.raises(ValueError, match="Unknown runtime input: theta"):
        validate_runtime_inputs({"theta": 0.03}, include_advanced=False)
