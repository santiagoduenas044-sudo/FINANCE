"""Domain model for the Signal Engine (Phase 6): SetupCandidate -> Signal | NoTrade.

A Signal is the definitive LONG/SHORT trading signal produced once a
SetupCandidate has passed every validation rule. NoTrade is a first-class,
always-explained rejection (never a silent drop).

This module has NO dependency on Discord, output adapters, risk/sizing, or
execution. Converting a candidate's BULLISH/BEARISH *bias* into a definitive
LONG/SHORT *decision* is exactly the job this module exists to do (the thing
Phase 5 explicitly deferred).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from ..features.market_structure.models import Direction
from ..setups.models import LevelRef


class SignalDirection(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


#: SetupCandidate.direction (BULLISH/BEARISH bias) -> definitive order side.
DIRECTION_TO_SIGNAL: dict[Direction, SignalDirection] = {
    Direction.BULLISH: SignalDirection.LONG,
    Direction.BEARISH: SignalDirection.SHORT,
}


def compute_signal_id(
    instrument: str,
    setup_name: str,
    direction: SignalDirection,
    timestamp: pd.Timestamp,
    timeframe: str,
    entry: float,
    stop: float,
) -> str:
    """Deterministic signal identity — sha256 of the relevant fields, no
    randomness. The same candidate always produces the same signal_id, which is
    also used as the engine's deduplication key."""
    raw = "|".join([
        instrument,
        setup_name,
        direction.value,
        timestamp.isoformat(),
        timeframe,
        f"{entry:.10g}",
        f"{stop:.10g}",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Signal:
    """A definitive, actionable trading signal. Still NOT sized (no risk/$ —
    that is a future Risk Engine's job) and NOT executed."""
    signal_id: str
    instrument: str
    setup: str
    direction: SignalDirection        # LONG / SHORT
    timestamp: pd.Timestamp
    timeframe: str
    entry: float
    stop: float
    take_profits: list[float]
    risk_reward: list[float]          # price-only, per take-profit
    reasons: list[str]
    market_context: dict = field(default_factory=dict)
    invalidation_conditions: list[str] = field(default_factory=list)
    invalidation_price: float = 0.0
    referenced: list[LevelRef] = field(default_factory=list)
    expiration: pd.Timestamp | None = None


@dataclass(frozen=True)
class NoTrade:
    """A first-class 'no trade' outcome. Always carries at least one reason —
    NO_TRADE is a normal, explained result, never a silent no-op."""
    instrument: str
    timestamp: pd.Timestamp
    reasons: list[str]
    setup: str | None = None
    timeframe: str | None = None
    candidate_direction: SignalDirection | None = None
