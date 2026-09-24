"""Liquidity Sweep setup detector (independent, deterministic, no-lookahead).

Definition (single-bar sweep + reclaim, confirmed on bar N):
  A *valid liquidity level* is one detected by Market Analysis and formed
  strictly BEFORE bar N (equal highs = buy-side / BULLISH; equal lows = sell-side
  / BEARISH).
  A *sweep* on bar N is a wick that pierces the level and a close back on the
  original side (rejection):
    - sell-side level L (BEARISH): low[N] < L and close[N] > L  => BULLISH bias
    - buy-side level H (BULLISH):  high[N] > H and close[N] < H  => BEARISH bias
  Everything needed to declare the candidate is available at bar N: the
  pre-existing level and bar N's own OHLC. No future bars are used.
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import LiquiditySweepParams
from ..data.context import AnalysisContext
from ..data.contracts import ContractRegistry
from ..data.models import CLOSE, HIGH, LOW, OPEN, SESSION
from ..features.analyzer import MarketAnalysis
from ..features.market_structure.models import Direction, LiquidityLevel
from ._helpers import build_targets, horizon, view_for
from .base import SetupDetector
from .models import LevelRef, SetupCandidate, SetupStatus, risk_reward


class LiquiditySweepDetector(SetupDetector):
    name = "liquidity_sweep"

    def __init__(self, params: LiquiditySweepParams, registry: ContractRegistry):
        self.p = params
        self.registry = registry

    def detect(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> list[SetupCandidate]:
        tf = self.p.timeframe
        bars = view_for(ctx, tf).bars
        if len(bars) < 2:
            return []
        ta = analysis.by_timeframe[tf]
        tick = self.registry.spec(ctx.symbol).tick_size
        buf = self.p.stop_buffer_ticks * tick

        N = bars.index[-1]
        row = bars.iloc[-1]
        h, l, c = float(row[HIGH]), float(row[LOW]), float(row[CLOSE])
        out: list[SetupCandidate] = []

        # One bar's wick can pierce several stacked levels at once. They all
        # describe the SAME physical sweep and yield an identical trade plan
        # (entry=close, stop=the wick extreme), so emitting one candidate per
        # level produced pure duplicates — and, when both sides were pierced,
        # a self-blocking LONG+SHORT pair. Keep the deepest level per side:
        # the furthest-pierced level is the one the move actually ran.
        swept_bull: LiquidityLevel | None = None   # sell-side swept -> BULLISH
        swept_bear: LiquidityLevel | None = None   # buy-side swept -> BEARISH
        for lvl in ta.liquidity_levels:
            if lvl.timestamp >= N:            # level must pre-exist bar N
                continue
            # A level first pierced BEFORE N has already had its stops taken;
            # wicking through it again is not a sweep of fresh liquidity.
            # swept_at == N is the sweep we are looking at right now.
            if lvl.swept_at is not None and lvl.swept_at < N:
                continue
            L = lvl.level
            if lvl.direction is Direction.BEARISH and l < L and c > L:
                if swept_bull is None or L > swept_bull.level:
                    swept_bull = lvl
            elif lvl.direction is Direction.BULLISH and h > L and c < L:
                if swept_bear is None or L < swept_bear.level:
                    swept_bear = lvl

        if swept_bull is not None:
            out.append(self._make(ctx, tf, N, Direction.BULLISH, entry=c,
                                  stop=l - buf, swept=swept_bull, ta=ta))
        if swept_bear is not None:
            out.append(self._make(ctx, tf, N, Direction.BEARISH, entry=c,
                                  stop=h + buf, swept=swept_bear, ta=ta))
        return out

    def _make(self, ctx, tf, N, direction, entry, stop, swept, ta) -> SetupCandidate:
        bullish = direction is Direction.BULLISH
        # optional level-based target: nearest opposing liquidity beyond entry
        extra = []
        if self.p.use_opposing_liquidity_target:
            opp_dir = Direction.BULLISH if bullish else Direction.BEARISH
            opposing = [x.level for x in ta.liquidity_levels
                        if x.direction is opp_dir
                        and ((bullish and x.level > entry) or (not bullish and x.level < entry))]
            if opposing:
                extra.append(min(opposing) if bullish else max(opposing))
        targets = build_targets(entry, stop, self.p.rr_targets, bullish, extra)
        rr = risk_reward(entry, stop, targets)

        side = "sell-side" if swept.direction is Direction.BEARISH else "buy-side"
        reasons = [
            f"Swept {side} liquidity at {swept.level:.2f} (formed {swept.timestamp:%m-%d %H:%M})",
            f"{'Low' if bullish else 'High'} pierced the level and price closed back "
            f"{'above' if bullish else 'below'} it (rejection)",
        ]
        refs = [LevelRef("liquidity", swept.timestamp, swept.level, f"{side} swept")]
        for e in extra:
            refs.append(LevelRef("opposing_liquidity", None, e, "target"))

        return SetupCandidate(
            setup_name=self.name, instrument=ctx.symbol, direction=direction,
            timestamp=N, timeframe=tf, entry=entry, stop_loss=stop,
            take_profits=targets, risk_reward=rr, reasons=reasons,
            invalidation_conditions=[
                f"Close beyond the sweep extreme ({stop:.2f}) invalidates the reclaim"],
            invalidation_price=stop,
            valid_until=horizon(N, tf, self.p.expiration_bars),
            market_context={"session": ctx.session,
                            "swept_level": swept.level,
                            "swept_direction": swept.direction.value},
            referenced=refs,
            status=SetupStatus.VALID,
        )
