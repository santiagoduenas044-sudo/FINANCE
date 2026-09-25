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
  * Stop: beyond the origin of the displacement (lowest low of the two candles
    before the gap, minus a buffer), clamped to min/max_stop_points from the
    midpoint.
  * A candidate is emitted only on the bar where the zone CONFIRMS (bar N).
    The plan is anchored at the midpoint (the "optimal" entry): R = |mid -
    stop|, TP1 = nearest untaken swing within [tp1_r, tp1_max_r] R else
    tp1_r, final = nearest untaken swing within [min_target_r, max_target_r]
    R else fallback_target_r. The fill is close[N] — what "take the entry
    now" actually gets — and is skipped if that risks more than
    max_risk_points or is already past TP1.

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
    stop: float                   # beyond the move's origin, clamped from the midpoint

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
        if d is Direction.BULLISH:
            origin = min(l[i - 2], l[i - 1]) - p.stop_buffer_points
        else:
            origin = max(h[i - 2], h[i - 1]) + p.stop_buffer_points
        dist = min(max(abs(mid - origin), p.min_stop_points), p.max_stop_points)
        stop = mid - dist if d is Direction.BULLISH else mid + dist
        out.append(Zone(zid, d, ts[i], top, bottom, round(stop, 4)))
    return out


class _Bars:
    """Execution bars as plain arrays: a zone is judged on every bar for
    every zone, and pandas row access there dominated a backtest."""

    def __init__(self, df: pd.DataFrame):
        self.index = df.index
        self.o = df[OPEN].to_numpy(float)
        self.h = df[HIGH].to_numpy(float)
        self.l = df[LOW].to_numpy(float)
        self.c = df[CLOSE].to_numpy(float)


def judge_zone(zone: Zone, exec_bars, p: ZoneRetraceParams) -> ZoneState:
    b = exec_bars if isinstance(exec_bars, _Bars) else _Bars(exec_bars)
    start = int(b.index.searchsorted(zone.formed_at, side="right"))
    if start >= len(b.index):
        return ZoneState(zone, FRESH, None, None)
    bull = zone.bullish
    touched = (b.l[start:] <= zone.top) if bull else (b.h[start:] >= zone.bottom)
    if not touched.any():
        last = float(b.c[-1])
        dist = last - zone.top if bull else zone.bottom - last
        state = ARMED if dist <= p.arm_distance_points else FRESH
        return ZoneState(zone, state, None, round(dist, 2))

    first = start + int(touched.argmax())
    end = min(first + p.confirm_bars, len(b.index))
    for i in range(first, end):
        o, c = b.o[i], b.c[i]
        if (c < zone.bottom) if bull else (c > zone.top):
            return ZoneState(zone, CANCELLED, b.index[i])
        if (c > o and c >= zone.mid) if bull else (c < o and c <= zone.mid):
            return ZoneState(zone, CONFIRMED, b.index[i])
    if end - first >= p.confirm_bars:
        return ZoneState(zone, EXPIRED, b.index[end - 1])
    return ZoneState(zone, TOUCHED, b.index[first])


