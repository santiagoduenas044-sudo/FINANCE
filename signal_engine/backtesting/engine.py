"""BacktestEngine (Phase 8): replays history bar-by-bar through the EXACT
SAME SetupEngine + SignalEngine (+ optional RiskEngine) used for live/paper —
no reimplementation, no duplicated detection/validation logic.

Setup Engine -> Signal Engine -> (optional) Risk Engine -> trade outcome resolution -> metrics

No-lookahead: each decision bar N gets an AnalysisContext built the same way
`SetupEngine.scan()` builds it (via `DataEngine.analysis_context`, sliced to
<= N). Signal generation NEVER sees bars beyond N. Trade OUTCOME resolution
(simulator.py) legitimately reads bars after N — that is not a lookahead
violation, it is what "what happened to this trade" means; see
docs/PHASE8_BACKTESTING.md.

This module does not execute orders, connect to a broker, or import
signal_engine.output.
"""
from __future__ import annotations

from typing import Iterable

import pandas as pd

from ..data.contracts import ContractRegistry
from ..data.engine import DataEngine
from ..risk.engine import RiskEngine
from ..risk.models import AccountState
from ..setups.engine import SetupEngine
from ..signals.engine import SignalEngine
from ..signals.models import Signal
from .dollars import compute_dollar_result
from .metrics import compute_metrics
from .models import BacktestResult, ClosedTrade, CostModel
from .simulator import resolve_trade_outcome


class BacktestEngine:
    def __init__(
        self,
        data_engine: DataEngine,
        setup_engine: SetupEngine,
        signal_engine: SignalEngine,
        registry: ContractRegistry,
        risk_engine: RiskEngine | None = None,
        account: AccountState | None = None,
        cost_model: CostModel | None = None,
    ):
        self.data_engine = data_engine
        self.setup_engine = setup_engine
        self.signal_engine = signal_engine
        self.registry = registry
        self.risk_engine = risk_engine
        self.account = account
        self.cost_model = cost_model or CostModel()

    def run(
        self, symbol: str, tf_bars: dict[str, pd.DataFrame],
        decision_times: Iterable[pd.Timestamp],
    ) -> BacktestResult:
        trades: list[ClosedTrade] = []
        no_trade_count = 0

        for N in decision_times:
            ctx = self.data_engine.analysis_context(symbol, N, tf_bars)
            candidates = self.setup_engine.detect(ctx)
            if not candidates:
                continue

            for result in self.signal_engine.evaluate_candidates(ctx, candidates):
                if not isinstance(result, Signal):
                    no_trade_count += 1
                    continue
                trades.append(self._resolve(result, tf_bars))

        metrics = compute_metrics(trades, no_trade_count=no_trade_count)
        return BacktestResult(symbol=symbol, trades=trades, metrics=metrics)

    def _resolve(self, signal: Signal, tf_bars: dict[str, pd.DataFrame]) -> ClosedTrade:
        bars = tf_bars[signal.timeframe]
        trade = resolve_trade_outcome(signal, bars)

        if self.risk_engine is None or self.account is None:
            return trade

        risk_decision = self.risk_engine.evaluate(signal, self.account)
        try:
            spec = self.registry.spec(signal.instrument)
        except KeyError:
            spec = None

        dollar = None
        if spec is not None:
            dollar = compute_dollar_result(trade, risk_decision, spec, self.cost_model)

        return ClosedTrade(
            signal_id=trade.signal_id, instrument=trade.instrument, setup=trade.setup,
            direction=trade.direction, entry_timestamp=trade.entry_timestamp,
            exit_timestamp=trade.exit_timestamp, entry=trade.entry, stop=trade.stop,
            take_profit=trade.take_profit, outcome=trade.outcome,
            r_multiple=trade.r_multiple, bars_held=trade.bars_held,
            risk_decision=risk_decision, dollar=dollar,
        )
