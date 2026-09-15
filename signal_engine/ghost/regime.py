"""Market-regime classification (Phase 14) — deterministic, quantitative, and
no-lookahead. Uses ONLY the existing causal indicators (EMA 20/50, ATR) and the
existing market-structure events (CHoCH) computed up to the decision bar.

Rules (documented so they are testable, not arbitrary), evaluated in priority
order on the chosen timeframe view:

  0. Fewer than `regime_lookback` bars                         -> UNKNOWN
  1. A CHoCH within the last `transition_lookback` bars        -> TRANSITION
  2. emaFast - emaSlow >  range_atr_ratio*ATR, slope up,
     close > emaFast                                           -> TREND_UP
  3. emaFast - emaSlow < -range_atr_ratio*ATR, slope down,
     close < emaFast                                           -> TREND_DOWN
  4. |emaFast - emaSlow| < range_atr_ratio*ATR (flat EMAs):
        volatility HIGH -> HIGH_VOLATILITY
        volatility LOW  -> LOW_VOLATILITY
        else            -> RANGE
  5. otherwise (mixed): HIGH/LOW volatility if extreme, else RANGE

Volatility bucket comes from the ATR percentile within the lookback window
(HIGH above `high_vol_pct`, LOW below `low_vol_pct`, else NORMAL).
"""
from __future__ import annotations

import pandas as pd

from ..config.schema import GhostConfig
from ..features.analyzer import TimeframeAnalysis
from ..features.market_structure.models import Direction, StructureType
from .models import MarketRegime, RegimeState, Volatility


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _atr_percentile(atr: pd.Series, lookback: int) -> tuple[float | None, float | None]:
    """(current_atr, percentile in [0,1]) over the last `lookback` values."""
    s = atr.dropna()
    if s.empty:
        return None, None
    window = s.tail(lookback)
    cur = float(window.iloc[-1])
    if len(window) < 3:
        return cur, None
    pct = float((window <= cur).mean())
    return cur, pct


def classify_regime(analysis: TimeframeAnalysis, cfg: GhostConfig,
                    decision_time: pd.Timestamp) -> RegimeState:
    ind = analysis.indicators
    n = 0 if ind is None else len(ind)

    def _unknown(detail: str) -> RegimeState:
        return RegimeState(MarketRegime.UNKNOWN, Volatility.UNKNOWN, 0.0, None,
                           None, None, None, detail, decision_time)

    if ind is None or n < max(3, cfg.regime_lookback // 2):
        return _unknown(f"insufficient bars ({n})")

    ema_fast_col = "ema_20" if "ema_20" in ind.columns else None
    ema_slow_col = "ema_50" if "ema_50" in ind.columns else None
    atr_col = "atr_14" if "atr_14" in ind.columns else None
    if not (ema_fast_col and ema_slow_col and atr_col):
        return _unknown("indicator columns missing")

    ema_fast_s = ind[ema_fast_col].dropna()
    ema_slow_s = ind[ema_slow_col].dropna()
    if ema_fast_s.empty or ema_slow_s.empty:
        return _unknown("EMAs not warmed up")

    ema_fast = float(ema_fast_s.iloc[-1])
    ema_slow = float(ema_slow_s.iloc[-1])
    # Trend uses the EMA relationship + slope (both causal). Close/price is not
    # needed here and is filled at the MarketState level for display.
    close = None
    cur_atr, atr_pct = _atr_percentile(ind[atr_col], cfg.regime_lookback)
    atr = cur_atr if cur_atr and cur_atr > 0 else None

    # volatility bucket
    if atr_pct is None:
        vol = Volatility.UNKNOWN
    elif atr_pct >= cfg.high_vol_pct:
        vol = Volatility.HIGH
    elif atr_pct <= cfg.low_vol_pct:
        vol = Volatility.LOW
    else:
        vol = Volatility.NORMAL

    diff = ema_fast - ema_slow
    trend_strength = _clip(diff / atr) if atr else 0.0

    # EMA slope over slope_lookback
    k = min(cfg.regime_slope_lookback, len(ema_fast_s) - 1)
    slope = (ema_fast - float(ema_fast_s.iloc[-1 - k])) if k > 0 else 0.0

    # (1) recent CHoCH -> TRANSITION
    recent_choch = _recent_choch(analysis, decision_time, cfg.transition_lookback, ind)
    thr = (cfg.range_atr_ratio * atr) if atr else 0.0

    if recent_choch:
        regime = MarketRegime.TRANSITION
        detail = "recent CHoCH"
    elif atr and diff > thr and slope > 0:
        regime = MarketRegime.TREND_UP
        detail = f"emaFast-emaSlow={diff:.2f} > {thr:.2f}, slope+"
    elif atr and diff < -thr and slope < 0:
        regime = MarketRegime.TREND_DOWN
        detail = f"emaFast-emaSlow={diff:.2f} < -{thr:.2f}, slope-"
    elif atr and abs(diff) < thr:
        if vol is Volatility.HIGH:
            regime, detail = MarketRegime.HIGH_VOLATILITY, "flat EMAs + high ATR"
        elif vol is Volatility.LOW:
            regime, detail = MarketRegime.LOW_VOLATILITY, "flat EMAs + low ATR"
        else:
            regime, detail = MarketRegime.RANGE, "flat EMAs"
    else:
        if vol is Volatility.HIGH:
            regime, detail = MarketRegime.HIGH_VOLATILITY, "mixed + high ATR"
        elif vol is Volatility.LOW:
            regime, detail = MarketRegime.LOW_VOLATILITY, "mixed + low ATR"
        else:
            regime, detail = MarketRegime.RANGE, "mixed / no clear trend"

    return RegimeState(regime, vol, trend_strength, atr_pct, ema_fast, ema_slow,
                       close, detail, decision_time)


def _recent_choch(analysis: TimeframeAnalysis, decision_time: pd.Timestamp,
                  lookback_bars: int, ind: pd.DataFrame) -> bool:
    """True if a CHoCH occurred within the last `lookback_bars` bars (by index
    position, so it is timeframe-agnostic and no-lookahead)."""
    events = [e for e in analysis.structure_events if e.type is StructureType.CHOCH]
    if not events:
        return False
    idx = ind.index
    if len(idx) == 0:
        return False
    cutoff_pos = max(0, len(idx) - lookback_bars)
    cutoff_time = idx[cutoff_pos]
    return any(e.timestamp >= cutoff_time for e in events)
