"""Domain model for the Bot Runtime (Phase 9).

BotStatus is a REAL control state — `BotRuntime.run_cycle()` refuses to run
the trading pipeline unless status is ONLINE. Pausing/stopping is not a UI-only
toggle: it changes what the runtime will actually do on its next cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class BotStatus(str, Enum):
    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"
    PAUSED = "PAUSED"
    ERROR = "ERROR"


class ComponentStatus(str, Enum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    DISABLED = "DISABLED"   # e.g. Discord/Risk not configured for this run


@dataclass(frozen=True)
class ComponentHealth:
    name: str
    status: ComponentStatus
    detail: str = ""


@dataclass(frozen=True)
class InstrumentStatus:
    symbol: str
    last_price: float | None = None
    last_bar_time: datetime | None = None
    session: str | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str          # "BOT_STARTED", "BOT_PAUSED", "BOT_STOPPED", "BOT_ERROR", "CYCLE"
    timestamp: datetime
    detail: str = ""


@dataclass(frozen=True)
class CycleResult:
    ran: bool
    timestamp: datetime
    reason: str | None = None          # why skipped, when ran=False
    signals_produced: int = 0
    no_trades_produced: int = 0
    errors: list[str] = field(default_factory=list)
