"""Helpers shared by structure backends (backend-agnostic)."""
from __future__ import annotations

from .models import SwingLabel, SwingPoint, SwingType


def label_swings(swings: list[SwingPoint]) -> list[SwingPoint]:
    """Attach HH/HL/LH/LL by comparing each swing to the previous swing of the
    same type. The first swing of each type has no prior => label None.
    Pure function of the confirmed swings passed in (no look-ahead)."""
    ordered = sorted(swings, key=lambda s: (s.timestamp, s.type.value))
    last_high: float | None = None
    last_low: float | None = None
    out: list[SwingPoint] = []
    for s in ordered:
        label: SwingLabel | None = None
        if s.type is SwingType.HIGH:
            if last_high is not None:
                label = SwingLabel.HH if s.price > last_high else SwingLabel.LH
            last_high = s.price
        else:
            if last_low is not None:
                label = SwingLabel.HL if s.price > last_low else SwingLabel.LL
            last_low = s.price
        out.append(SwingPoint(s.timestamp, s.price, s.type, s.confirmed_at, label))
    return out
