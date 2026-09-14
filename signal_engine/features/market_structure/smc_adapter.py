"""Adapter over the third-party ``smartmoneyconcepts`` library.

Wraps smc behind our MarketStructureProvider interface and maps its positional,
array-style output to our timestamp-keyed domain models. Consumers never see smc
types, so the library can be swapped for NativeStructureProvider (or anything
else) without changing Setup Engine / Signal Engine / Backtester.

NO-LOOKAHEAD: smc marks provisional swings on the trailing bars (the current
running extreme), which are not yet confirmed. This adapter DROPS the trailing
`swing_length` bars for swing-derived detections and only emits FVGs whose
3-candle formation is complete, so nothing depends on bars beyond the frame.
The frame passed in is always the as-of view (index <= decision bar N).
"""
from __future__ import annotations

import pandas as pd

from ...data.models import CLOSE, HIGH, LOW, OPEN, VOLUME
from ._common import label_swings
from .base import MarketStructureProvider
from .models import (
    Direction, FairValueGap, LiquidityLevel, OrderBlock, StructureEvent,
    StructureType, SwingPoint, SwingType,
)


def _require_smc():
    try:
        from smartmoneyconcepts.smc import smc
        return smc
    except Exception as exc:  # pragma: no cover
        raise ImportError(
            "smartmoneyconcepts is not installed; use NativeStructureProvider "
            "or `pip install smartmoneyconcepts`."
        ) from exc


class SmcStructureProvider(MarketStructureProvider):
    def __init__(self, swing_length: int = 5, liquidity_range_percent: float = 0.01):
        self.swing_length = swing_length
        self.liquidity_range_percent = liquidity_range_percent
        self._smc = _require_smc()

    # smc needs lowercase ohlc and works on positional rows
    @staticmethod
    def _pos_frame(bars: pd.DataFrame) -> pd.DataFrame:
        return bars[[OPEN, HIGH, LOW, CLOSE, VOLUME]].reset_index(drop=True)

    def _raw_swings(self, pos: pd.DataFrame) -> pd.DataFrame:
        return self._smc.swing_highs_lows(pos, swing_length=self.swing_length)

    # ---- swings --------------------------------------------------------- #
    def swings(self, bars: pd.DataFrame) -> list[SwingPoint]:
        ts = bars.index
        n = len(bars)
        pos = self._pos_frame(bars)
        shl = self._raw_swings(pos)
        hl = shl["HighLow"].to_numpy()
        lvl = shl["Level"].to_numpy()
        confirmed_cut = n - self.swing_length     # drop trailing unconfirmed
        out: list[SwingPoint] = []
        for i in range(n):
            if pd.isna(hl[i]) or i >= confirmed_cut:
                continue
            typ = SwingType.HIGH if hl[i] == 1 else SwingType.LOW
            conf_pos = min(i + self.swing_length, n - 1)
            out.append(SwingPoint(ts[i], float(lvl[i]), typ, ts[conf_pos]))
        return label_swings(out)

    # ---- BOS / CHoCH ---------------------------------------------------- #
    def structure_events(self, bars: pd.DataFrame) -> list[StructureEvent]:
        ts = bars.index
        n = len(bars)
        pos = self._pos_frame(bars)
        shl = self._raw_swings(pos)
        bc = self._smc.bos_choch(pos, shl, close_break=True)
        bos = bc["BOS"].to_numpy(); choch = bc["CHOCH"].to_numpy()
        level = bc["Level"].to_numpy(); broken = bc["BrokenIndex"].to_numpy()
        out: list[StructureEvent] = []
        for i in range(n):
            is_bos = not pd.isna(bos[i])
            is_choch = not pd.isna(choch[i])
            if not (is_bos or is_choch):
                continue
            if pd.isna(broken[i]):            # not yet broken => not confirmed
                continue
            sign = bos[i] if is_bos else choch[i]
            direction = Direction.BULLISH if sign == 1 else Direction.BEARISH
            typ = StructureType.BOS if is_bos else StructureType.CHOCH
            bpos = int(broken[i])
            if bpos >= n:
                continue
            out.append(StructureEvent(ts[bpos], typ, direction, float(level[i]), ts[i]))
        return sorted(out, key=lambda e: e.timestamp)

    # ---- FVG ------------------------------------------------------------ #
    def fair_value_gaps(self, bars: pd.DataFrame) -> list[FairValueGap]:
        ts = bars.index
        n = len(bars)
        pos = self._pos_frame(bars)
        f = self._smc.fvg(pos)
        fv = f["FVG"].to_numpy(); top = f["Top"].to_numpy()
        bottom = f["Bottom"].to_numpy(); mit = f["MitigatedIndex"].to_numpy()
        out: list[FairValueGap] = []
        for i in range(n):
            if pd.isna(fv[i]):
                continue
            if i > n - 3:                    # 3-candle formation not complete
                continue
            direction = Direction.BULLISH if fv[i] == 1 else Direction.BEARISH
            mit_at = None
            if not pd.isna(mit[i]) and int(mit[i]) > 0 and int(mit[i]) < n:
                mit_at = ts[int(mit[i])]
            out.append(FairValueGap(ts[i], float(top[i]), float(bottom[i]), direction, mit_at))
        return out

    # ---- liquidity ------------------------------------------------------ #
    def liquidity_levels(self, bars: pd.DataFrame) -> list[LiquidityLevel]:
        ts = bars.index
        n = len(bars)
        pos = self._pos_frame(bars)
        shl = self._raw_swings(pos)
        lq = self._smc.liquidity(pos, shl, range_percent=self.liquidity_range_percent)
        liq = lq["Liquidity"].to_numpy(); level = lq["Level"].to_numpy()
        swept = lq["Swept"].to_numpy()
        confirmed_cut = n - self.swing_length
        out: list[LiquidityLevel] = []
        for i in range(n):
            if pd.isna(liq[i]) or i >= confirmed_cut:
                continue
            direction = Direction.BULLISH if liq[i] == 1 else Direction.BEARISH
            swept_at = None
            if not pd.isna(swept[i]) and int(swept[i]) > 0 and int(swept[i]) < n:
                swept_at = ts[int(swept[i])]
            out.append(LiquidityLevel(ts[i], float(level[i]), direction, swept_at))
        return out

    # ---- order blocks --------------------------------------------------- #
    def order_blocks(self, bars: pd.DataFrame) -> list[OrderBlock]:
        ts = bars.index
        n = len(bars)
        pos = self._pos_frame(bars)
        shl = self._raw_swings(pos)
        ob = self._smc.ob(pos, shl)
        obv = ob["OB"].to_numpy(); top = ob["Top"].to_numpy()
        bottom = ob["Bottom"].to_numpy(); vol = ob["OBVolume"].to_numpy()
        mit = ob["MitigatedIndex"].to_numpy()
        confirmed_cut = n - self.swing_length
        out: list[OrderBlock] = []
        for i in range(n):
            if pd.isna(obv[i]) or i >= confirmed_cut:
                continue
            direction = Direction.BULLISH if obv[i] == 1 else Direction.BEARISH
            mit_at = None
            if not pd.isna(mit[i]) and int(mit[i]) > 0 and int(mit[i]) < n:
                mit_at = ts[int(mit[i])]
            v = None if pd.isna(vol[i]) else float(vol[i])
            out.append(OrderBlock(ts[i], float(top[i]), float(bottom[i]), direction, v, mit_at))
        return out
