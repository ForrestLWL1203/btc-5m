"""Runtime config assembly for CLI, presets, and future UI/API callers."""

from __future__ import annotations

import argparse
import copy
from pathlib import Path
from typing import Any

from polybot.config_loader import load_config
from polybot.runtime_inputs import (
    RUNTIME_INPUT_FIELDS,
    runtime_input_field,
    runtime_input_schema,
    validate_runtime_inputs,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent

PRESET_PATHS = {
    "enhanced": _REPO_ROOT / "paired_window_early_entry_dry.yaml",
}


def add_runtime_config_args(parser: argparse.ArgumentParser) -> None:
    """Add runtime configuration arguments to the CLI parser."""
    parser.add_argument(
        "--config",
        type=str,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESET_PATHS),
        help="Named runtime preset; use instead of --config for UI/API-friendly startup",
    )
    for field in RUNTIME_INPUT_FIELDS:
        if field.value_type == "bool":
            parser.add_argument(
                field.cli_flag,
                dest=field.name,
                action="store_true",
                help=field.description,
            )
            parser.add_argument(
                f"--no-{field.cli_flag[2:]}",
                dest=field.name,
                action="store_false",
                help=f"Disable: {field.description}",
            )
            parser.set_defaults(**{field.name: None})
            continue
        if field.value_type == "int":
            arg_type = int
        elif field.value_type == "float":
            arg_type = float
        else:
            arg_type = str
        parser.add_argument(
            field.cli_flag,
            type=arg_type,
            choices=list(field.choices) if field.choices is not None else None,
            help=field.description,
        )
def build_runtime_config(args: argparse.Namespace) -> dict:
    """Build the effective runtime config from preset/config and CLI overrides."""
    if bool(args.config) == bool(args.preset):
        raise ValueError("Provide exactly one of --config or --preset")

    cfg = _load_base_config(args)
    _apply_cli_overrides(cfg, args)
    return cfg


def _load_base_config(args: argparse.Namespace) -> dict:
    if args.config:
        return load_config(args.config)
    preset_path = PRESET_PATHS[args.preset]
    return load_config(str(preset_path))


def _apply_cli_overrides(cfg: dict, args: argparse.Namespace) -> None:
    """Merge explicit CLI args into the loaded config dict in-place."""
    raw_overrides = {
        field.name: getattr(args, field.name, None)
        for field in RUNTIME_INPUT_FIELDS
    }
    overrides = validate_runtime_inputs(raw_overrides, include_advanced=True)
    apply_runtime_overrides(cfg, overrides)


def apply_runtime_overrides(cfg: dict, overrides: dict[str, Any]) -> None:
    """Apply validated runtime overrides into a config dict in-place."""
    for name, value in overrides.items():
        path = runtime_input_field(name).config_path
        _set_path(cfg, path, value)


def _set_path(cfg: dict, path: tuple[str, ...], value: Any) -> None:
    node = cfg
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def preset_config(name: str) -> dict:
    """Return a copy of a named preset config for tests or future UI callers."""
    if name not in PRESET_PATHS:
        raise KeyError(name)
    return copy.deepcopy(load_config(str(PRESET_PATHS[name])))


def public_runtime_input_schema() -> list[dict[str, Any]]:
    """Return schema for frontend-safe runtime inputs."""
    return runtime_input_schema(include_advanced=False)


def advanced_runtime_input_schema() -> list[dict[str, Any]]:
    """Return schema including advanced engineering inputs."""
    return runtime_input_schema(include_advanced=True)
