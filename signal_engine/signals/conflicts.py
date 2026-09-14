"""Conflict detection between concurrently-detected setups.

A REAL conflict is two or more SetupCandidates for the SAME instrument, within
the SAME batch (i.e. the same SetupEngine.detect(ctx) call / decision bar),
that disagree on directional bias (one BULLISH, another BEARISH). Multiple
setups agreeing on the same direction is NOT a conflict — that is simply
confluence, and each still becomes its own Signal.
"""
from __future__ import annotations

from collections import defaultdict

from ..setups.models import SetupCandidate
from .models import DIRECTION_TO_SIGNAL


def find_conflicts(candidates: list[SetupCandidate]) -> dict[str, list[SetupCandidate]]:
    """Return {instrument: candidates} for every instrument where the batch's
    candidates disagree on final direction. Instruments with unanimous (or
    single) direction are omitted entirely."""
    by_instrument: dict[str, list[SetupCandidate]] = defaultdict(list)
    for c in candidates:
        by_instrument[c.instrument].append(c)

    conflicts: dict[str, list[SetupCandidate]] = {}
    for instrument, group in by_instrument.items():
        directions = {DIRECTION_TO_SIGNAL[c.direction] for c in group}
        if len(directions) > 1:
            conflicts[instrument] = group
    return conflicts


def conflict_reason(group: list[SetupCandidate]) -> str:
    parts = ", ".join(
        f"{c.setup_name}={DIRECTION_TO_SIGNAL[c.direction].value}" for c in group
    )
    return f"Conflicting setups for {group[0].instrument} at this bar: {parts}"
