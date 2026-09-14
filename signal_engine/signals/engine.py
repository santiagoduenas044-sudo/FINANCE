"""SignalEngine: converts SetupCandidate(s) -> Signal | NoTrade.

Setup Engine -> Signal Engine -> Output Dispatcher -> Discord

Determinism & no-lookahead: every method here is a pure function of its
arguments except for an internal, in-memory dedup cache (mirroring the
pattern already used by the Discord output adapter). Nothing reads the
wall-clock, the network, or any future bar — only the candidate's own fields
and the already-built (as-of) AnalysisContext. This is exactly why the SAME
engine works for backtesting and for live/paper: replaying history bar-by-bar
through this engine produces identical results to running it live at each bar.

This module MUST NOT import signal_engine.output (or anything Discord-related).
"""
from __future__ import annotations

from ..config.schema import SignalConfig
from ..data.context import AnalysisContext
from ..setups.models import SetupCandidate
from .conflicts import conflict_reason, find_conflicts
from .models import (
    DIRECTION_TO_SIGNAL, NoTrade, Signal, SignalDirection, compute_signal_id,
)
from .validators import (
    validate_entry_matches_context, validate_not_expired, validate_risk_reward,
    validate_side, validate_status,
)


class SignalEngine:
    def __init__(self, config: SignalConfig):
        self.cfg = config
        self._seen: set[str] = set()

    # ------------------------------------------------------------------ #
    # Single-candidate evaluation. Pure (does not consult/mutate dedup
    # state) so it can be called directly and predictably in tests.
    # ------------------------------------------------------------------ #
    def evaluate(self, ctx: AnalysisContext, candidate: SetupCandidate,
                override_reason: str | None = None) -> Signal | NoTrade:
        direction = DIRECTION_TO_SIGNAL[candidate.direction]

        if override_reason is not None:
            return self._no_trade(candidate, [override_reason], direction)

        reasons: list[str] = []
        for reason in (
            validate_status(candidate),
            validate_not_expired(candidate, ctx.decision_time),
            validate_side(direction, candidate.entry, candidate.stop_loss,
                         candidate.take_profits),
            validate_risk_reward(candidate.entry, candidate.stop_loss,
                                 candidate.take_profits, self.cfg.min_risk_reward),
        ):
            if reason:
                reasons.append(reason)

        if self.cfg.validate_entry_against_context:
            reason = validate_entry_matches_context(candidate, ctx)
            if reason:
                reasons.append(reason)

        if reasons:
            return self._no_trade(candidate, reasons, direction)

        signal_id = compute_signal_id(
            candidate.instrument, candidate.setup_name, direction,
            candidate.timestamp, candidate.timeframe, candidate.entry,
            candidate.stop_loss,
        )
        return Signal(
            signal_id=signal_id,
            instrument=candidate.instrument,
            setup=candidate.setup_name,
            direction=direction,
            timestamp=candidate.timestamp,
            timeframe=candidate.timeframe,
            entry=candidate.entry,
            stop=candidate.stop_loss,
            take_profits=list(candidate.take_profits),      # setup-specific targets, kept as-is
            risk_reward=list(candidate.risk_reward),
            reasons=list(candidate.reasons),
            market_context=dict(candidate.market_context),
            invalidation_conditions=list(candidate.invalidation_conditions),
            invalidation_price=candidate.invalidation_price,
            referenced=list(candidate.referenced),
            expiration=candidate.valid_until,
        )

    def _no_trade(self, candidate: SetupCandidate, reasons: list[str],
                  direction: SignalDirection) -> NoTrade:
        return NoTrade(
            instrument=candidate.instrument,
            timestamp=candidate.timestamp,
            reasons=reasons,
            setup=candidate.setup_name,
            timeframe=candidate.timeframe,
            candidate_direction=direction,
        )

    def dedup_key(self, candidate: SetupCandidate) -> str:
        """Deterministic identity for a candidate, independent of whether it
        resolves to a Signal or a NoTrade — used to avoid re-processing the
        exact same candidate twice."""
        direction = DIRECTION_TO_SIGNAL[candidate.direction]
        return compute_signal_id(
            candidate.instrument, candidate.setup_name, direction,
            candidate.timestamp, candidate.timeframe, candidate.entry,
            candidate.stop_loss,
        )

    # ------------------------------------------------------------------ #
    # Batch evaluation: conflict detection across concurrently-detected
    # setups, plus deterministic deduplication. This is the entry point
    # Setup Engine output should be fed through.
    # ------------------------------------------------------------------ #
    def evaluate_candidates(
        self, ctx: AnalysisContext, candidates: list[SetupCandidate],
    ) -> list[Signal | NoTrade]:
        conflicts = find_conflicts(candidates)
        results: list[Signal | NoTrade] = []

        for candidate in candidates:
            key = self.dedup_key(candidate)
            if self.cfg.deduplication_enabled and key in self._seen:
                continue    # already processed this exact candidate before

            if candidate.instrument in conflicts:
                result = self.evaluate(
                    ctx, candidate,
                    override_reason=conflict_reason(conflicts[candidate.instrument]),
                )
            else:
                result = self.evaluate(ctx, candidate)

            if self.cfg.deduplication_enabled:
                self._seen.add(key)
            results.append(result)

        return results
