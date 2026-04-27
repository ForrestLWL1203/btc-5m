"""Shared runtime input schema and validation for CLI and future UI/API callers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True)
class RuntimeInputField:
    """Schema entry for one runtime input."""

    name: str
    config_path: tuple[str, ...]
    value_type: str
    description: str
    ui_exposed: bool = True
    choices: Optional[tuple[Any, ...]] = None
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    default: Any = None
    cli_flag: Optional[str] = None
    coerce: Optional[Callable[[Any], Any]] = None

    def normalize(self, value: Any) -> Any:
        if self.coerce is not None:
            value = self.coerce(value)
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"{self.name} must be one of {list(self.choices)}")
        if self.value_type in {"int", "float"}:
            numeric = float(value)
            if self.minimum is not None and numeric < self.minimum:
                raise ValueError(f"{self.name} must be >= {self.minimum}")
            if self.maximum is not None and numeric > self.maximum:
                raise ValueError(f"{self.name} must be <= {self.maximum}")
        return value


RUNTIME_INPUT_FIELDS: tuple[RuntimeInputField, ...] = (
    RuntimeInputField(
        name="market",
        config_path=("market", "asset"),
        value_type="enum",
        choices=("btc", "eth"),
        description="Market asset",
        cli_flag="--market",
    ),
    RuntimeInputField(
        name="timeframe",
        config_path=("market", "timeframe"),
        value_type="enum",
        choices=("5m",),
        description="Market timeframe",
        cli_flag="--timeframe",
    ),
    RuntimeInputField(
        name="rounds",
        config_path=("rounds",),
        value_type="int",
        minimum=1,
        description="Number of windows to run",
        cli_flag="--rounds",
        coerce=int,
    ),
    RuntimeInputField(
        name="theta",
        config_path=("strategy", "theta_pct"),
        value_type="float",
        minimum=0.0001,
        maximum=5.0,
        description="BTC move threshold percent",
        ui_exposed=False,
        cli_flag="--theta",
        coerce=float,
    ),
    RuntimeInputField(
        name="persistence",
        config_path=("strategy", "persistence_sec"),
        value_type="float",
        minimum=1.0,
        maximum=300.0,
        description="BTC move persistence seconds",
        ui_exposed=False,
        cli_flag="--persistence",
        coerce=float,
    ),
    RuntimeInputField(
        name="max_entry_price",
        config_path=("strategy", "max_entry_price"),
        value_type="float",
        minimum=0.01,
        maximum=0.99,
        description="Entry price cap",
        cli_flag="--max-entry-price",
        coerce=float,
    ),
    RuntimeInputField(
        name="entry_start",
        config_path=("strategy", "entry_start_remaining_sec"),
        value_type="float",
        minimum=1.0,
        maximum=300.0,
        description="Entry band start remaining seconds",
        cli_flag="--entry-start",
        coerce=float,
    ),
    RuntimeInputField(
        name="entry_end",
        config_path=("strategy", "entry_end_remaining_sec"),
        value_type="float",
        minimum=1.0,
        maximum=300.0,
        description="Entry band end remaining seconds",
        cli_flag="--entry-end",
        coerce=float,
    ),
    RuntimeInputField(
        name="min_move_ratio",
        config_path=("strategy", "min_move_ratio"),
        value_type="float",
        minimum=0.0,
        maximum=5.0,
        description="Min ratio of current to past BTC move",
        ui_exposed=False,
        cli_flag="--min-move-ratio",
        coerce=float,
    ),
    RuntimeInputField(
        name="amount",
        config_path=("params", "amount"),
        value_type="float",
        minimum=0.01,
        maximum=100000.0,
        description="Trade size in USD per entry",
        cli_flag="--amount",
        coerce=float,
    ),
    RuntimeInputField(
        name="entry_ask_level",
        config_path=("params", "entry_ask_level"),
        value_type="int",
        minimum=1,
        maximum=20,
        description="Minimum ask-book level used for the BUY price hint; live depth still ignores level 1 for fillability",
        ui_exposed=False,
        cli_flag="--entry-ask-level",
        coerce=int,
    ),
    RuntimeInputField(
        name="low_price_threshold",
        config_path=("params", "low_price_threshold"),
        value_type="float",
        minimum=0.0,
        maximum=1.0,
        description="Use a deeper ask level when target-leg top ask is below this price",
        ui_exposed=False,
        cli_flag="--low-price-threshold",
        coerce=float,
    ),
    RuntimeInputField(
        name="low_price_entry_ask_level",
        config_path=("params", "low_price_entry_ask_level"),
        value_type="int",
        minimum=1,
        maximum=20,
        description="Ask-book level used when top ask is below low_price_threshold",
        ui_exposed=False,
        cli_flag="--low-price-entry-ask-level",
        coerce=int,
    ),
    RuntimeInputField(
        name="max_entries",
        config_path=("params", "max_entries_per_window"),
        value_type="int",
        minimum=1,
        maximum=10,
        description="Max entries per window",
        cli_flag="--max-entries",
        coerce=int,
    ),
    RuntimeInputField(
        name="consecutive_loss_amount",
        config_path=("risk", "consecutive_loss_amount"),
        value_type="float",
        minimum=0.0,
        maximum=100000.0,
        description="Pause after this much consecutive realized loss",
        ui_exposed=False,
        cli_flag="--consecutive-loss-amount",
        coerce=float,
    ),
    RuntimeInputField(
        name="daily_loss_amount",
        config_path=("risk", "daily_loss_amount"),
        value_type="float",
        minimum=0.0,
        maximum=100000.0,
        description="Pause after this much daily realized loss",
        ui_exposed=False,
        cli_flag="--daily-loss-amount",
        coerce=float,
    ),
)

_FIELD_BY_NAME = {field.name: field for field in RUNTIME_INPUT_FIELDS}


def runtime_input_schema(*, include_advanced: bool = True) -> list[dict[str, Any]]:
    """Return JSON-serializable schema metadata for runtime inputs."""
    fields = RUNTIME_INPUT_FIELDS if include_advanced else [f for f in RUNTIME_INPUT_FIELDS if f.ui_exposed]
    return [
        {
            "name": field.name,
            "type": field.value_type,
            "description": field.description,
            "ui_exposed": field.ui_exposed,
            "choices": list(field.choices) if field.choices is not None else None,
            "minimum": field.minimum,
            "maximum": field.maximum,
            "default": field.default,
        }
        for field in fields
    ]


def validate_runtime_inputs(
    values: dict[str, Any],
    *,
    include_advanced: bool = True,
) -> dict[str, Any]:
    """Validate and normalize runtime input overrides."""
    allowed = _FIELD_BY_NAME if include_advanced else {
        field.name: field for field in RUNTIME_INPUT_FIELDS if field.ui_exposed
    }
    normalized: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        field = allowed.get(key)
        if field is None:
            raise ValueError(f"Unknown runtime input: {key}")
        normalized[key] = field.normalize(value)
    _validate_runtime_input_relationships(normalized)
    return normalized


def runtime_input_field(name: str) -> RuntimeInputField:
    """Return schema entry by field name."""
    return _FIELD_BY_NAME[name]


def _validate_runtime_input_relationships(values: dict[str, Any]) -> None:
    entry_start = values.get("entry_start")
    entry_end = values.get("entry_end")
    if entry_start is not None and entry_end is not None and entry_start <= entry_end:
        raise ValueError("entry_start must be greater than entry_end")
