"""Shared helpers for setup detectors (no cross-setup coupling)."""
from __future__ import annotations

import pandas as pd

from ..data.context import AnalysisContext, TimeframeView
from ..data.models import tf_to_timedelta


def view_for(ctx: AnalysisContext, tf: str) -> TimeframeView:
    for v in (ctx.execution, ctx.structure, ctx.htf):
        if v.timeframe == tf:
            return v
    raise KeyError(f"setup timeframe {tf!r} is not one of the AnalysisContext roles")


def horizon(ts: pd.Timestamp, tf: str, n_bars: int) -> pd.Timestamp:
    return ts + n_bars * tf_to_timedelta(tf)


def build_targets(entry: float, stop: float, rr_list: list[float], bullish: bool,
                  extra_levels: list[float] | None = None) -> list[float]:
    """Targets from R multiples (+ optional level-based targets), all strictly
    beyond entry in the trade direction, sorted nearest-first."""
    risk = abs(entry - stop)
    tps: list[float] = []
    for lv in (extra_levels or []):
        if (bullish and lv > entry) or (not bullish and lv < entry):
            tps.append(float(lv))
    for rr in rr_list:
        tps.append(entry + rr * risk if bullish else entry - rr * risk)
    uniq = sorted({round(t, 6) for t in tps})
    return uniq if bullish else sorted(uniq, reverse=True)
