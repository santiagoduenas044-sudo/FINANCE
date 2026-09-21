"""Explainable confluence scoring (Phase 14).

Turns the structured MarketState + a base Signal into a transparent quality
score with itemised contributions, so GHOST can answer exactly *why* one trade
scored 82 and another 54. The score is the RELATIVE QUALITY of the conditions —
NOT a probability of winning. All weights are configurable (GhostConfig.weights).
"""
from __future__ import annotations

from ..config.schema import GhostConfig
from ..features.analyzer import TimeframeAnalysis
from .models import (
    Bias, ConfluenceContribution, ConfluenceResult, MarketRegime, MarketState,
)

# Setups that are trend/continuation vs mean-reversion/reversal in nature —
# used only to judge regime fit, never to change the setups themselves.
_TREND_SETUPS = {"orb"}
_REVERSION_SETUPS = {"liquidity_sweep", "vwap_reclaim"}


def _nearest_rr(signal) -> float | None:
    rr = list(getattr(signal, "risk_reward", []) or [])
    return min(rr) if rr else None


def _rvol(exec_ta: TimeframeAnalysis) -> float | None:
    ind = exec_ta.indicators if exec_ta else None
    if ind is None or "rvol_20" not in ind:
        return None
    s = ind["rvol_20"].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def _structural_penalty_scale(setup: str, cfg: GhostConfig) -> float:
    """Reversion setups are *designed* to fire against the prevailing structure —
    that is their thesis, not a defect. Charging them the full trend-penalty
    double-counts the setup's own premise, so they pay a reduced rate."""
    return cfg.reversion_penalty_scale if setup in _REVERSION_SETUPS else 1.0


def _regime_fit(regime: MarketRegime, direction: str, setup: str) -> tuple[float, str]:
    """Return a factor in [-1, 1] for how well the regime fits this
    direction/setup, plus a short note."""
    long = direction == "LONG"
    if regime is MarketRegime.TREND_UP:
        return (1.0, "trend up") if long else (-0.5, "counter-trend (up)")
    if regime is MarketRegime.TREND_DOWN:
        return (1.0, "trend down") if not long else (-0.5, "counter-trend (down)")
    if regime is MarketRegime.RANGE:
        if setup in _REVERSION_SETUPS:
            return 0.6, "range favors reversion"
        if setup in _TREND_SETUPS:
            return -0.4, "breakout inside range"
        return 0.0, "range"
    if regime in (MarketRegime.HIGH_VOLATILITY, MarketRegime.TRANSITION):
        return -0.2, regime.value.lower()
    if regime is MarketRegime.LOW_VOLATILITY:
        return 0.1, "low volatility"
    return 0.0, "unknown regime"


def score_confluence(direction: str, ms: MarketState, signal,
                     exec_ta: TimeframeAnalysis, cfg: GhostConfig) -> ConfluenceResult:
    w = cfg.weights
    want = Bias.BULLISH if direction == "LONG" else Bias.BEARISH
    opp = Bias.BEARISH if direction == "LONG" else Bias.BULLISH
    contribs: list[ConfluenceContribution] = []
    setup = getattr(signal, "setup", "") or ""
    pen = _structural_penalty_scale(setup, cfg)

    def add(name: str, pts: float, detail: str) -> None:
        if pts != 0:
            contribs.append(ConfluenceContribution(name, pts, detail))

    # HTF (context) alignment / conflict
    if ms.mtf.context.bias is want:
        add("htf_alignment", w.htf_alignment, f"HTF {ms.mtf.context.bias.value}")
    elif ms.mtf.context.bias is opp:
        add("htf_conflict", round(-w.htf_conflict_penalty * pen, 2),
            f"HTF {ms.mtf.context.bias.value}")

    # structure-timeframe (e.g. 15m) alignment
    if ms.mtf.structure.bias is want:
        add("mtf_structure_alignment", w.mtf_structure_alignment,
            f"{ms.mtf.structure.timeframe} {ms.mtf.structure.bias.value}")

    # execution structure alignment / against
    if ms.structure.aligns_with(direction):
        add("structure_alignment", w.structure_alignment,
            f"structure {ms.structure.bias.value} ({ms.structure.swing_summary})")
    elif ms.structure.opposes(direction):
        add("against_structure", round(-w.against_structure_penalty * pen, 2),
            f"structure {ms.structure.bias.value}")

    # liquidity: a sweep against the trade's stops that then reclaimed is supportive
    supportive = (ms.liquidity.last_sweep_direction is opp) and ms.liquidity.reclaimed
    partial = (ms.liquidity.last_sweep_direction is opp) and not ms.liquidity.reclaimed
    if supportive:
        add("liquidity_sweep", w.liquidity_sweep, ms.liquidity.detail or "sweep + reclaim")
    elif partial:
        add("liquidity_sweep", w.liquidity_sweep * 0.5, ms.liquidity.detail or "sweep (no reclaim)")

    # VWAP side
    if (direction == "LONG" and ms.vwap_side == "ABOVE") or \
       (direction == "SHORT" and ms.vwap_side == "BELOW"):
        add("vwap_alignment", w.vwap_alignment, f"price {ms.vwap_side} VWAP")

    # volume confirmation
    rvol = _rvol(exec_ta)
    if rvol is not None and rvol >= cfg.rvol_confirm:
        add("volume_confirmation", w.volume_confirmation, f"RVOL {rvol:.2f}")

    # R:R quality. NOTE: while every setup builds targets as exact R multiples
    # of rr_targets (nearest = 1.0R by construction), this factor has zero
    # variance and acts as a constant offset — it cannot rank anything. It only
    # discriminates once targets become level-based. Budget the threshold
    # accordingly: min_confluence_score minus this constant is the real bar.
    rr = _nearest_rr(signal)
    if rr is not None:
        if rr >= 1.2:
            add("rr_quality", w.rr_quality, f"R:R {rr:.2f}")
        elif rr >= 1.0:
            add("rr_quality", w.rr_quality * 0.5, f"R:R {rr:.2f}")

    # regime fit
    factor, note = _regime_fit(ms.regime.regime, direction, setup)
    if factor != 0:
        add("regime_alignment", round(w.regime_alignment * factor, 2), note)

    score = round(sum(c.points for c in contribs), 2)
    return ConfluenceResult(direction, score, cfg.min_confluence_score,
                            score >= cfg.min_confluence_score, contribs)
