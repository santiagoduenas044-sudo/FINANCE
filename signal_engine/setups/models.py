"""Domain model for setup candidates (Phase 5).

A SetupCandidate is NOT a LONG/SHORT order. It is a detected, self-explaining
trading *opportunity* with a directional bias, entry/stop/target proposals and
its rationale/invalidation. Converting a candidate into a definitive signal is
the future Signal Engine's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from ..features.market_structure.models import Direction


class SetupStatus(str, Enum):
    VALID = "VALID"        # emitted / still in play
    INVALID = "INVALID"    # protective (invalidation) level violated
    EXPIRED = "EXPIRED"    # validity horizon passed without resolution


@dataclass(frozen=True)
class LevelRef:
    """A referenced price level or structural event the setup relies on."""
    kind: str                    # e.g. "liquidity", "vwap", "opening_range_high", "swing_low"
    timestamp: pd.Timestamp | None
    price: float
    note: str = ""


@dataclass(frozen=True)
class SetupCandidate:
    setup_name: str
    instrument: str
    direction: Direction              # BULLISH / BEARISH bias (NOT a LONG/SHORT order)
    timestamp: pd.Timestamp           # the confirmation/decision bar N
    timeframe: str
    entry: float
    stop_loss: float
    take_profits: list[float]
    risk_reward: list[float]          # per take-profit, price-only (no $)
    reasons: list[str]                # rationale
    invalidation_conditions: list[str]
    invalidation_price: float         # machine-checkable protective level
    valid_until: pd.Timestamp         # expiration horizon
    market_context: dict = field(default_factory=dict)
    referenced: list[LevelRef] = field(default_factory=list)
    status: SetupStatus = SetupStatus.VALID


def risk_reward(entry: float, stop: float, targets: list[float]) -> list[float]:
    """Price-only R:R per target. No dollars, no multiplier (independent of the
    unverified ContractSpec)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return [0.0 for _ in targets]
    return [abs(t - entry) / risk for t in targets]


def update_status(candidate: SetupCandidate, bars_after: pd.DataFrame) -> SetupStatus:
    """Lifecycle transition given bars strictly AFTER the candidate's timestamp.

    Deterministic and used by downstream layers (Signal Engine / backtester) to
    track a candidate forward — it never affects emission:
      - INVALID if the protective level is violated,
      - EXPIRED if valid_until passes with neither TP1 nor the stop touched,
      - VALID otherwise.
    """
    future = bars_after[bars_after.index > candidate.timestamp]
    if future.empty:
        return candidate.status

    bullish = candidate.direction is Direction.BULLISH
    tp1 = candidate.take_profits[0] if candidate.take_profits else None
    for ts, row in future.iterrows():
        hit_stop = (row["low"] <= candidate.invalidation_price) if bullish \
            else (row["high"] >= candidate.invalidation_price)
        if hit_stop:
            return SetupStatus.INVALID
        if tp1 is not None:
            hit_tp = (row["high"] >= tp1) if bullish else (row["low"] <= tp1)
            if hit_tp:
                return SetupStatus.VALID  # thesis held to target
        if ts >= candidate.valid_until:
            return SetupStatus.EXPIRED
    return SetupStatus.VALID
