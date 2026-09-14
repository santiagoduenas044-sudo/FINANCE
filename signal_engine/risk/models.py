"""Domain model for the Risk Engine (Phase 7).

Signal -> Risk Validation -> Position Size / Risk Decision

RiskDecision is a first-class, always-explained result: APPROVED (with a
concrete, floored position size) or REJECTED (with every reason that applied,
not just the first). No dollars are ever computed for an instrument whose
ContractSpec has not passed `ContractRegistry.require_verified()` — see
`signal_engine/risk/engine.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RiskOutcome(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RiskReasonCode(str, Enum):
    """Machine-checkable reason categories, in addition to the human-readable
    `reasons` strings. Lets upstream code (paper log, output layer) branch on
    *why* without parsing free text."""
    NONE = "NONE"                                   # approved; no rejection
    UNVERIFIED_CONTRACT_SPEC = "UNVERIFIED_CONTRACT_SPEC"
    UNKNOWN_INSTRUMENT = "UNKNOWN_INSTRUMENT"
    INVALID_STOP = "INVALID_STOP"                    # zero/negative risk distance
    ZERO_RISK_BUDGET = "ZERO_RISK_BUDGET"            # computed max risk <= 0
    INSUFFICIENT_SIZE = "INSUFFICIENT_SIZE"          # floors to < 1 contract
    MAX_TRADES_PER_DAY = "MAX_TRADES_PER_DAY"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED = "DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED"


@dataclass(frozen=True)
class AccountState:
    """The caller's account snapshot "as of now". The Risk Engine NEVER
    derives this itself (no wall-clock, no hidden global state, no I/O) — it
    only uses exactly what is passed in, which is what makes it a pure
    function and keeps backtest/paper/live identical."""
    equity: float
    daily_realized_pnl: float = 0.0   # signed; negative = loss realized today
    trades_today: int = 0
    daily_realized_risk: float = 0.0  # sum of dollar_risk already committed today (informational)


@dataclass(frozen=True)
class RiskDecision:
    outcome: RiskOutcome
    instrument: str
    reason_codes: list[RiskReasonCode]
    reasons: list[str]

    # sizing (0 / 0.0 when a hard-stop prevented any computation, e.g. an
    # unverified spec or an invalid stop distance)
    stop_distance_points: float = 0.0
    stop_distance_ticks: float = 0.0
    risk_per_contract: float = 0.0     # "riesgo por contrato"
    max_risk_dollars: float = 0.0      # "riesgo máximo por operación" (post any cap)
    suggested_contracts: int = 0       # "contratos permitidos"
    dollar_risk: float = 0.0           # "riesgo total" = suggested_contracts * risk_per_contract
    risk_reward: list[float] = field(default_factory=list)   # carried through from Signal, unchanged

    # daily-state echo, for transparency/traceability
    daily_realized_pnl: float = 0.0
    daily_loss_limit_dollar: float = 0.0
    trades_today: int = 0
    max_trades_per_day: int = 0

    @property
    def approved(self) -> bool:
        return self.outcome is RiskOutcome.APPROVED
