"""AnalysisContext: the immutable, as-of-bar-N view handed to the rest of the
pipeline. Building it here (and only here) is what guarantees no-lookahead:
every timeframe view is sliced to bars whose CLOSE is <= decision_time.

NOTE: this phase builds ONLY the data structure. No indicators, market
structure, setups, signals, or IA are computed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..config.schema import TimeframeRoles
from .models import OHLCV


@dataclass(frozen=True)
class TimeframeView:
    timeframe: str
    bars: pd.DataFrame          # CLOSE-labeled, index <= decision_time

    def last_closed(self) -> pd.Series | None:
        return None if self.bars.empty else self.bars.iloc[-1]

    def lookback(self, n: int) -> pd.DataFrame:
        return self.bars.tail(n)


@dataclass(frozen=True)
class AnalysisContext:
    symbol: str
    decision_time: pd.Timestamp
    session: str
    htf: TimeframeView          # context  (e.g. 1H)
    structure: TimeframeView    # structure (e.g. 15m)
    execution: TimeframeView    # entry     (e.g. 5m)


def _slice_asof(df: pd.DataFrame, decision_time: pd.Timestamp) -> pd.DataFrame:
    """Keep only bars whose CLOSE label is <= decision_time (fully closed)."""
    return df[df.index <= decision_time]


def build_analysis_context(
    symbol: str,
    decision_time: pd.Timestamp,
    tf_bars: dict[str, pd.DataFrame],
    roles: TimeframeRoles,
    session: str,
) -> AnalysisContext:
    """Assemble an as-of context from resampled timeframe frames.

    ``tf_bars`` maps timeframe string -> CLOSE-labeled OHLCV(+annotation) frame.
    """
    def view(tf: str) -> TimeframeView:
        df = tf_bars.get(tf)
        if df is None:
            raise KeyError(f"missing timeframe {tf!r} for AnalysisContext")
        return TimeframeView(tf, _slice_asof(df, decision_time))

    return AnalysisContext(
        symbol=symbol,
        decision_time=decision_time,
        session=session,
        htf=view(roles.context),
        structure=view(roles.structure),
        execution=view(roles.execution),
    )
