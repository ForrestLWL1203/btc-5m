"""TradeConfig — runtime parameters shared by the active paired-window strategy."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class TradeConfig:
    """Execution controls for the current runtime strategy."""

    amount: float = 5.0
    max_entries_per_window: Optional[int] = None
    rounds: Optional[int] = None  # None = infinite
