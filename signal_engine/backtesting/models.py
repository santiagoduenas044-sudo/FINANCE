"""Domain model for the Backtesting Engine (Phase 8).

Two layers of result, deliberately decoupled:

  1) R-MULTIPLE outcome (always computable): resolved purely from a Signal's
     own entry/stop/take_profit prices plus subsequent bars. No ContractSpec,
     no dollars, no CME dependency at all.
  2) DOLLAR outcome (optional, gated): only computed when a RiskEngine backed
     by a VERIFIED ContractRegistry is supplied. Exactly the same
     `require_verified()` gate as Phase 7 — never bypassed here either.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd

from ..risk.models import RiskDecision
from ..signals.models import Signal, SignalDirection


class TradeOutcome(str, Enum):
    WIN = "WIN"
    LOSS = "LOSS"
    EXPIRED = "EXPIRED"   # signal.expiration passed with neither TP nor stop touched
    OPEN = "OPEN"         # historical data ran out before resolution


@dataclass(frozen=True)
class CostModel:
    """Applied ONLY to the dollar pipeline (needs tick_size/commission, both
    real-world/CME-adjacent facts) — the R-multiple pipeline stays idealized
    and cost-free by design (documented limitation, see PHASE8_BACKTESTING.md)."""
    slippage_ticks: float = 1.0
    commission_per_contract_per_side: float = 0.0


@dataclass(frozen=True)
class DollarResult:
    """Only ever populated when RiskDecision.outcome == APPROVED (i.e. a
    verified ContractSpec produced a real position size)."""
    suggested_contracts: int
    entry_fill: float
    exit_fill: float
    gross_pnl: float
    commission_total: float
    net_pnl: float


@dataclass(frozen=True)
class ClosedTrade:
    signal_id: str
    instrument: str
    setup: str
    direction: SignalDirection
    entry_timestamp: pd.Timestamp
    exit_timestamp: pd.Timestamp | None
    entry: float
    stop: float
    take_profit: float | None       # TP1 — the target used for resolution
    outcome: TradeOutcome
    r_multiple: float | None        # None for EXPIRED / OPEN
    bars_held: int
    risk_decision: RiskDecision | None = None
    dollar: DollarResult | None = None


@dataclass(frozen=True)
class GroupStats:
    """Aggregate stats for one slice (an instrument, a setup, a direction)."""
    trades: int
    wins: int
    losses: int
    win_rate: float
    net_r: float


@dataclass(frozen=True)
class BacktestMetrics:
    total_signals: int              # every Signal produced (resolved or not)
    no_trade_count: int             # every NoTrade produced
    resolved_trades: int            # WIN + LOSS only
    wins: int
    losses: int
    expired: int
    open_trades: int
    win_rate: float
    avg_win_r: float
    avg_loss_r: float                # positive magnitude
    profit_factor: float             # sum(win R) / abs(sum(loss R))
    expectancy_r: float              # mean R across resolved trades
    net_r: float                     # sum R across resolved trades
    max_drawdown_r: float            # peak-to-trough on cumulative R equity curve
    sharpe_r: float                  # mean(R)/std(R), per-trade (NOT annualized)
    sortino_r: float                 # mean(R)/downside-std(R), per-trade
    max_consecutive_wins: int
    max_consecutive_losses: int
    by_instrument: dict[str, GroupStats] = field(default_factory=dict)
    by_setup: dict[str, GroupStats] = field(default_factory=dict)
    by_direction: dict[str, GroupStats] = field(default_factory=dict)
    # dollar aggregates — None unless every trade's sizing came from a
    # verified registry (see BacktestEngine)
    net_pnl_dollars: float | None = None
    max_drawdown_dollars: float | None = None
    avg_win_dollars: float | None = None
    avg_loss_dollars: float | None = None
    total_commission_dollars: float | None = None
    dollar_metrics_reason: str | None = None   # explains why dollar fields are None


@dataclass(frozen=True)
class BacktestResult:
    symbol: str
    trades: list[ClosedTrade]
    metrics: BacktestMetrics
