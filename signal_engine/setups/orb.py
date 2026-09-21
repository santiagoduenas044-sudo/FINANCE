"""Opening Range Breakout (ORB) setup detector.

MUY IMPORTANTE: a breakout can NEVER be detected before the opening range is
fully formed. The OR window is [session_open, session_open + opening_range_minutes].
The decision bar N must be strictly AFTER the OR window end; otherwise no
candidate is produced (even if price has exceeded a partial range). The OR
high/low are computed only from bars whose close falls inside the completed
window (all <= OR end <= N), so they never depend on future bars.

Definition (crossing bar, confirmed on bar N):
  - bullish: close[N] > OR_high and the previous bar closed inside the range
  - bearish: close[N] < OR_low  and the previous bar closed inside the range
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import OrbParams
from ..data.context import AnalysisContext
from ..data.models import CLOSE, HIGH, LOW, SESSION, TRADING_DATE
from ..data.sessions import SessionClassifier
from ..features.analyzer import MarketAnalysis
from ..features.market_structure.models import Direction
from ._helpers import build_targets, horizon, view_for
from .base import SetupDetector
from .models import LevelRef, SetupCandidate, SetupStatus, risk_reward


class OrbDetector(SetupDetector):
    name = "orb"

    def __init__(self, params: OrbParams, classifier: SessionClassifier, tz: str):
        self.p = params
        self.classifier = classifier
        self.tz = tz

    def detect(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> list[SetupCandidate]:
        tf = self.p.timeframe
        bars = view_for(ctx, tf).bars
        if len(bars) < 2 or SESSION not in bars.columns:
            return []

        N = bars.index[-1]
        if bars[SESSION].iloc[-1] != self.p.session:
            return []
        tdate = bars[TRADING_DATE].iloc[-1]
        open_dt = self.classifier.session_open_dt(tdate, self.p.session, self.tz)
        or_end = open_dt + pd.Timedelta(minutes=self.p.opening_range_minutes)

        # OR must be fully formed AND N must be a bar strictly after the OR window
        if N <= or_end:
            return []

        or_bars = bars[(bars.index > open_dt) & (bars.index <= or_end)
                       & (bars[SESSION] == self.p.session)
                       & (bars[TRADING_DATE] == tdate)]
        if or_bars.empty:
            return []
        or_high = float(or_bars[HIGH].max())
        or_low = float(or_bars[LOW].min())
        or_range = or_high - or_low
        if or_range <= 0:
            return []

        c_now = float(bars[CLOSE].iloc[-1])
        c_prev = float(bars[CLOSE].iloc[-2])
        prev_ts = bars.index[-2]
        # previous bar must be in the same session/day and inside the range (crossing)
        same_prev = (bars[SESSION].iloc[-2] == self.p.session
                     and bars[TRADING_DATE].iloc[-2] == tdate)

        if c_now > or_high and c_prev <= or_high and same_prev:
            return [self._make(ctx, tf, N, Direction.BULLISH, c_now, or_high, or_low, or_range)]
        if c_now < or_low and c_prev >= or_low and same_prev:
            return [self._make(ctx, tf, N, Direction.BEARISH, c_now, or_high, or_low, or_range)]
        return []

    def _make(self, ctx, tf, N, direction, entry, or_high, or_low, or_range) -> SetupCandidate:
        bullish = direction is Direction.BULLISH
        stop = or_low if bullish else or_high            # opposite OR boundary
        targets = build_targets(entry, stop, self.p.rr_targets, bullish)
        rr = risk_reward(entry, stop, targets)
        reasons = [
            f"Opening range [{or_low:.2f}, {or_high:.2f}] fully formed",
            f"Price closed {'above' if bullish else 'below'} the OR "
            f"{'high' if bullish else 'low'} (breakout)",
        ]
        refs = [LevelRef("opening_range_high", None, or_high, "OR high"),
                LevelRef("opening_range_low", None, or_low, "OR low")]
        return SetupCandidate(
            setup_name=self.name, instrument=ctx.symbol, direction=direction,
            timestamp=N, timeframe=tf, entry=entry, stop_loss=stop,
            take_profits=targets, risk_reward=rr, reasons=reasons,
            invalidation_conditions=[
                f"Close back inside the opening range (beyond {stop:.2f})"],
            invalidation_price=stop,
            valid_until=horizon(N, tf, self.p.expiration_bars),
            market_context={"session": ctx.session, "or_high": or_high,
                            "or_low": or_low, "or_range": or_range},
            referenced=refs, status=SetupStatus.VALID,
        )
