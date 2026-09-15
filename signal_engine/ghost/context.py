"""Market-context builder (Phase 14): turns the existing MarketAnalysis (per
timeframe) + the as-of AnalysisContext into a single structured MarketState —
multi-timeframe bias, structure state, liquidity state and key levels.

Everything is derived from data up to the decision bar (no-lookahead) using
objects the Market Analysis layer already produces. No indicators or structure
are recomputed here; GHOST only INTERPRETS them.
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import GhostConfig
from ..data.context import AnalysisContext
from ..data.models import SESSION, TRADING_DATE
from ..features.analyzer import MarketAnalysis, TimeframeAnalysis
from ..features.market_structure.models import Direction, StructureType, SwingType
from .models import (
    Bias, KeyLevel, LiquidityState, MarketState, MTFAlignment, MTFContext,
    StructureState, TimeframeBias,
)
from .regime import classify_regime


# --------------------------------------------------------------------------- #
# Bias helpers
# --------------------------------------------------------------------------- #
def _ema_bias(ta: TimeframeAnalysis) -> Bias:
    ind = ta.indicators
    if ind is None or "ema_20" not in ind or "ema_50" not in ind:
        return Bias.NEUTRAL
    f, s = ind["ema_20"].dropna(), ind["ema_50"].dropna()
    if f.empty or s.empty:
        return Bias.NEUTRAL
    d = float(f.iloc[-1]) - float(s.iloc[-1])
    if d > 0:
        return Bias.BULLISH
    if d < 0:
        return Bias.BEARISH
    return Bias.NEUTRAL


def _event_bias(ta: TimeframeAnalysis) -> Bias:
    if not ta.structure_events:
        return Bias.NEUTRAL
    last = ta.structure_events[-1]
    return Bias.BULLISH if last.direction is Direction.BULLISH else Bias.BEARISH


def _swing_bias(ta: TimeframeAnalysis) -> Bias:
    highs = [s for s in ta.swings if s.type is SwingType.HIGH]
    lows = [s for s in ta.swings if s.type is SwingType.LOW]
    if len(highs) < 2 or len(lows) < 2:
        return Bias.NEUTRAL
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        return Bias.BULLISH
    if lh and ll:
        return Bias.BEARISH
    return Bias.NEUTRAL


def timeframe_bias(ta: TimeframeAnalysis) -> Bias:
    """Majority vote of EMA / last-structure-event / swing biases. A tie or no
    majority is NEUTRAL — GHOST never guesses a direction it cannot justify."""
    votes = [_ema_bias(ta), _event_bias(ta), _swing_bias(ta)]
    bull = votes.count(Bias.BULLISH)
    bear = votes.count(Bias.BEARISH)
    if bull >= 2 and bull > bear:
        return Bias.BULLISH
    if bear >= 2 and bear > bull:
        return Bias.BEARISH
    return Bias.NEUTRAL


# --------------------------------------------------------------------------- #
def _recent_choch(ta: TimeframeAnalysis, lookback_bars: int) -> tuple[bool, object]:
    events = ta.structure_events
    if not events:
        return False, None
    ind = ta.indicators
    if ind is None or len(ind.index) == 0:
        return False, events[-1]
    cutoff = ind.index[max(0, len(ind.index) - lookback_bars)]
    choch = [e for e in events if e.type is StructureType.CHOCH and e.timestamp >= cutoff]
    return (len(choch) > 0), events[-1]


def structure_state(ta: TimeframeAnalysis, cfg: GhostConfig) -> StructureState:
    bias = timeframe_bias(ta)
    changed, last = _recent_choch(ta, cfg.transition_lookback)
    last_type = last.type.value if last is not None else None
    last_dir = None
    if last is not None:
        last_dir = Bias.BULLISH if last.direction is Direction.BULLISH else Bias.BEARISH
    # short label of the last swings, e.g. "HH,HL"
    labels = [s.label.value for s in ta.swings[-4:] if s.label is not None]
    return StructureState(bias, changed, last_type, last_dir, ",".join(labels))


def mtf_context(ma: MarketAnalysis, ctx: AnalysisContext, cfg: GhostConfig) -> MTFContext:
    htf = ma.by_timeframe.get(ctx.htf.timeframe)
    stf = ma.by_timeframe.get(ctx.structure.timeframe)
    etf = ma.by_timeframe.get(ctx.execution.timeframe)
    cb = timeframe_bias(htf) if htf else Bias.NEUTRAL
    sb = timeframe_bias(stf) if stf else Bias.NEUTRAL
    eb = timeframe_bias(etf) if etf else Bias.NEUTRAL

    def opp(a: Bias, b: Bias) -> bool:
        return {a, b} == {Bias.BULLISH, Bias.BEARISH}

    stf_changed = _recent_choch(stf, cfg.transition_lookback)[0] if stf else False
    if cb is Bias.NEUTRAL and sb is Bias.NEUTRAL and eb is Bias.NEUTRAL:
        align = MTFAlignment.UNKNOWN
    elif cb == sb == eb and cb is not Bias.NEUTRAL:
        align = MTFAlignment.ALIGNED
    elif opp(cb, eb):
        align = MTFAlignment.CONFLICT
    elif opp(cb, sb) and stf_changed:
        align = MTFAlignment.TRANSITION
    else:
        align = MTFAlignment.MIXED

    return MTFContext(
        TimeframeBias(ctx.htf.timeframe, cb, "context"),
        TimeframeBias(ctx.structure.timeframe, sb, "structure"),
        TimeframeBias(ctx.execution.timeframe, eb, "execution"),
        align,
    )


def liquidity_state(ta: TimeframeAnalysis, price: float | None) -> LiquidityState:
    levels = ta.liquidity_levels
    swept = [lv for lv in levels if lv.swept_at is not None]
    last = max(swept, key=lambda lv: lv.swept_at) if swept else None
    last_dir = last_price = last_time = None
    reclaimed = False
    if last is not None:
        last_dir = Bias.BULLISH if last.direction is Direction.BULLISH else Bias.BEARISH
        last_price = float(last.level)
        last_time = last.swept_at
        if price is not None:
            # buy-side (highs) swept then price back below => reclaimed; mirror for sell-side
            reclaimed = (price < last_price) if last.direction is Direction.BULLISH \
                else (price > last_price)

    unswept = [lv for lv in levels if lv.swept_at is None]
    ups = sorted(float(lv.level) for lv in unswept
                 if lv.direction is Direction.BULLISH and (price is None or lv.level > price))
    downs = sorted((float(lv.level) for lv in unswept
                    if lv.direction is Direction.BEARISH and (price is None or lv.level < price)),
                   reverse=True)
    detail = ""
    if last is not None:
        side = "buy-side" if last.direction is Direction.BULLISH else "sell-side"
        detail = f"{side} swept @ {last_price:.2f}" + (" (reclaimed)" if reclaimed else "")
    return LiquidityState(last_dir, last_price, last_time, reclaimed,
                          ups[0] if ups else None, downs[0] if downs else None, detail)


# --------------------------------------------------------------------------- #
# Key levels
# --------------------------------------------------------------------------- #
def _touched(bars: pd.DataFrame, price: float, tol: float, lookback: int = 20) -> bool:
    tail = bars.tail(lookback)
    if tail.empty:
        return False
    return bool(((tail["low"] <= price + tol) & (tail["high"] >= price - tol)).any())


def key_levels(ctx: AnalysisContext, exec_ta: TimeframeAnalysis,
               price: float | None, swept_prices: set[float]) -> list[KeyLevel]:
    bars = ctx.execution.bars
    tf = ctx.execution.timeframe
    if bars.empty:
        return []
    atr = 1.0
    if exec_ta.indicators is not None and "atr_14" in exec_ta.indicators:
        a = exec_ta.indicators["atr_14"].dropna()
        if not a.empty and float(a.iloc[-1]) > 0:
            atr = float(a.iloc[-1])
    tol = atr * 0.15
    out: list[KeyLevel] = []

    def add(pr: float, kind: str, side: str, created=None) -> None:
        dist = (price - pr) if price is not None else None
        out.append(KeyLevel(
            price=float(pr), kind=kind, timeframe=tf, side=side, created_at=created,
            distance=dist, touched=_touched(bars, pr, tol),
            swept=any(abs(pr - sp) <= tol for sp in swept_prices),
        ))

    # previous-day + session high/low (from the annotation the Data Engine adds)
    if TRADING_DATE in bars.columns:
        by_day = list(bars.groupby(bars[TRADING_DATE], sort=True))
        if by_day:
            cur_day, cur_df = by_day[-1]
            add(float(cur_df["high"].max()), "session_high", "buy-side")
            add(float(cur_df["low"].min()), "session_low", "sell-side")
            if len(by_day) >= 2:
                _, prev_df = by_day[-2]
                add(float(prev_df["high"].max()), "prev_day_high", "buy-side")
                add(float(prev_df["low"].min()), "prev_day_low", "sell-side")

    # recent swing highs/lows
    for sw in exec_ta.swings[-6:]:
        side = "buy-side" if sw.type is SwingType.HIGH else "sell-side"
        add(float(sw.price), f"swing_{sw.type.value.lower()}", side, sw.timestamp)

    # equal highs / equal lows (clusters within tolerance) -> liquidity pools
    highs = sorted(float(s.price) for s in exec_ta.swings if s.type is SwingType.HIGH)
    lows = sorted(float(s.price) for s in exec_ta.swings if s.type is SwingType.LOW)
    for cluster, kind, side in ((highs, "equal_highs", "buy-side"),
                                (lows, "equal_lows", "sell-side")):
        for i in range(len(cluster) - 1):
            if abs(cluster[i + 1] - cluster[i]) <= tol:
                add((cluster[i] + cluster[i + 1]) / 2.0, kind, side)
                break
    return out


# --------------------------------------------------------------------------- #
def build_market_state(ctx: AnalysisContext, ma: MarketAnalysis,
                       cfg: GhostConfig) -> MarketState:
    exec_ta = ma.by_timeframe.get(ctx.execution.timeframe)
    bars = ctx.execution.bars
    price = float(bars["close"].iloc[-1]) if not bars.empty else None

    vwap = vwap_side = None
    if exec_ta is not None and exec_ta.indicators is not None and "vwap" in exec_ta.indicators:
        v = exec_ta.indicators["vwap"].dropna()
        if not v.empty:
            vwap = float(v.iloc[-1])
    if vwap is not None and price is not None:
        vwap_side = "ABOVE" if price > vwap else ("BELOW" if price < vwap else "AT")
    else:
        vwap_side = "UNKNOWN"

    regime = classify_regime(exec_ta, cfg, ctx.decision_time) if exec_ta \
        else classify_regime(_empty_ta(ctx), cfg, ctx.decision_time)
    structure = structure_state(exec_ta, cfg) if exec_ta \
        else StructureState(Bias.NEUTRAL, False, None, None, "")
    mtf = mtf_context(ma, ctx, cfg)
    liq = liquidity_state(exec_ta, price) if exec_ta \
        else LiquidityState(None, None, None, False, None, None, "")
    swept_prices = {float(lv.level) for lv in (exec_ta.liquidity_levels if exec_ta else [])
                    if lv.swept_at is not None}
    levels = key_levels(ctx, exec_ta, price, swept_prices) if exec_ta else []

    return MarketState(
        symbol=ctx.symbol, decision_time=ctx.decision_time, session=ctx.session,
        price=price, vwap=vwap, vwap_side=vwap_side, regime=regime, mtf=mtf,
        structure=structure, liquidity=liq, key_levels=levels,
    )


def _empty_ta(ctx: AnalysisContext) -> TimeframeAnalysis:
    return TimeframeAnalysis(ctx.execution.timeframe, ctx.decision_time,
                             pd.DataFrame(), [], [], [], [])
