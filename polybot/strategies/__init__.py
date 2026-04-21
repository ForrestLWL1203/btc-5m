"""Trading strategies package."""

from .base import Strategy
from .paired_window import PairedWindowStrategy

__all__ = ["Strategy", "PairedWindowStrategy"]
