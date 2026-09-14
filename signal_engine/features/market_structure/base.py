"""Backend-agnostic market-structure interface.

Consumers depend on this ABC and the domain models, never on a concrete backend.
Implementations: SmcStructureProvider (wraps smart-money-concepts) and
NativeStructureProvider (our own). Swapping the backend requires no changes to
Setup Engine / Signal Engine / Backtester.

CONTRACT: every method receives an AS-OF bars frame (index <= decision bar N,
already sliced by AnalysisContext) and must return only events that are KNOWN by
the last bar of that frame. Implementations must not use information beyond the
provided frame, and must drop trailing, not-yet-confirmed detections.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from .models import (
    FairValueGap, LiquidityLevel, OrderBlock, StructureEvent, SwingPoint,
)


class MarketStructureProvider(ABC):
    @abstractmethod
    def swings(self, bars: pd.DataFrame) -> list[SwingPoint]:
        """Confirmed swing highs/lows (HH/HL/LH/LL labels attached)."""

    @abstractmethod
    def structure_events(self, bars: pd.DataFrame) -> list[StructureEvent]:
        """Confirmed BOS / CHoCH (only breaks that already happened)."""

    @abstractmethod
    def fair_value_gaps(self, bars: pd.DataFrame) -> list[FairValueGap]:
        """Confirmed fair value gaps (3-candle), with mitigation if any."""

    @abstractmethod
    def liquidity_levels(self, bars: pd.DataFrame) -> list[LiquidityLevel]:
        """Liquidity levels (equal highs/lows), with sweep info if any."""

    @abstractmethod
    def order_blocks(self, bars: pd.DataFrame) -> list[OrderBlock]:
        """Order blocks, with mitigation info if any."""
