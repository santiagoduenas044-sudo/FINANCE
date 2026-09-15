"""Bot Runtime (Phase 9): the real ON/OFF/PAUSE switch for the trading
pipeline, plus health/status tracking for the dashboard and Discord.

Market Data -> Analysis -> Setup -> Signal -> Risk -> Runtime -> Dashboard/Discord

No order execution, no broker, no AI. This package does not modify the
Setup/Signal/Risk engines — it only orchestrates them.
"""
from __future__ import annotations

from .engine import BotRuntime
from .models import (
    BotStatus, ComponentHealth, ComponentStatus, CycleResult, InstrumentStatus,
    RuntimeEvent,
)

__all__ = [
    "BotRuntime", "BotStatus", "ComponentStatus", "ComponentHealth",
    "InstrumentStatus", "RuntimeEvent", "CycleResult",
]
