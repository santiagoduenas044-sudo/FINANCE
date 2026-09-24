"""VWAP Reclaim setup detector (independent, deterministic, no-lookahead).

Uses the session-anchored VWAP from Market Analysis (Phase 4). Definition
(single-bar cross, confirmed on bar N, within the same session):
  - loss of VWAP then reclaim (BULLISH): the prior bar closed at/below VWAP and
    bar N closes ABOVE VWAP  => BULLISH bias
  - loss of VWAP from above (BEARISH): the prior bar closed at/above VWAP and
    bar N closes BELOW VWAP  => BEARISH bias
The previous bar must belong to the same trading_date + session as N (VWAP is
session-anchored), so the reclaim never compares across sessions.
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import VwapReclaimParams
from ..data.context import AnalysisContext
from ..data.contracts import ContractRegistry
from ..data.models import CLOSE, HIGH, LOW, SESSION, TRADING_DATE
from ..features.analyzer import MarketAnalysis
from ..features.market_structure.models import Direction
from ._helpers import build_targets, horizon, view_for
from .base import SetupDetector
from .models import LevelRef, SetupCandidate, SetupStatus, risk_reward


class VwapReclaimDetector(SetupDetector):
    name = "vwap_reclaim"

    def __init__(self, params: VwapReclaimParams, registry: ContractRegistry):
        self.p = params
        self.registry = registry

    def detect(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> list[SetupCandidate]:
        tf = self.p.timeframe
        bars = view_for(ctx, tf).bars
        if len(bars) < 2:
            return []
        ta = analysis.by_timeframe[tf]
        vwap = ta.indicators.get("vwap")
        if vwap is None:
            return []

        N = bars.index[-1]
        # previous bar must be same session + trading_date (session-anchored VWAP)
        if SESSION in bars.columns and TRADING_DATE in bars.columns:
            if not (bars[SESSION].iloc[-1] == bars[SESSION].iloc[-2]
                    and bars[TRADING_DATE].iloc[-1] == bars[TRADING_DATE].iloc[-2]):
                return []

        # positional access (vwap is aligned to bars) avoids label ambiguity
        v_now, v_prev = float(vwap.iloc[-1]), float(vwap.iloc[-2])
        if pd.isna(v_now) or pd.isna(v_prev):
            return []
        c_now = float(bars[CLOSE].iloc[-1])
        c_prev = float(bars[CLOSE].iloc[-2])
        tick = self.registry.spec(ctx.symbol).tick_size
        buf = self.p.stop_buffer_ticks * tick

        # require the price to have genuinely been below VWAP in the lookback window
        window = bars.iloc[-(self.p.lookback_bars + 1):-1]
        was_below = bool((window[CLOSE] < vwap.reindex(window.index)).any())
        was_above = bool((window[CLOSE] > vwap.reindex(window.index)).any())

        if c_prev <= v_prev and c_now > v_now and was_below:
            return [self._make(ctx, tf, N, Direction.BULLISH, c_now, v_now, buf, ta, bars)]
        if c_prev >= v_prev and c_now < v_now and was_above:
            return [self._make(ctx, tf, N, Direction.BEARISH, c_now, v_now, buf, ta, bars)]
        return []

    def _dip_extreme(self, bars: pd.DataFrame, vwap: pd.Series, bullish: bool) -> float:
        """Lowest low (bullish) / highest high (bearish) of the reclaim bar and
        the unbroken run of bars before it that closed on the far side of VWAP,
        within the current session. That run IS the dip being reclaimed: a new
        extreme beyond it means the reclaim failed."""
        key = None
        if SESSION in bars.columns and TRADING_DATE in bars.columns:
            key = (bars[SESSION].iloc[-1], bars[TRADING_DATE].iloc[-1])
        col = LOW if bullish else HIGH
        ext = float(bars[col].iloc[-1])
        n = len(bars)
        for i in range(n - 2, max(-1, n - 2 - self.p.lookback_bars), -1):
            if key and (bars[SESSION].iloc[i], bars[TRADING_DATE].iloc[i]) != key:
                break
            c, v = float(bars[CLOSE].iloc[i]), float(vwap.iloc[i])
            if (bullish and c > v) or (not bullish and c < v):
                break
            x = float(bars[col].iloc[i])
            ext = min(ext, x) if bullish else max(ext, x)
        return ext

    def _make(self, ctx, tf, N, direction, entry, vwap_now, buf, ta, bars) -> SetupCandidate:
        bullish = direction is Direction.BULLISH
        # Previously the stop went to the most recent swing anywhere in the
        # multi-day buffer — often a prior session's level, 7+ ATR away (the
        # ~150-pt NQ stop), or a swing sitting INSIDE the dip. The VWAP is
        # session-anchored, so the reclaim's invalidation is this session's dip.
        dip = self._dip_extreme(bars, ta.indicators["vwap"], bullish)
        stop = dip - buf if bullish else dip + buf

        targets = build_targets(entry, stop, self.p.rr_targets, bullish)
        rr = risk_reward(entry, stop, targets)
        reasons = [
            f"Price {'reclaimed' if bullish else 'lost'} session VWAP ({vwap_now:.2f})",
            f"Prior bar closed {'below/at' if bullish else 'above/at'} VWAP; "
            f"bar N closed {'above' if bullish else 'below'}",
        ]
        refs = [LevelRef("vwap", N, float(vwap_now), "session-anchored VWAP"),
                LevelRef("vwap_dip", None, dip, "dip extreme (stop anchor)")]

        return SetupCandidate(
            setup_name=self.name, instrument=ctx.symbol, direction=direction,
            timestamp=N, timeframe=tf, entry=entry, stop_loss=stop,
            take_profits=targets, risk_reward=rr, reasons=reasons,
            invalidation_conditions=[
                f"Close back {'below' if bullish else 'above'} VWAP, or price beyond {stop:.2f}"],
            invalidation_price=stop,
            valid_until=horizon(N, tf, self.p.expiration_bars),
            market_context={"session": ctx.session, "vwap": float(vwap_now)},
            referenced=refs, status=SetupStatus.VALID,
        )
