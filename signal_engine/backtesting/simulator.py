"""Trade outcome simulation (Phase 8).

`resolve_trade_outcome` is a PURE function of (signal, bars) — it determines
what happened AFTER a Signal fired by walking forward through subsequent bars
looking for the first touch of the stop or of TP1 (the nearest take-profit).

IMPORTANT — this is legitimately allowed to read bars AFTER signal.timestamp:
that is the whole point of resolving a historical outcome. This is NOT a
no-lookahead violation. The no-lookahead guarantee that matters (never using
future bars to DECIDE to enter) is owned entirely by Setup/Signal Engine
(Phases 5-6) and is untouched here — this module only asks "given a signal
that already fired, what happened next?".

KNOWN SIMPLIFICATION (documented, not hidden): OHLC bars don't reveal
intra-bar path. If both the stop and TP1 are touched within the SAME bar, we
conservatively assume the stop was hit first (the pessimistic assumption).
See docs/PHASE8_BACKTESTING.md.
"""
from __future__ import annotations

import pandas as pd

from ..data.models import CLOSE, HIGH, LOW
from ..signals.models import Signal, SignalDirection
from .models import ClosedTrade, TradeOutcome


def resolve_trade_outcome(signal: Signal, bars: pd.DataFrame) -> ClosedTrade:
    """`bars` is the FULL historical frame for signal.timeframe (any rows at
    or before signal.timestamp are ignored here)."""
    bullish = signal.direction is SignalDirection.LONG
    tp1 = signal.take_profits[0] if signal.take_profits else None

    future = bars[bars.index > signal.timestamp]
    if signal.expiration is not None:
        future = future[future.index <= signal.expiration]

    bars_held = 0
    for ts, row in future.iterrows():
        bars_held += 1
        high, low = float(row[HIGH]), float(row[LOW])
        stop_hit = (low <= signal.stop) if bullish else (high >= signal.stop)
        tp_hit = (tp1 is not None) and ((high >= tp1) if bullish else (low <= tp1))

        if stop_hit and tp_hit:
            # both touched in the same bar: intra-bar order unknown from OHLC
            # alone -> conservative assumption, stop hit first (see module docstring)
            return _make(signal, ts, TradeOutcome.LOSS, -1.0, bars_held)
        if stop_hit:
            return _make(signal, ts, TradeOutcome.LOSS, -1.0, bars_held)
        if tp_hit:
            r = signal.risk_reward[0] if signal.risk_reward else None
            return _make(signal, ts, TradeOutcome.WIN, r, bars_held)

    # neither touched
    if signal.expiration is not None:
        return _make(signal, signal.expiration, TradeOutcome.EXPIRED, None, bars_held)
    return _make(signal, None, TradeOutcome.OPEN, None, bars_held)


def _make(signal: Signal, exit_ts, outcome: TradeOutcome, r: float | None,
         bars_held: int) -> ClosedTrade:
    return ClosedTrade(
        signal_id=signal.signal_id,
        instrument=signal.instrument,
        setup=signal.setup,
        direction=signal.direction,
        entry_timestamp=signal.timestamp,
        exit_timestamp=exit_ts,
        entry=signal.entry,
        stop=signal.stop,
        take_profit=signal.take_profits[0] if signal.take_profits else None,
        outcome=outcome,
        r_multiple=r,
        bars_held=bars_held,
    )
