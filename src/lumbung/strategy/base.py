"""Shared strategy types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Action(StrEnum):
    ENTER_LONG = "enter_long"
    EXIT = "exit"
    HOLD = "hold"


@dataclass(frozen=True)
class Signal:
    """A strategy's decision for one pair at one bar.

    `price` is the bar's close -- the reference the executor measures slippage
    against, not necessarily the price we end up paying.
    """

    action: Action
    pair: str
    price: float
    stop: float | None = None
    atr: float | None = None
    reason: str = ""
    bar_time: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        """Distance from entry to stop, in IDR per coin. The sizing denominator."""
        if self.stop is None:
            return 0.0
        return max(self.price - self.stop, 0.0)
