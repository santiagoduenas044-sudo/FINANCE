"""Dollar-denominated trade result (Phase 8) — the gated pipeline.

Only ever called for a trade that (a) resolved WIN or LOSS (a known exit
price) and (b) whose Signal was APPROVED by the RiskEngine — which itself
never computes anything unless `ContractRegistry.require_verified()`
succeeds (Phase 7). This module adds NO new verification bypass: it simply
will not be invoked at all when specs are unverified, because the
RiskDecision it depends on will already be REJECTED/UNVERIFIED_CONTRACT_SPEC
in that case (see BacktestEngine).
"""
from __future__ import annotations

from ..config.schema import ContractSpec
from ..risk.models import RiskDecision, RiskOutcome
from ..signals.models import SignalDirection
from .models import ClosedTrade, CostModel, DollarResult, TradeOutcome


def compute_dollar_result(
    trade: ClosedTrade, risk_decision: RiskDecision, spec: ContractSpec,
    cost_model: CostModel,
) -> DollarResult | None:
    if trade.outcome not in (TradeOutcome.WIN, TradeOutcome.LOSS):
        return None                      # unresolved trades have no known exit
    if risk_decision.outcome is not RiskOutcome.APPROVED:
        return None                      # unverified specs / sizing failed -> no $ computed
    if risk_decision.suggested_contracts < 1:
        return None

    bullish = trade.direction is SignalDirection.LONG
    slip = cost_model.slippage_ticks * spec.tick_size

    if trade.outcome is TradeOutcome.WIN:
        raw_exit = trade.take_profit
        exit_fill = raw_exit - slip if bullish else raw_exit + slip   # slippage hurts fills
    else:
        raw_exit = trade.stop
        exit_fill = raw_exit - slip if bullish else raw_exit + slip

    entry_fill = trade.entry + slip if bullish else trade.entry - slip  # slippage hurts entry too

    price_move = (exit_fill - entry_fill) if bullish else (entry_fill - exit_fill)
    gross_pnl = spec.points_to_dollars(price_move) * risk_decision.suggested_contracts
    commission_total = (cost_model.commission_per_contract_per_side * 2
                        * risk_decision.suggested_contracts)
    net_pnl = gross_pnl - commission_total

    return DollarResult(
        suggested_contracts=risk_decision.suggested_contracts,
        entry_fill=entry_fill,
        exit_fill=exit_fill,
        gross_pnl=gross_pnl,
        commission_total=commission_total,
        net_pnl=net_pnl,
    )
