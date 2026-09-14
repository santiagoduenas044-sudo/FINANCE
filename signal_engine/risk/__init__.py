"""Risk Engine (Phase 7): Signal -> Risk Validation -> Position Size / Risk Decision.

Signal Engine -> Risk Engine -> Output

This package has NO dependency on Discord, TradingView, a broker, Streamlit,
Ollama, or IA/LLM. It never computes a dollar amount for an instrument unless
ContractRegistry.require_verified() succeeds — that guard is never bypassed.
"""
from __future__ import annotations

from .engine import RiskEngine
from .models import (
    AccountState, RiskDecision, RiskOutcome, RiskReasonCode,
)

__all__ = [
    "RiskEngine", "AccountState", "RiskDecision", "RiskOutcome", "RiskReasonCode",
]
