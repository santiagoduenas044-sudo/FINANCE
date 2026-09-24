"""Zone Retrace setup detector — first retrace into a fresh fair-value gap,
confirmed by a reaction candle (deterministic, no-lookahead).

The idea, in the operator's words: mark the zone, warn that price is coming
back to it, and only take the trade if price REACTS there. If price cuts
through the zone without reacting, the idea is cancelled.

Definitions (bullish; bearish is the mirror image):
  * Zone: a bullish FVG on `zone_timeframe` (low of candle 3 > high of candle
    1), confirmed at the close of candle 3 (time T), with height between
    min_zone_points and max_zone_points. Midpoint = (top + bottom) / 2.
  * Every zone is judged on the execution bars strictly AFTER T:
      FRESH      no bar has reached the zone top yet
      ARMED      FRESH and the last close is within arm_distance_points of it
      TOUCHED    first touch happened, reaction still possible
      CONFIRMED  within confirm_bars of the first touch a bar closed bullish
                 at/above the midpoint, before any bar closed below the zone
      CANCELLED  a bar closed below the zone bottom first (no reaction)
      EXPIRED    confirm_bars passed after the first touch without a reaction
    Only the FIRST touch counts: a zone is used once.
  * A candidate is emitted only on the bar where the zone CONFIRMS (bar N),
    so the trade is known at N's close: entry = close[N], stop = midpoint -
    stop_points, TP1 = tp1_r * risk, final target = nearest untaken swing high
    between min_target_r and max_target_r, else fallback_target_r.

`zone_states()` exposes the same per-zone judgement so the runtime can send the
"get ready" heads-up and the cancellation, without a second implementation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pandas as pd

from ..config.schema import ZoneRetraceParams
from ..data.context import AnalysisContext
from ..data.models import CLOSE, HIGH, LOW, OPEN
from ..features.analyzer import MarketAnalysis
from ..features.market_structure.models import Direction, SwingType
from ._helpers import horizon, view_for
from .base import SetupDetector
from .models import LevelRef, SetupCandidate, SetupStatus, risk_reward

FRESH, ARMED, TOUCHED = "FRESH", "ARMED", "TOUCHED"
CONFIRMED, CANCELLED, EXPIRED = "CONFIRMED", "CANCELLED", "EXPIRED"


@dataclass(frozen=True)
class Zone:
    zone_id: str
    direction: Direction          # BULLISH = buy zone below price
    formed_at: pd.Timestamp
    top: float
    bottom: float
    stop: float                   # midpoint -/+ stop_points

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    @property
    def bullish(self) -> bool:
        return self.direction is Direction.BULLISH


@dataclass(frozen=True)
class ZoneState:
    zone: Zone
    state: str
    at: pd.Timestamp | None       # bar where the state was reached (None = FRESH/ARMED)
    distance: float | None = None  # points from last close to the zone edge (FRESH/ARMED)


def find_zones(bars: pd.DataFrame, symbol: str, p: ZoneRetraceParams) -> list[Zone]:
    """FVGs formed within the last zone_lookback_bars of `bars`."""
    window = bars.iloc[-(p.zone_lookback_bars + 2):]
    h = window[HIGH].to_numpy(); l = window[LOW].to_numpy()
    ts = window.index
    out: list[Zone] = []
    for i in range(2, len(window)):
        if l[i] > h[i - 2]:
            top, bottom, d = float(l[i]), float(h[i - 2]), Direction.BULLISH
        elif h[i] < l[i - 2]:
            top, bottom, d = float(l[i - 2]), float(h[i]), Direction.BEARISH
        else:
            continue
        if not p.min_zone_points <= top - bottom <= p.max_zone_points:
            continue
        raw = f"{symbol}|{d.value}|{ts[i].isoformat()}|{top:.2f}|{bottom:.2f}"
        zid = hashlib.sha256(raw.encode()).hexdigest()[:12]
        mid = (top + bottom) / 2.0
        stop = mid - p.stop_points if d is Direction.BULLISH else mid + p.stop_points
        out.append(Zone(zid, d, ts[i], top, bottom, round(stop, 4)))
    return out


def judge_zone(zone: Zone, exec_bars: pd.DataFrame, p: ZoneRetraceParams) -> ZoneState:
    after = exec_bars[exec_bars.index > zone.formed_at]
    if after.empty:
        return ZoneState(zone, FRESH, None, None)
    bull = zone.bullish
    touched = (after[LOW] <= zone.top) if bull else (after[HIGH] >= zone.bottom)
    if not touched.any():
        last = float(after[CLOSE].iloc[-1])
        dist = last - zone.top if bull else zone.bottom - last
        state = ARMED if dist <= p.arm_distance_points else FRESH
        return ZoneState(zone, state, None, round(dist, 2))

    first = int(touched.to_numpy().argmax())
    window = after.iloc[first:first + p.confirm_bars]
    for ts, bar in window.iterrows():
        o, c = float(bar[OPEN]), float(bar[CLOSE])
        if (c < zone.bottom) if bull else (c > zone.top):
            return ZoneState(zone, CANCELLED, ts)
        if (c > o and c >= zone.mid) if bull else (c < o and c <= zone.mid):
            return ZoneState(zone, CONFIRMED, ts)
    if len(window) >= p.confirm_bars:
        return ZoneState(zone, EXPIRED, window.index[-1])
    return ZoneState(zone, TOUCHED, window.index[0])


class ZoneRetraceDetector(SetupDetector):
    name = "zone_retrace"

    def __init__(self, params: ZoneRetraceParams):
        self.p = params

    def zone_states(self, ctx: AnalysisContext) -> list[ZoneState]:
        p = self.p.for_symbol(ctx.symbol)
        zbars = view_for(ctx, p.zone_timeframe).bars
        ebars = view_for(ctx, p.timeframe).bars
        if len(zbars) < 3 or ebars.empty:
            return []
        return [judge_zone(z, ebars, p) for z in find_zones(zbars, ctx.symbol, p)]

    def detect(self, ctx: AnalysisContext, analysis: MarketAnalysis) -> list[SetupCandidate]:
        ebars = view_for(ctx, self.p.timeframe).bars
        if ebars.empty:
            return []
        N = ebars.index[-1]
        close = float(ebars[CLOSE].iloc[-1])
        best: dict[Direction, Zone] = {}
        for st in self.zone_states(ctx):
            if st.state != CONFIRMED or st.at != N:
                continue
            z = st.zone
            cur = best.get(z.direction)
            # Several stacked gaps can confirm on one bar: trade the one price
            # is actually reacting from (closest to the close).
            if cur is None or abs(close - z.mid) < abs(close - cur.mid):
                best[z.direction] = z
        out = []
        for z in best.values():
            c = self._make(ctx, analysis, N, close, z)
            if c is not None:
                out.append(c)
        return out

    # ------------------------------------------------------------------ #
    def _make(self, ctx, analysis, N, entry: float, z: Zone) -> SetupCandidate | None:
        p = self.p.for_symbol(ctx.symbol)
        bull = z.bullish
        stop = z.stop
        risk = abs(entry - stop)
        if risk <= 0 or risk > p.max_risk_points:
            return None
        tp1 = entry + p.tp1_r * risk if bull else entry - p.tp1_r * risk
        final, basis = self._final_target(ctx, analysis, entry, risk, bull)
        tps = [tp1, final]
        direction = z.direction
        side = "compra" if bull else "venta"
        return SetupCandidate(
            setup_name=self.name, instrument=ctx.symbol, direction=direction,
            timestamp=N, timeframe=p.timeframe, entry=entry, stop_loss=stop,
            take_profits=tps, risk_reward=risk_reward(entry, stop, tps),
            reasons=[
                f"Zona de {side} {z.bottom:.2f}–{z.top:.2f} (FVG {p.zone_timeframe} "
                f"formado {z.formed_at:%H:%M}), primer regreso del precio",
                f"Reacción confirmada: vela {'alcista' if bull else 'bajista'} que "
                f"cerró {'arriba' if bull else 'abajo'} de la mitad ({z.mid:.2f})",
                f"Objetivo final: {basis}",
            ],
            invalidation_conditions=[f"Stop {stop:.2f}: {p.stop_points:g} pts "
                                     f"{'bajo' if bull else 'sobre'} la mitad de la zona"],
            invalidation_price=stop,
            valid_until=horizon(N, p.timeframe, p.expiration_bars),
            market_context={"session": ctx.session, "zone_id": z.zone_id,
                            "zone_top": z.top, "zone_bottom": z.bottom,
                            "zone_mid": round(z.mid, 2),
                            "target_basis": basis},
            referenced=[LevelRef("zone", z.formed_at, z.mid,
                                 f"{z.bottom:.2f}-{z.top:.2f}")],
            status=SetupStatus.VALID,
        )

    def _final_target(self, ctx, analysis, entry, risk, bull) -> tuple[float, str]:
        """Nearest swing the market has not taken yet, if it pays between
        min_target_r and max_target_r; otherwise fallback_target_r."""
        p = self.p
        lo = entry + p.min_target_r * risk if bull else entry - p.max_target_r * risk
        hi = entry + p.max_target_r * risk if bull else entry - p.min_target_r * risk
        levels: list[float] = []
        for tf in (ctx.structure.timeframe, ctx.htf.timeframe):
            ta = analysis.by_timeframe.get(tf)
            if ta is None:
                continue
            bars = view_for(ctx, tf).bars
            want = SwingType.HIGH if bull else SwingType.LOW
            for s in ta.swings:
                if s.type is not want or not lo <= s.price <= hi:
                    continue
                later = bars[bars.index > s.timestamp]
                taken = (later[HIGH] > s.price).any() if bull else (later[LOW] < s.price).any()
                if not taken:
                    levels.append(float(s.price))
        if levels:
            lvl = min(levels) if bull else max(levels)
            return lvl, f"{'máximo' if bull else 'mínimo'} sin tomar en {lvl:,.2f}"
        fb = entry + p.fallback_target_r * risk if bull else entry - p.fallback_target_r * risk
        return fb, f"{p.fallback_target_r:g}R (sin estructura clara en rango)"
