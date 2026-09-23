"""Regime-adaptive take-profit targets.

The setups build their targets from a fixed R list (`rr_targets: [1.0, 2.0]`),
so a strong trend and a dead range ask for exactly the same distance. That is
the wrong shape: in a trend the move that justifies the risk keeps going, and
in a range it stalls and reverses well before 2R.

GHOST does not reach into the Setup or Signal engines — it decides. This module
is that decision, expressed as a pure function of the signal and the market
state, so it is testable on its own and the backtest can apply exactly what the
runtime applies. The NEAR target is left as the setup built it: taking part of
the position off early is what makes a far target survivable. Only the FAR
target moves.

Nothing here fabricates data — the band comes from the same `_regime_fit` that
confluence scoring already uses, so the score and the target tell one story.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from ..config.schema import GhostConfig
from .confluence import _regime_fit
from .models import MarketState, MTFAlignment


@dataclass(frozen=True)
class TargetPlan:
    take_profits: list[float]
    risk_reward: list[float]
    far_r: float
    band: str            # aligned / favorable / neutral / counter
    basis: str           # short human-readable justification


def _band(fit: float) -> str:
    if fit >= 0.8:
        return "aligned"
    if fit >= 0.3:
        return "favorable"
    if fit >= -0.3:
        return "neutral"
    return "counter"


def plan_targets(sig, ms: MarketState, cfg: GhostConfig) -> TargetPlan | None:
    """Targets for this signal given the regime, or None to keep the setup's
    own plan (adaptation disabled, or nothing sensible to change)."""
    if not cfg.adaptive_targets:
        return None
    risk = abs(sig.entry - sig.stop)
    if risk <= 0 or not sig.take_profits:
        return None

    direction = sig.direction.value
    fit, note = _regime_fit(ms.regime.regime, direction, sig.setup or "")
    band = _band(fit)
    far_r = cfg.target_r_by_band.get(band)
    if far_r is None:
        return None

    # All three timeframes pointing the same way is the one condition under
    # which a trend target is worth stretching; a lone execution-timeframe
    # impulse is not.
    stretched = False
    if band == "aligned" and ms.mtf.alignment is MTFAlignment.ALIGNED \
            and cfg.target_r_mtf_bonus > 0:
        far_r += cfg.target_r_mtf_bonus
        stretched = True

    long = direction == "LONG"
    near = sig.take_profits[0]
    near_r = abs(near - sig.entry) / risk
    if far_r <= near_r:
        # The regime does not justify going beyond the near target — take the
        # single close target rather than inventing an out-of-order plan.
        tps = [near]
    else:
        far = sig.entry + far_r * risk if long else sig.entry - far_r * risk
        tps = [near, far]

    rr = [abs(t - sig.entry) / risk for t in tps]
    basis = (f"{note}; {band} -> {far_r:g}R"
             + (" (MTF aligned)" if stretched else ""))
    return TargetPlan(take_profits=tps, risk_reward=rr, far_r=far_r,
                      band=band, basis=basis)


def apply_plan(sig, plan: TargetPlan | None):
    """A copy of `sig` carrying the adapted plan. `signal_id` is derived from
    instrument/setup/direction/timestamp/entry/stop — never from the targets —
    so identity survives the change and end-to-end dedup still holds."""
    if plan is None:
        return sig
    return replace(sig, take_profits=list(plan.take_profits),
                   risk_reward=list(plan.risk_reward))
