"""SetupEngine: runs the enabled, independent setup detectors against an as-of
AnalysisContext and returns SetupCandidates. Setups are toggled individually via
config and never depend on each other.

This is NOT the Signal Engine: it does not rank, score, or convert candidates
into LONG/SHORT signals.
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import EngineConfig
from ..data.context import AnalysisContext
from ..data.contracts import ContractRegistry
from ..data.engine import DataEngine
from ..data.sessions import SessionClassifier
from ..features.analyzer import MarketAnalyzer
from .base import SetupDetector
from .liquidity_sweep import LiquiditySweepDetector
from .models import SetupCandidate
from .orb import OrbDetector
from .vwap_reclaim import VwapReclaimDetector


class SetupEngine:
    def __init__(self, config: EngineConfig, registry: ContractRegistry,
                 analyzer: MarketAnalyzer | None = None):
        self.cfg = config
        self.registry = registry
        self.analyzer = analyzer or self._build_analyzer(config)
        self.detectors: list[SetupDetector] = self._build_detectors()

    @staticmethod
    def _build_analyzer(cfg: EngineConfig) -> MarketAnalyzer:
        a = cfg.analysis
        if a.structure_backend == "smc":
            kwargs = {"swing_length": a.smc_swing_length}
        else:
            kwargs = {"n_left": a.native_n_left, "n_right": a.native_n_right}
        return MarketAnalyzer(backend=a.structure_backend, provider_kwargs=kwargs,
                              with_order_blocks=a.with_order_blocks)

    def _build_detectors(self) -> list[SetupDetector]:
        s = self.cfg.setups
        dets: list[SetupDetector] = []
        if s.liquidity_sweep.enabled:
            dets.append(LiquiditySweepDetector(s.liquidity_sweep, self.registry))
        if s.vwap_reclaim.enabled:
            dets.append(VwapReclaimDetector(s.vwap_reclaim, self.registry))
        if s.orb.enabled:
            dets.append(OrbDetector(s.orb, SessionClassifier(self.cfg.sessions),
                                    self.cfg.data.timezone))
        return dets

    @property
    def enabled_setups(self) -> list[str]:
        return [d.name for d in self.detectors]

    def detect(self, ctx: AnalysisContext) -> list[SetupCandidate]:
        analysis = self.analyzer.analyze(ctx)
        out: list[SetupCandidate] = []
        for d in self.detectors:
            out.extend(d.detect(ctx, analysis))
        return out

    # ---- scanning helper (for demos/backtests; deterministic, as-of each N) - #
    def scan(self, engine: DataEngine, symbol: str,
             tf_bars: dict[str, pd.DataFrame],
             decision_times) -> dict[pd.Timestamp, list[SetupCandidate]]:
        """Run detection at each decision time using an as-of context built from
        the (sliced) timeframe frames. Uses only data <= each N."""
        results: dict[pd.Timestamp, list[SetupCandidate]] = {}
        for N in decision_times:
            ctx = engine.analysis_context(symbol, N, tf_bars)
            cands = self.detect(ctx)
            if cands:
                results[N] = cands
        return results