class ZoneRetraceDetector(SetupDetector):
    name = "zone_retrace"

    def __init__(self, params: ZoneRetraceParams):
        self.p = params
        self._last: tuple | None = None    # (ctx, states): detector + runtime share it

    def zone_states(self, ctx: AnalysisContext) -> list[ZoneState]:
        if self._last is not None and self._last[0] is ctx:
            return self._last[1]
        p = self.p.for_symbol(ctx.symbol)
        zbars = view_for(ctx, p.zone_timeframe).bars
        ebars = view_for(ctx, p.timeframe).bars
        states = []
        if len(zbars) >= 3 and not ebars.empty:
            arrays = _Bars(ebars)
            states = [judge_zone(z, arrays, p) for z in find_zones(zbars, ctx.symbol, p)]
        self._last = (ctx, states)
        return states

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
    def plan_for(self, ctx, analysis, z: Zone) -> dict:
        """The trade plan for a zone, anchored at its midpoint: what the
        heads-up announces and what a confirmation will trade."""
        p = self.p.for_symbol(ctx.symbol)
        bull = z.bullish
        plan_r = abs(z.mid - z.stop)
        tp1, _ = self._target(ctx, analysis, z.mid, plan_r, bull, p.tp1_r, p.tp1_max_r, p.tp1_r)
        if p.fixed_target_r:
            k = p.fixed_target_r
            final = z.mid + k * plan_r if bull else z.mid - k * plan_r
            basis = f"{k:g}:1 fijo"
        else:
            final, basis = self._target(ctx, analysis, z.mid, plan_r, bull, p.min_target_r,
                                        p.max_target_r, p.fallback_target_r, beyond=tp1)
        if p.single_target:
            tp1 = final
        return {"stop": z.stop, "tp1": tp1, "final": final, "basis": basis,
                "plan_r": plan_r, "tp1_rr": round(abs(tp1 - z.mid) / plan_r, 2),
                "plan_rr": round(abs(final - z.mid) / plan_r, 1),
                # every untaken swing from 1R to 6R, in R from the midpoint:
                # what a different target rule would have had to choose from
                "swing_rs": self._swing_rs(ctx, analysis, z.mid, plan_r, bull),
                "confirm_minutes": p.confirm_bars
                * int(pd.Timedelta(p.timeframe.replace("m", "min")).total_seconds() // 60)}

    def _make(self, ctx, analysis, N, entry: float, z: Zone) -> SetupCandidate | None:
        p = self.p.for_symbol(ctx.symbol)
        bull = z.bullish
        stop = z.stop
        plan = self.plan_for(ctx, analysis, z)
        plan_r, tp1, final, basis = plan["plan_r"], plan["tp1"], plan["final"], plan["basis"]
        risk = abs(entry - stop)
        beyond_tp1 = entry >= tp1 if bull else entry <= tp1
        if risk <= 0 or risk > p.max_risk_points or beyond_tp1 \
                or (entry <= stop if bull else entry >= stop):
            return None
        tps = [final] if p.single_target else [tp1, final]
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
            invalidation_conditions=[f"Stop {stop:.2f}: {plan_r:g} pts "
                                     f"{'bajo' if bull else 'sobre'} la mitad de la zona"],
            invalidation_price=stop,
            valid_until=horizon(N, p.timeframe, p.expiration_bars),
            market_context={"session": ctx.session, "zone_id": z.zone_id,
                            "zone_top": z.top, "zone_bottom": z.bottom,
                            "zone_mid": round(z.mid, 2),
                            "plan_rr": round(abs(final - z.mid) / plan_r, 1),
                            "swing_rs": plan["swing_rs"],
                            "target_basis": basis},
            referenced=[LevelRef("zone", z.formed_at, z.mid,
                                 f"{z.bottom:.2f}-{z.top:.2f}")],
            status=SetupStatus.VALID,
        )

    def _swing_rs(self, ctx, analysis, entry, risk, bull) -> list[float]:
        lo = entry + 1.0 * risk if bull else entry - 6.0 * risk
        hi = entry + 6.0 * risk if bull else entry - 1.0 * risk
        return sorted(round(abs(v - entry) / risk, 3)
                      for v in self._untaken(ctx, analysis, bull, lo, hi, None))

    def _untaken(self, ctx, analysis, bull, lo, hi, beyond) -> list[float]:
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
                if beyond is not None and ((s.price <= beyond) if bull else (s.price >= beyond)):
                    continue
                later = bars[bars.index > s.timestamp]
                taken = (later[HIGH] > s.price).any() if bull else (later[LOW] < s.price).any()
                if not taken:
                    levels.append(float(s.price))
        return levels

    def _target(self, ctx, analysis, entry, risk, bull, min_r, max_r, fallback_r,
                beyond: float | None = None) -> tuple[float, str]:
        """Nearest swing the market has not taken yet, if it pays between
        min_r and max_r (and lies beyond `beyond`); otherwise fallback_r."""
        lo = entry + min_r * risk if bull else entry - max_r * risk
        hi = entry + max_r * risk if bull else entry - min_r * risk
        levels = self._untaken(ctx, analysis, bull, lo, hi, beyond)
        if levels:
            lvl = min(levels) if bull else max(levels)
            return lvl, f"{'máximo' if bull else 'mínimo'} sin tomar en {lvl:,.2f}"
        fb = entry + fallback_r * risk if bull else entry - fallback_r * risk
        return fb, f"{fallback_r:g}R (sin estructura clara en rango)"
