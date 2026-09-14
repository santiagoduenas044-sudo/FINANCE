"""MarketAnalyzer: the Market Analysis facade.

Given an as-of view of bars (a TimeframeView from AnalysisContext) it computes
the indicator set and the market-structure objects, returning a single
``TimeframeAnalysis`` domain object for the Setup Engine to consume later.

This layer contains NO strategy, NO signals, NO risk, NO IA. It only produces
reliable, no-lookahead analysis tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..data.context import AnalysisContext, TimeframeView
from . import indicators as ind
from .market_structure import MarketStructureProvider, get_structure_provider
from .market_structure.models import (
    FairValueGap, LiquidityLevel, OrderBlock, StructureEvent, SwingPoint,
)


@dataclass(frozen=True)
class TimeframeAnalysis:
    timeframe: str
    decision_time: pd.Timestamp
    indicators: pd.DataFrame            # aligned to the view's bars
    swings: list[SwingPoint]
    structure_events: list[StructureEvent]
    fair_value_gaps: list[FairValueGap]
    liquidity_levels: list[LiquidityLevel]
    order_blocks: list[OrderBlock] = field(default_factory=list)


@dataclass(frozen=True)
class MarketAnalysis:
    symbol: str
    decision_time: pd.Timestamp
    by_timeframe: dict[str, TimeframeAnalysis]


class MarketAnalyzer:
    def __init__(self, provider: MarketStructureProvider | None = None,
                 backend: str = "native", provider_kwargs: dict | None = None,
                 with_order_blocks: bool = True):
        self.provider = provider or get_structure_provider(backend, **(provider_kwargs or {}))
        self.with_order_blocks = with_order_blocks

    def analyze_view(self, view: TimeframeView,
                     decision_time: pd.Timestamp) -> TimeframeAnalysis:
        bars = view.bars
        obs = self.provider.order_blocks(bars) if self.with_order_blocks else []
        return TimeframeAnalysis(
            timeframe=view.timeframe,
            decision_time=decision_time,
            indicators=ind.compute_indicators(bars),
            swings=self.provider.swings(bars),
            structure_events=self.provider.structure_events(bars),
            fair_value_gaps=self.provider.fair_value_gaps(bars),
            liquidity_levels=self.provider.liquidity_levels(bars),
            order_blocks=obs,
        )

    def analyze(self, ctx: AnalysisContext) -> MarketAnalysis:
        views = {
            ctx.htf.timeframe: ctx.htf,
            ctx.structure.timeframe: ctx.structure,
            ctx.execution.timeframe: ctx.execution,
        }
        by_tf = {tf: self.analyze_view(v, ctx.decision_time) for tf, v in views.items()}
        return MarketAnalysis(ctx.symbol, ctx.decision_time, by_tf)
