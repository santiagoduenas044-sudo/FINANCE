"""Validation rules applied to a SetupCandidate before it may become a
definitive Signal. Every function is PURE and no-lookahead: it only reads the
candidate's own fields and the already-built (as-of) AnalysisContext — never
future bars, never wall-clock time, never I/O.

Each validator returns ``None`` when the check passes, or a human-readable
rejection reason (str) when it fails. The engine collects every failing reason
so a NoTrade always explains itself fully.
"""
from __future__ import annotations

import pandas as pd

from ..data.context import AnalysisContext
from ..data.models import CLOSE
from ..setups.models import SetupCandidate, SetupStatus
from .models import SignalDirection


def validate_status(candidate: SetupCandidate) -> str | None:
    """Reject candidates that are already INVALID or EXPIRED (per the setup's
    own lifecycle in setups.models.update_status)."""
    if candidate.status is not SetupStatus.VALID:
        return f"Setup candidate status is {candidate.status.value}, not VALID"
    return None


def validate_not_expired(candidate: SetupCandidate, as_of: pd.Timestamp) -> str | None:
    """Reject if the candidate's own validity horizon has already passed as of
    the current decision time. Uses only `as_of` (the caller's current
    decision_time) and the candidate's own valid_until — no future data."""
    if as_of > candidate.valid_until:
        return (f"Setup candidate expired: valid_until="
                f"{candidate.valid_until.isoformat()}, as_of={as_of.isoformat()}")
    return None


def validate_side(direction: SignalDirection, entry: float, stop: float,
                  take_profits: list[float]) -> str | None:
    """SL/TP must sit on the correct side of entry for the given direction."""
    if direction is SignalDirection.LONG:
        if not (stop < entry):
            return f"Invalid stop for LONG: stop {stop:.4f} must be below entry {entry:.4f}"
        bad = [tp for tp in take_profits if tp <= entry]
        if bad:
            return (f"Invalid take-profit(s) for LONG: {bad} must be above "
                    f"entry {entry:.4f}")
    else:
        if not (stop > entry):
            return f"Invalid stop for SHORT: stop {stop:.4f} must be above entry {entry:.4f}"
        bad = [tp for tp in take_profits if tp >= entry]
        if bad:
            return (f"Invalid take-profit(s) for SHORT: {bad} must be below "
                    f"entry {entry:.4f}")
    return None


def validate_risk_reward(entry: float, stop: float, take_profits: list[float],
                         min_risk_reward: float) -> str | None:
    """The nearest take-profit's R:R must meet the configured threshold."""
    if not take_profits:
        return "No take-profit target to compute R:R"
    risk = abs(entry - stop)
    if risk <= 0:
        return "Zero or negative risk distance (entry == stop)"
    nearest_rr = abs(take_profits[0] - entry) / risk
    if nearest_rr < min_risk_reward:
        return f"R:R {nearest_rr:.2f} below configured minimum {min_risk_reward:.2f}"
    return None


def validate_entry_matches_context(candidate: SetupCandidate,
                                   ctx: AnalysisContext) -> str | None:
    """Re-confirm entry == close[N] using ONLY the as-of AnalysisContext already
    built for this decision (never future bars, never re-fetching data).

    If the candidate's timeframe/timestamp is not present in this context
    (e.g. a stale candidate being re-evaluated against a later decision time),
    this is not treated as a hard failure — there is nothing to contradict it
    with, and re-deriving that is the job of validate_not_expired instead."""
    view = ctx.view_for(candidate.timeframe)
    if view is None or view.bars.empty or candidate.timestamp not in view.bars.index:
        return None
    bar_close = float(view.bars.loc[candidate.timestamp, CLOSE])
    if abs(bar_close - candidate.entry) > 1e-9:
        return (f"Entry {candidate.entry:.4f} does not match bar close "
                f"{bar_close:.4f} at {candidate.timestamp.isoformat()}")
    return None
