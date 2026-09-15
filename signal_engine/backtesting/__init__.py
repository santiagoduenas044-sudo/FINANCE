"""Backtesting Engine (Phase 8).

Setup Engine -> Signal Engine -> (optional) Risk Engine -> trade resolution -> metrics

No execution, no broker, no dashboard, no parameter optimization, no
Walk-Forward/Monte-Carlo analysis (deliberately out of scope this phase — see
docs/PHASE8_BACKTESTING.md).
"""
from __future__ import annotations

from .dollars import compute_dollar_result
from .engine import BacktestEngine
from .metrics import compute_metrics
from .models import (
    BacktestMetrics, BacktestResult, ClosedTrade, CostModel, DollarResult,
    GroupStats, TradeOutcome,
)
from .simulator import resolve_trade_outcome

__all__ = [
    "BacktestEngine", "BacktestResult", "BacktestMetrics", "ClosedTrade",
    "TradeOutcome", "CostModel", "DollarResult", "GroupStats",
    "resolve_trade_outcome", "compute_dollar_result", "compute_metrics",
]
