"""Setup Engine package (Phase 5): independent, deterministic setup detectors."""
from __future__ import annotations

from .base import SetupDetector
from .engine import SetupEngine
from .liquidity_sweep import LiquiditySweepDetector
from .models import LevelRef, SetupCandidate, SetupStatus, risk_reward, update_status
from .orb import OrbDetector
from .vwap_reclaim import VwapReclaimDetector

__all__ = [
    "SetupDetector", "SetupEngine",
    "LiquiditySweepDetector", "VwapReclaimDetector", "OrbDetector",
    "SetupCandidate", "SetupStatus", "LevelRef", "risk_reward", "update_status",
]
