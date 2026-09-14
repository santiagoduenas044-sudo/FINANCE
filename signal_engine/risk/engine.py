"""RiskEngine: Signal -> Risk Validation -> Position Size / Risk Decision.

Signal Engine -> Risk Engine -> Output

Determinism & purity: `evaluate()` is a pure function of (signal, account,
config, registry) — no wall-clock, no I/O, no hidden state. The SAME engine
therefore works identically for backtest, paper, and live use, exactly like
the Setup/Signal engines before it.

CRITICAL — CME verification gate: this module NEVER computes a dollar amount
for an instrument unless `ContractRegistry.require_verified()` succeeds. That
guard (from Phase 3/6.5) is called directly and its RuntimeError is turned
into a REJECTED RiskDecision with reason_code=UNVERIFIED_CONTRACT_SPEC — the
guard itself is never bypassed, weakened, or duplicated.

This module MUST NOT import signal_engine.output, or anything related to
Discord, TradingView, a broker, Streamlit, or IA/LLM.
"""
from __future__ import annotations

import math

from ..config.schema import RiskConfig
from ..data.contracts import ContractRegistry
from ..signals.models import Signal
from .models import AccountState, RiskDecision, RiskOutcome, RiskReasonCode


class RiskEngine:
    def __init__(self, config: RiskConfig, registry: ContractRegistry):
        self.cfg = config
        self.registry = registry

    def evaluate(self, signal: Signal, account: AccountState) -> RiskDecision:
        # ------------------------------------------------------------------ #
        # 1) CME verification gate — hard stop, no computation beyond this.
        #    require_verified() is called directly; never bypassed.
        # ------------------------------------------------------------------ #
        try:
            self.registry.require_verified()
        except RuntimeError as exc:
            return self._hard_stop(
                signal, RiskReasonCode.UNVERIFIED_CONTRACT_SPEC, str(exc),
            )

        # ------------------------------------------------------------------ #
        # 2) Instrument must have a known spec (defensive; ES/NQ/MES/MNQ are
        #    all supported generically via the registry, no per-symbol code).
        # ------------------------------------------------------------------ #
        try:
            spec = self.registry.spec(signal.instrument)
        except KeyError:
            return self._hard_stop(
                signal, RiskReasonCode.UNKNOWN_INSTRUMENT,
                f"No ContractSpec for instrument {signal.instrument!r}",
            )

        # ------------------------------------------------------------------ #
        # 3) Stop distance must be strictly positive (never negative/zero risk).
        # ------------------------------------------------------------------ #
        stop_distance_points = abs(signal.entry - signal.stop)
        if stop_distance_points <= 0:
            return self._hard_stop(
                signal, RiskReasonCode.INVALID_STOP,
                f"Invalid stop: entry {signal.entry:.4f} and stop "
                f"{signal.stop:.4f} give zero/negative risk distance",
            )
        stop_distance_ticks = spec.ticks_from_points(stop_distance_points)

        # risk per contract = stop distance (points) x point value
        risk_per_contract = stop_distance_points * spec.point_value

        # ------------------------------------------------------------------ #
        # 4) Risk budget for this trade: fixed-dollar or percent-of-account,
        #    then optionally capped by an absolute per-trade ceiling.
        # ------------------------------------------------------------------ #
        if self.cfg.risk_mode == "fixed":
            max_risk_dollars = self.cfg.fixed_risk_dollar
        else:
            max_risk_dollars = account.equity * self.cfg.risk_pct
        if self.cfg.max_risk_per_trade_dollar is not None:
            max_risk_dollars = min(max_risk_dollars, self.cfg.max_risk_per_trade_dollar)

        reasons: list[str] = []
        codes: list[RiskReasonCode] = []

        if max_risk_dollars <= 0:
            return self._hard_stop(
                signal, RiskReasonCode.ZERO_RISK_BUDGET,
                f"Computed risk budget is non-positive ({max_risk_dollars:.2f}); "
                f"check account.equity and risk config",
            )

        # ------------------------------------------------------------------ #
        # 5) Position sizing — floor, never round up; respect max_contracts.
        # ------------------------------------------------------------------ #
        raw_contracts = math.floor(max_risk_dollars / risk_per_contract)
        suggested_contracts = max(0, min(raw_contracts, self.cfg.max_contracts))
        if suggested_contracts < raw_contracts:
            reasons.append(
                f"Sized position clamped to max_contracts={self.cfg.max_contracts} "
                f"(unclamped would have been {raw_contracts})"
            )
        if suggested_contracts < 1:
            codes.append(RiskReasonCode.INSUFFICIENT_SIZE)
            reasons.append(
                f"Risk per contract (${risk_per_contract:.2f}) exceeds the risk "
                f"budget (${max_risk_dollars:.2f}); cannot size even 1 contract"
            )
        dollar_risk = suggested_contracts * risk_per_contract

        # ------------------------------------------------------------------ #
        # 6) Daily trade-count and loss-limit checks (simple, not trailing).
        # ------------------------------------------------------------------ #
        if account.trades_today >= self.cfg.max_trades_per_day:
            codes.append(RiskReasonCode.MAX_TRADES_PER_DAY)
            reasons.append(
                f"Max trades per day reached ({account.trades_today}/"
                f"{self.cfg.max_trades_per_day})"
            )

        loss_limit = self.cfg.daily_loss_limit_dollar
        if account.daily_realized_pnl <= -loss_limit:
            codes.append(RiskReasonCode.DAILY_LOSS_LIMIT_REACHED)
            reasons.append(
                f"Daily loss limit already reached (realized P&L "
                f"{account.daily_realized_pnl:.2f} <= -{loss_limit:.2f})"
            )
        elif account.daily_realized_pnl - max_risk_dollars <= -loss_limit:
            # not yet breached, but this trade's full risk, if lost, would do it
            codes.append(RiskReasonCode.DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED)
            reasons.append(
                f"This trade's max risk (${max_risk_dollars:.2f}) would push "
                f"daily P&L past the -{loss_limit:.2f} limit "
                f"(currently {account.daily_realized_pnl:.2f})"
            )

        outcome = RiskOutcome.REJECTED if codes else RiskOutcome.APPROVED
        if not codes:
            codes = [RiskReasonCode.NONE]

        return RiskDecision(
            outcome=outcome,
            instrument=signal.instrument,
            reason_codes=codes,
            reasons=reasons,
            stop_distance_points=stop_distance_points,
            stop_distance_ticks=stop_distance_ticks,
            risk_per_contract=risk_per_contract,
            max_risk_dollars=max_risk_dollars,
            suggested_contracts=suggested_contracts,
            dollar_risk=dollar_risk,
            risk_reward=list(signal.risk_reward),
            daily_realized_pnl=account.daily_realized_pnl,
            daily_loss_limit_dollar=loss_limit,
            trades_today=account.trades_today,
            max_trades_per_day=self.cfg.max_trades_per_day,
        )

    def _hard_stop(self, signal: Signal, code: RiskReasonCode, reason: str) -> RiskDecision:
        """A rejection where no monetary computation was performed at all
        (unverified spec, unknown instrument, or invalid stop distance) — every
        sizing field stays at its zero default."""
        return RiskDecision(
            outcome=RiskOutcome.REJECTED,
            instrument=signal.instrument,
            reason_codes=[code],
            reasons=[reason],
            risk_reward=list(signal.risk_reward),
            daily_loss_limit_dollar=self.cfg.daily_loss_limit_dollar,
            max_trades_per_day=self.cfg.max_trades_per_day,
        )
