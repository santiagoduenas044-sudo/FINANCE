"""Native market-structure backend (our own implementation).

Fully controlled and no-lookahead by construction: every detection uses only
bars within its confirmation window, and trailing not-yet-confirmed detections
are excluded. Serves both as a production backend and as a reference oracle for
testing the smc adapter.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ...data.models import CLOSE, HIGH, LOW, OPEN
from ._common import label_swings
from .base import MarketStructureProvider
from .models import (
    Direction, FairValueGap, LiquidityLevel, OrderBlock, StructureEvent,
    StructureType, SwingPoint, SwingType,
)


class NativeStructureProvider(MarketStructureProvider):
    def __init__(self, n_left: int = 3, n_right: int = 3,
                 fvg_range_percent: float = 0.0005,
                 liquidity_tolerance: float = 0.0005):
        self.n_left = n_left
        self.n_right = n_right
        self.fvg_range_percent = fvg_range_percent
        self.liquidity_tolerance = liquidity_tolerance

    # ---- swings --------------------------------------------------------- #
    def swings(self, bars: pd.DataFrame) -> list[SwingPoint]:
        h = bars[HIGH].to_numpy()
        l = bars[LOW].to_numpy()
        ts = bars.index
        n = len(bars)
        nl, nr = self.n_left, self.n_right
        out: list[SwingPoint] = []
        for i in range(nl, n - nr):          # need nr bars to the right => confirmed
            if h[i] > h[i - nl:i].max() and h[i] > h[i + 1:i + 1 + nr].max():
                out.append(SwingPoint(ts[i], float(h[i]), SwingType.HIGH, ts[i + nr]))
            if l[i] < l[i - nl:i].min() and l[i] < l[i + 1:i + 1 + nr].min():
                out.append(SwingPoint(ts[i], float(l[i]), SwingType.LOW, ts[i + nr]))
        return label_swings(out)

    # ---- BOS / CHoCH ---------------------------------------------------- #
    def structure_events(self, bars: pd.DataFrame) -> list[StructureEvent]:
        swings = self.swings(bars)
        ts = bars.index
        close = bars[CLOSE].to_numpy()
        n = len(bars)
        pos_of = {t: i for i, t in enumerate(ts)}

        highs = sorted(((s.confirmed_at, s.timestamp, s.price)
                        for s in swings if s.type is SwingType.HIGH),
                       key=lambda x: pos_of[x[0]])
        lows = sorted(((s.confirmed_at, s.timestamp, s.price)
                       for s in swings if s.type is SwingType.LOW),
                      key=lambda x: pos_of[x[0]])

        events: list[StructureEvent] = []
        trend: Direction | None = None
        last_high = last_low = None
        hi = lo = 0
        broken_high_ts = broken_low_ts = None
        for i in range(n):
            while hi < len(highs) and pos_of[highs[hi][0]] <= i:
                last_high = highs[hi]; hi += 1
            while lo < len(lows) and pos_of[lows[lo][0]] <= i:
                last_low = lows[lo]; lo += 1
            c = close[i]
            if last_high is not None and last_high[1] != broken_high_ts and c > last_high[2]:
                typ = StructureType.CHOCH if trend is Direction.BEARISH else StructureType.BOS
                events.append(StructureEvent(ts[i], typ, Direction.BULLISH,
                                             last_high[2], last_high[1]))
                trend = Direction.BULLISH
                broken_high_ts = last_high[1]
            if last_low is not None and last_low[1] != broken_low_ts and c < last_low[2]:
                typ = StructureType.CHOCH if trend is Direction.BULLISH else StructureType.BOS
                events.append(StructureEvent(ts[i], typ, Direction.BEARISH,
                                             last_low[2], last_low[1]))
                trend = Direction.BEARISH
                broken_low_ts = last_low[1]
        return events

    # ---- FVG ------------------------------------------------------------ #
    def fair_value_gaps(self, bars: pd.DataFrame) -> list[FairValueGap]:
        o = bars[OPEN].to_numpy(); h = bars[HIGH].to_numpy()
        l = bars[LOW].to_numpy(); ts = bars.index
        n = len(bars)
        raw = []
        for i in range(1, n - 1):            # middle candle i; confirmed at i+1
            if l[i + 1] > h[i - 1]:          # bullish gap
                raw.append((i + 1, l[i + 1], h[i - 1], Direction.BULLISH))
            elif h[i + 1] < l[i - 1]:        # bearish gap
                raw.append((i + 1, l[i - 1], h[i + 1], Direction.BEARISH))
        out: list[FairValueGap] = []
        for p, top, bottom, direction in raw:
            mitigated_at = None
            for j in range(p + 1, n):
                if direction is Direction.BULLISH and l[j] <= top:
                    mitigated_at = ts[j]; break
                if direction is Direction.BEARISH and h[j] >= bottom:
                    mitigated_at = ts[j]; break
            out.append(FairValueGap(ts[p], float(top), float(bottom), direction, mitigated_at))
        return out

    # ---- liquidity ------------------------------------------------------ #
    def liquidity_levels(self, bars: pd.DataFrame) -> list[LiquidityLevel]:
        swings = self.swings(bars)
        ts = bars.index
        h = bars[HIGH].to_numpy(); l = bars[LOW].to_numpy()
        pos_of = {t: i for i, t in enumerate(ts)}
        n = len(bars)
        out: list[LiquidityLevel] = []

        def clusters(points, direction):
            pts = sorted(points, key=lambda s: pos_of[s.timestamp])
            i = 0
            while i < len(pts) - 1:
                a = pts[i]; b = pts[i + 1]
                if abs(a.price - b.price) <= self.liquidity_tolerance * b.price:
                    level = (a.price + b.price) / 2.0
                    form_pos = pos_of[b.timestamp]
                    swept_at = None
                    for j in range(form_pos + 1, n):
                        if direction is Direction.BULLISH and h[j] > level:
                            swept_at = ts[j]; break
                        if direction is Direction.BEARISH and l[j] < level:
                            swept_at = ts[j]; break
                    out.append(LiquidityLevel(b.timestamp, float(level), direction, swept_at))
                i += 1

        clusters([s for s in swings if s.type is SwingType.HIGH], Direction.BULLISH)
        clusters([s for s in swings if s.type is SwingType.LOW], Direction.BEARISH)
        return sorted(out, key=lambda x: pos_of[x.timestamp])

    # ---- order blocks --------------------------------------------------- #
    def order_blocks(self, bars: pd.DataFrame) -> list[OrderBlock]:
        """Simple structure-anchored OB: the last opposite-color candle before a
        structure break in the break's direction."""
        events = self.structure_events(bars)
        o = bars[OPEN].to_numpy(); h = bars[HIGH].to_numpy()
        l = bars[LOW].to_numpy(); c = bars[CLOSE].to_numpy()
        ts = bars.index
        pos_of = {t: i for i, t in enumerate(ts)}
        n = len(bars)
        out: list[OrderBlock] = []
        for ev in events:
            b = pos_of[ev.timestamp]
            if ev.direction is Direction.BULLISH:
                j = next((k for k in range(b, -1, -1) if c[k] < o[k]), None)
                if j is None:
                    continue
                top, bottom = float(h[j]), float(l[j])
                mit = next((ts[k] for k in range(b + 1, n) if l[k] <= top), None)
            else:
                j = next((k for k in range(b, -1, -1) if c[k] > o[k]), None)
                if j is None:
                    continue
                top, bottom = float(h[j]), float(l[j])
                mit = next((ts[k] for k in range(b + 1, n) if h[k] >= bottom), None)
            out.append(OrderBlock(ts[j], top, bottom, ev.direction, None, mit))
        return out
