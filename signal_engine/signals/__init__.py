"""Signal Engine (Phase 6): SetupCandidate -> Signal | NoTrade.

Setup Engine -> Signal Engine -> Output Dispatcher -> Discord

This package has NO dependency on signal_engine.output (Discord or any other
channel), and no dependency on risk/sizing, execution, TradingView, or IA/LLM.
"""
from __future__ import annotations

from .conflicts import conflict_reason, find_conflicts
from .engine import SignalEngine
from .models import (
    DIRECTION_TO_SIGNAL, NoTrade, Signal, SignalDirection, compute_signal_id,
)

__all__ = [
    "SignalEngine", "Signal", "NoTrade", "SignalDirection",
    "DIRECTION_TO_SIGNAL", "compute_signal_id", "find_conflicts", "conflict_reason",
]
