"""Library-agnostic domain models for market structure.

These are what the rest of the system (Setup Engine, Signal Engine, Backtester)
consumes. They are intentionally decoupled from smart-money-concepts (or any
other backend), so the backend can be swapped without touching consumers.

Every event carries a ``timestamp`` (when it occurred / was observed) and, where
the concept requires later bars to be known, a ``confirmed_at`` timestamp. In
as-of usage both are always <= the decision bar N (guaranteed by the providers).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd


class SwingType(str, Enum):
    HIGH = "HIGH"
    LOW = "LOW"


class SwingLabel(str, Enum):
    HH = "HH"   # higher high
    HL = "HL"   # higher low
    LH = "LH"   # lower high
    LL = "LL"   # lower low


class Direction(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class StructureType(str, Enum):
    BOS = "BOS"       # break of structure (continuation)
    CHOCH = "CHOCH"   # change of character (reversal)


@dataclass(frozen=True)
class SwingPoint:
    timestamp: pd.Timestamp     # bar of the swing extreme
    price: float
    type: SwingType
    confirmed_at: pd.Timestamp  # bar at which the swing became confirmed
    label: SwingLabel | None = None


@dataclass(frozen=True)
class StructureEvent:
    timestamp: pd.Timestamp     # the bar where structure broke (the break)
    type: StructureType
    direction: Direction
    level: float                # the swing level that was broken
    swing_timestamp: pd.Timestamp  # the swing that defined the level


@dataclass(frozen=True)
class FairValueGap:
    timestamp: pd.Timestamp     # formation bar (3rd candle close) => confirmed
    top: float
    bottom: float
    direction: Direction
    mitigated_at: pd.Timestamp | None = None


@dataclass(frozen=True)
class LiquidityLevel:
    timestamp: pd.Timestamp     # bar of the level (swing-based)
    level: float
    direction: Direction        # BULLISH = buy-side (highs), BEARISH = sell-side (lows)
    swept_at: pd.Timestamp | None = None


@dataclass(frozen=True)
class OrderBlock:
    timestamp: pd.Timestamp     # formation bar
    top: float
    bottom: float
    direction: Direction
    volume: float | None = None
    mitigated_at: pd.Timestamp | None = None
