"""Risk Engine demo (Phase 7).

Signal -> Risk Validation -> Position Size / Risk Decision

Shows TWO scenarios, deliberately kept separate:

  1) The REAL, on-disk ContractRegistry (contracts.yaml, verified=false as of
     Phase 6.5) — this MUST block every symbol with UNVERIFIED_CONTRACT_SPEC.
     This is the actual, current state of the project and the safety
     mechanism working as intended.

  2) A LOCAL, in-memory "hypothetically verified" registry — used ONLY to
     demonstrate the position-sizing arithmetic (matching the worked examples
     in docs/RISK_MANAGEMENT.md). This NEVER touches contracts.yaml and is
     NOT a claim that CME Group has been verified.
"""
from __future__ import annotations

import pandas as pd

from signal_engine.config.schema import ContractSpec, ContractsFile, RiskConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState
from signal_engine.signals.models import Signal, SignalDirection

TZ = "America/New_York"


def hr(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


def make_signal(instrument, direction, entry, stop, take_profits, risk_reward) -> Signal:
    return Signal(
        signal_id=f"demo-{instrument}", instrument=instrument, setup="orb",
        direction=direction, timestamp=pd.Timestamp("2026-06-15 10:00", tz=TZ),
        timeframe="5m", entry=entry, stop=stop, take_profits=take_profits,
        risk_reward=risk_reward, reasons=["demo signal"],
    )


def show(decision) -> None:
    print(f"  outcome={decision.outcome.value}  reason_codes={[c.value for c in decision.reason_codes]}")
    if decision.reasons:
        for r in decision.reasons:
            print(f"    - {r}")
    print(f"  stop_distance_points={decision.stop_distance_points:.2f} "
         f"stop_distance_ticks={decision.stop_distance_ticks:.1f}")
    print(f"  risk_per_contract=${decision.risk_per_contract:.2f}  "
         f"max_risk_dollars=${decision.max_risk_dollars:.2f}")
    print(f"  suggested_contracts={decision.suggested_contracts}  "
         f"dollar_risk=${decision.dollar_risk:.2f}")
    print(f"  daily: pnl={decision.daily_realized_pnl:.2f} / limit=-{decision.daily_loss_limit_dollar:.2f}  "
         f"trades_today={decision.trades_today}/{decision.max_trades_per_day}")


def main() -> None:
    hr("1) REAL registry (contracts.yaml) — verified=false, MUST block")
    real_registry = ContractRegistry.load()
    print(f"registry.verified = {real_registry.verified}")
    engine_real = RiskEngine(RiskConfig(), real_registry)
    for symbol in ("ES", "NQ", "MES", "MNQ"):
        sig = make_signal(symbol, SignalDirection.LONG, 100.0, 99.0, [102.0], [2.0])
        decision = engine_real.evaluate(sig, AccountState(equity=100_000.0))
        print(f"\n[{symbol}]")
        show(decision)

    hr("2) HYPOTHETICAL verified registry (in-memory only, NOT real CME verification)")
    hypothetical = ContractRegistry(ContractsFile(
        verified=True,
        verification={"source": "demo-only, NOT a real CME verification", "date": "2099-01-01"},
        contracts={
            "ES": ContractSpec(symbol="ES", name="E-mini S&P 500", multiplier=50.0,
                               tick_size=0.25, tick_value=12.50, months=["H", "M", "U", "Z"]),
            "NQ": ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", multiplier=20.0,
                               tick_size=0.25, tick_value=5.00, months=["H", "M", "U", "Z"]),
            "MES": ContractSpec(symbol="MES", name="Micro E-mini S&P 500", multiplier=5.0,
                                tick_size=0.25, tick_value=1.25, months=["H", "M", "U", "Z"],
                                is_micro=True, micro_of="ES"),
            "MNQ": ContractSpec(symbol="MNQ", name="Micro E-mini Nasdaq-100", multiplier=2.0,
                                tick_size=0.25, tick_value=0.50, months=["H", "M", "U", "Z"],
                                is_micro=True, micro_of="NQ"),
        },
    ))

    print("\n--- NQ LONG, percent mode 1% of $100k (docs/RISK_MANAGEMENT.md example) ---")
    engine = RiskEngine(RiskConfig(risk_mode="percent", risk_pct=0.01,
                                   daily_loss_limit_dollar=5000), hypothetical)
    sig = make_signal("NQ", SignalDirection.LONG, 23450.0, 23410.0, [23510.0, 23570.0], [1.5, 3.0])
    show(engine.evaluate(sig, AccountState(equity=100_000.0)))

    print("\n--- ES SHORT, percent mode 0.5% of $50k -> too small for ES (docs example) ---")
    engine = RiskEngine(RiskConfig(risk_mode="percent", risk_pct=0.005,
                                   daily_loss_limit_dollar=5000), hypothetical)
    sig = make_signal("ES", SignalDirection.SHORT, 5000.0, 5006.0, [4990.0], [1.67])
    show(engine.evaluate(sig, AccountState(equity=50_000.0)))

    print("\n--- same ES scenario, sized in MES instead (fits) ---")
    sig = make_signal("MES", SignalDirection.SHORT, 5000.0, 5006.0, [4990.0], [1.67])
    show(engine.evaluate(sig, AccountState(equity=50_000.0)))

    print("\n--- daily loss limit already reached -> NO_TRADE ---")
    engine = RiskEngine(RiskConfig(risk_mode="fixed", fixed_risk_dollar=200.0,
                                   daily_loss_limit_dollar=500.0), hypothetical)
    sig = make_signal("NQ", SignalDirection.LONG, 23450.0, 23410.0, [23510.0], [1.5])
    show(engine.evaluate(sig, AccountState(equity=100_000.0, daily_realized_pnl=-500.0)))

    print("\n--- max trades per day reached -> NO_TRADE ---")
    engine = RiskEngine(RiskConfig(risk_mode="fixed", fixed_risk_dollar=200.0,
                                   max_trades_per_day=3, daily_loss_limit_dollar=5000), hypothetical)
    show(engine.evaluate(sig, AccountState(equity=100_000.0, trades_today=3)))

    print("\n--- invalid stop (entry == stop) -> NO_TRADE ---")
    engine = RiskEngine(RiskConfig(), hypothetical)
    bad_sig = make_signal("NQ", SignalDirection.LONG, 23450.0, 23450.0, [23510.0], [1.5])
    show(engine.evaluate(bad_sig, AccountState(equity=100_000.0)))


if __name__ == "__main__":
    main()
