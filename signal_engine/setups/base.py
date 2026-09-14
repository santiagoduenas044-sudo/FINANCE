"""SetupDetector interface. Each setup is an independent, self-contained
detector: it consumes the as-of AnalysisContext + MarketAnalysis and emits zero
or more SetupCandidate for the decision bar N. Setups must NOT depend on one
another.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..data.context import AnalysisContext
from ..features.analyzer import MarketAnalysis
from .models import SetupCandidate


class SetupDetector(ABC):
    name: str

    @abstractmethod
    def detect(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> list[SetupCandidate]:
        """Return candidates confirmed as of ctx.decision_time (bar N).
        Must use only information available at/before N."""
