"""Market structure package: library-agnostic models + interface + backends."""
from __future__ import annotations

from .base import MarketStructureProvider
from .models import (
    Direction, FairValueGap, LiquidityLevel, OrderBlock, StructureEvent,
    StructureType, SwingLabel, SwingPoint, SwingType,
)
from .native import NativeStructureProvider


def get_structure_provider(backend: str = "smc", **kwargs) -> MarketStructureProvider:
    """Factory. backend in {"smc", "native"}. Defaults to smc (reuses
    smart-money-concepts); falls back with a clear error if unavailable."""
    if backend == "native":
        return NativeStructureProvider(**kwargs)
    if backend == "smc":
        from .smc_adapter import SmcStructureProvider
        return SmcStructureProvider(**kwargs)
    raise ValueError(f"unknown structure backend: {backend!r}")


__all__ = [
    "MarketStructureProvider", "NativeStructureProvider", "get_structure_provider",
    "SwingPoint", "SwingType", "SwingLabel", "StructureEvent", "StructureType",
    "FairValueGap", "LiquidityLevel", "OrderBlock", "Direction",
]
