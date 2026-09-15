"""Backtesting Engine demo (Phase 8).

Setup Engine -> Signal Engine -> (optional) Risk Engine -> trade resolution -> metrics

Shows TWO runs over the same sample data:
  1) R-multiple only, using the REAL on-disk ContractRegistry (verified=false)
     — proves the CME block does NOT stop R-based backtesting.
  2) Full dollar pipeline, using a LOCAL "hypothetically verified" registry
     (NOT a real CME verification — see docs/CME_VERIFICATION.md).
"""
from __future__ import annotations

from pathlib import Path

from signal_engine.config.schema import ContractSpec, ContractsFile, RiskConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from signal_engine.backtesting.engine import BacktestEngine
from signal_engine.backtesting.models import CostModel

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def hr(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def show(result) -> None:
    m = result.metrics
    print(f"signals={m.total_signals}  no_trade={m.no_trade_count}  "
         f"resolved={m.resolved_trades} (wins={m.wins} losses={m.losses} "
         f"expired={m.expired} open={m.open_trades})")
    print(f"win_rate={m.win_rate:.2%}  expectancy_r={m.expectancy_r:.2f}  "
         f"net_r={m.net_r:.2f}  profit_factor={m.profit_factor:.2f}")
    print(f"max_drawdown_r={m.max_drawdown_r:.2f}  sharpe_r={m.sharpe_r:.2f}  "
         f"sortino_r={m.sortino_r:.2f}")
    print(f"max_consecutive_wins={m.max_consecutive_wins}  "
         f"max_consecutive_losses={m.max_consecutive_losses}")
    print("by_setup:", {k: (v.trades, f"{v.win_rate:.0%}", round(v.net_r, 2))
                        for k, v in m.by_setup.items()})
    if m.dollar_metrics_reason:
        print(f"dollar metrics: NOT COMPUTED — {m.dollar_metrics_reason}")
    else:
        print(f"net_pnl_dollars=${m.net_pnl_dollars:,.2f}  "
             f"max_drawdown_dollars=${m.max_drawdown_dollars:,.2f}")
        print(f"avg_win_dollars=${m.avg_win_dollars:,.2f}  "
             f"avg_loss_dollars=${m.avg_loss_dollars:,.2f}  "
             f"total_commission=${m.total_commission_dollars:,.2f}")


def main() -> None:
    data_engine = DataEngine()
    provider = CsvProvider(SAMPLES)
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)
    dts = list(lc.timeframes["5m"].index[10:])

    hr("1) R-multiple only — REAL registry (contracts.yaml, verified=false)")
    real_registry = ContractRegistry.load()
    print(f"registry.verified = {real_registry.verified}")
    setup_engine = SetupEngine(data_engine.cfg, real_registry)
    signal_engine = SignalEngine(data_engine.cfg.signals)
    bt = BacktestEngine(data_engine, setup_engine, signal_engine, real_registry)
    show(bt.run("ES", lc.timeframes, dts))

    hr("2) Full dollar pipeline — HYPOTHETICAL verified registry (NOT real CME verification)")
    hypothetical = ContractRegistry(ContractsFile(
        verified=True,
        verification={"source": "demo-only, NOT a real CME verification", "date": "2099-01-01"},
        contracts={"ES": ContractSpec(symbol="ES", name="E-mini S&P 500", multiplier=50.0,
                                      tick_size=0.25, tick_value=12.50,
                                      months=["H", "M", "U", "Z"])},
    ))
    setup_engine2 = SetupEngine(data_engine.cfg, hypothetical)
    signal_engine2 = SignalEngine(data_engine.cfg.signals)
    risk_engine = RiskEngine(RiskConfig(risk_mode="fixed", fixed_risk_dollar=1000.0,
                                        daily_loss_limit_dollar=1_000_000,
                                        max_trades_per_day=1000), hypothetical)
    account = AccountState(equity=200_000.0)
    cost_model = CostModel(slippage_ticks=1.0, commission_per_contract_per_side=2.25)
    bt2 = BacktestEngine(data_engine, setup_engine2, signal_engine2, hypothetical,
                         risk_engine=risk_engine, account=account, cost_model=cost_model)
    show(bt2.run("ES", lc.timeframes, dts))


if __name__ == "__main__":
    main()
