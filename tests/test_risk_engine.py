"""Tests for the Risk Engine (Phase 7): Signal -> Risk Validation -> Position
Size / Risk Decision.

IMPORTANT — about `verified`: the REAL, on-disk contracts.yaml has
`verified: false` (CME access is still blocked; see docs/CME_VERIFICATION.md)
and MUST keep blocking dollar computation — this is asserted directly in
`test_real_registry_is_still_unverified_and_blocks`. Every OTHER test in this
file that needs to exercise sizing arithmetic builds its OWN, purely in-memory
`ContractsFile(verified=True, ...)` (same pattern already used in
tests/test_cme_contract_specs.py::test_require_verified_would_pass_once_flag_is_true).
This is NOT a real CME verification and NEVER touches contracts.yaml — it only
lets us test the math in isolation while the guard itself is exercised,
never bypassed, never reimplemented.

No internet is used anywhere in this file.
"""
from __future__ import annotations

import pandas as pd
import pytest

from signal_engine.config.schema import ContractSpec, ContractsFile, RiskConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState, RiskOutcome, RiskReasonCode
from signal_engine.signals.models import Signal, SignalDirection

TZ = "America/New_York"

# ---- hypothetical, in-memory "verified" specs (NOT real CME verification) - #
_HYPOTHETICAL_SPECS = {
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
}


def _hypothetical_registry() -> ContractRegistry:
    """A LOCAL, in-memory registry with verified=True — used ONLY to test
    sizing arithmetic. Never written to disk, never claims real CME
    verification."""
    return ContractRegistry(ContractsFile(
        verified=True,
        verification={"source": "test-only, NOT a real CME verification", "date": "2099-01-01"},
        contracts=dict(_HYPOTHETICAL_SPECS),
    ))


def _signal(instrument="NQ", direction=SignalDirection.LONG, entry=23450.0,
           stop=23410.0, take_profits=None, risk_reward=None) -> Signal:
    return Signal(
        signal_id="sig-1", instrument=instrument, setup="orb", direction=direction,
        timestamp=pd.Timestamp("2026-06-15 10:00", tz=TZ), timeframe="5m",
        entry=entry, stop=stop,
        take_profits=take_profits if take_profits is not None else [23510.0, 23570.0],
        risk_reward=risk_reward if risk_reward is not None else [1.5, 3.0],
        reasons=["test"],
    )


@pytest.fixture
def registry() -> ContractRegistry:
    return _hypothetical_registry()


# --------------------------------------------------------------------------- #
# Real registry: verified=false MUST block (the actual, critical safety test)
# --------------------------------------------------------------------------- #
def test_real_registry_is_still_unverified_and_blocks():
    real_registry = ContractRegistry.load()
    assert real_registry.verified is False    # sanity: still blocked as of Phase 6.5

    engine = RiskEngine(RiskConfig(), real_registry)
    decision = engine.evaluate(_signal(), AccountState(equity=100_000.0))

    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.reason_codes == [RiskReasonCode.UNVERIFIED_CONTRACT_SPEC]
    assert "CME_VERIFICATION.md" in decision.reasons[0]
    # no monetary computation was performed at all
    assert decision.suggested_contracts == 0
    assert decision.risk_per_contract == 0.0
    assert decision.dollar_risk == 0.0
    assert decision.max_risk_dollars == 0.0


def test_unverified_block_applies_to_all_four_instruments():
    real_registry = ContractRegistry.load()
    engine = RiskEngine(RiskConfig(), real_registry)
    for symbol in ("ES", "NQ", "MES", "MNQ"):
        decision = engine.evaluate(_signal(instrument=symbol), AccountState(equity=50_000.0))
        assert decision.outcome is RiskOutcome.REJECTED
        assert decision.reason_codes == [RiskReasonCode.UNVERIFIED_CONTRACT_SPEC]


def test_require_verified_is_actually_called_not_reimplemented(registry, monkeypatch):
    """Prove the engine calls the real guard method (not a private re-check of
    `.verified`) by monkeypatching require_verified() to raise unconditionally
    even though verified=True."""
    def boom():
        raise RuntimeError("guard forced to fail for this test")
    monkeypatch.setattr(registry, "require_verified", boom)
    engine = RiskEngine(RiskConfig(), registry)
    decision = engine.evaluate(_signal(), AccountState(equity=100_000.0))
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.reason_codes == [RiskReasonCode.UNVERIFIED_CONTRACT_SPEC]
    assert "guard forced to fail" in decision.reasons[0]


# --------------------------------------------------------------------------- #
# Risk per contract / sizing math — ES, NQ, MES, MNQ (hypothetical registry)
# --------------------------------------------------------------------------- #
def test_risk_per_contract_nq_matches_worked_example(registry):
    """docs/RISK_MANAGEMENT.md worked example: NQ, 40pt stop -> $800/contract."""
    engine = RiskEngine(RiskConfig(risk_mode="percent", risk_pct=0.01,
                                   daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="NQ", entry=23450.0, stop=23410.0)   # 40 pts
    decision = engine.evaluate(sig, AccountState(equity=100_000.0))
    assert decision.outcome is RiskOutcome.APPROVED
    assert decision.stop_distance_points == pytest.approx(40.0)
    assert decision.stop_distance_ticks == pytest.approx(160.0)
    assert decision.risk_per_contract == pytest.approx(800.0)     # 40 * $20
    assert decision.max_risk_dollars == pytest.approx(1000.0)     # 1% of 100k
    assert decision.suggested_contracts == 1                      # floor(1000/800)
    assert decision.dollar_risk == pytest.approx(800.0)


def test_risk_per_contract_es_matches_worked_example(registry):
    """docs/RISK_MANAGEMENT.md worked example: ES, 6pt stop, 0.5% of $50k."""
    engine = RiskEngine(RiskConfig(risk_mode="percent", risk_pct=0.005,
                                   daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="ES", entry=5000.00, stop=5006.00, direction=SignalDirection.SHORT,
                 take_profits=[4990.0], risk_reward=[1.67])
    decision = engine.evaluate(sig, AccountState(equity=50_000.0))
    assert decision.risk_per_contract == pytest.approx(300.0)     # 6 * $50
    assert decision.max_risk_dollars == pytest.approx(250.0)      # 0.5% of 50k
    assert decision.suggested_contracts == 0                      # floor(250/300) = 0
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.INSUFFICIENT_SIZE in decision.reason_codes


def test_risk_per_contract_mes_sizes_where_es_could_not(registry):
    """Same ES scenario, but sized in MES (1/10th multiplier) — same
    instrument-agnostic engine, just a different `instrument` on the Signal."""
    engine = RiskEngine(RiskConfig(risk_mode="percent", risk_pct=0.005,
                                   daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="MES", entry=5000.00, stop=5006.00, direction=SignalDirection.SHORT,
                 take_profits=[4990.0], risk_reward=[1.67])
    decision = engine.evaluate(sig, AccountState(equity=50_000.0))
    assert decision.risk_per_contract == pytest.approx(30.0)      # 6 * $5
    assert decision.max_risk_dollars == pytest.approx(250.0)
    assert decision.suggested_contracts == 8                      # floor(250/30) = 8
    assert decision.dollar_risk == pytest.approx(240.0)
    assert decision.outcome is RiskOutcome.APPROVED


def test_risk_per_contract_mnq(registry):
    engine = RiskEngine(RiskConfig(risk_mode="fixed", fixed_risk_dollar=100.0,
                                   daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="MNQ", entry=20000.0, stop=19980.0)   # 20 pts
    decision = engine.evaluate(sig, AccountState(equity=10_000.0))
    assert decision.risk_per_contract == pytest.approx(40.0)      # 20 * $2
    assert decision.max_risk_dollars == pytest.approx(100.0)
    assert decision.suggested_contracts == 2                      # floor(100/40) = 2
    assert decision.dollar_risk == pytest.approx(80.0)


# --------------------------------------------------------------------------- #
# fixed-dollar vs percentage-of-account modes
# --------------------------------------------------------------------------- #
def test_fixed_dollar_mode_ignores_equity(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=400.0, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    d1 = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=10_000.0))
    d2 = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=1_000_000.0))
    assert d1.max_risk_dollars == d2.max_risk_dollars == 400.0


def test_percent_mode_scales_with_equity(registry):
    cfg = RiskConfig(risk_mode="percent", risk_pct=0.02, daily_loss_limit_dollar=50_000)
    engine = RiskEngine(cfg, registry)
    small = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=10_000.0))
    large = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=100_000.0))
    assert small.max_risk_dollars == pytest.approx(200.0)
    assert large.max_risk_dollars == pytest.approx(2000.0)


def test_max_risk_per_trade_dollar_caps_percent_mode(registry):
    cfg = RiskConfig(risk_mode="percent", risk_pct=0.05,
                     max_risk_per_trade_dollar=300.0, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    # 5% of 100k would be $5000, but the hard ceiling caps it to $300
    decision = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=100_000.0))
    assert decision.max_risk_dollars == pytest.approx(300.0)


# --------------------------------------------------------------------------- #
# max_contracts clamp
# --------------------------------------------------------------------------- #
def test_max_contracts_clamps_large_size(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=1_000_000.0,
                     max_contracts=3, daily_loss_limit_dollar=10_000_000)
    engine = RiskEngine(cfg, registry)
    sig = _signal(instrument="MNQ", entry=20000.0, stop=19990.0)   # 10 pts -> $20/contract
    decision = engine.evaluate(sig, AccountState(equity=1_000_000.0))
    assert decision.suggested_contracts == 3       # clamped, not the raw huge number
    assert any("clamped" in r.lower() for r in decision.reasons)
    assert decision.outcome is RiskOutcome.APPROVED   # clamping alone isn't a rejection


# --------------------------------------------------------------------------- #
# daily loss limit
# --------------------------------------------------------------------------- #
def test_daily_loss_limit_already_reached_blocks(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=100.0, daily_loss_limit_dollar=500.0)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, daily_realized_pnl=-500.0)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED in decision.reason_codes


def test_daily_loss_limit_would_be_exceeded_blocks(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=200.0, daily_loss_limit_dollar=500.0)
    engine = RiskEngine(cfg, registry)
    # already down $400; one more $200 trade would push to -$600, past -$500
    account = AccountState(equity=100_000.0, daily_realized_pnl=-400.0)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.DAILY_LOSS_LIMIT_WOULD_BE_EXCEEDED in decision.reason_codes


def test_daily_loss_limit_not_yet_reached_allows(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=800.0, daily_loss_limit_dollar=2000.0)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, daily_realized_pnl=-200.0)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.APPROVED


def test_positive_daily_pnl_never_blocks(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=800.0, daily_loss_limit_dollar=500.0)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, daily_realized_pnl=2000.0)   # having a good day
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.APPROVED


# --------------------------------------------------------------------------- #
# max trades per day
# --------------------------------------------------------------------------- #
def test_max_trades_per_day_blocks(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=100.0,
                     max_trades_per_day=3, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, trades_today=3)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.MAX_TRADES_PER_DAY in decision.reason_codes


def test_below_max_trades_per_day_allows(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=800.0,
                     max_trades_per_day=3, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, trades_today=2)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.APPROVED


def test_multiple_reasons_accumulate(registry):
    """Both max-trades AND daily-loss-limit can fail together; both reasons
    must be reported (not just the first)."""
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=100.0,
                     max_trades_per_day=2, daily_loss_limit_dollar=300.0)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, trades_today=5, daily_realized_pnl=-300.0)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.REJECTED
    assert RiskReasonCode.MAX_TRADES_PER_DAY in decision.reason_codes
    assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED in decision.reason_codes
    assert len(decision.reasons) >= 2


# --------------------------------------------------------------------------- #
# invalid stop / zero / negative risk
# --------------------------------------------------------------------------- #
def test_invalid_stop_zero_distance_is_rejected(registry):
    engine = RiskEngine(RiskConfig(), registry)
    sig = _signal(instrument="NQ", entry=23450.0, stop=23450.0)   # entry == stop
    decision = engine.evaluate(sig, AccountState(equity=100_000.0))
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.reason_codes == [RiskReasonCode.INVALID_STOP]
    assert decision.suggested_contracts == 0


def test_negative_distance_is_impossible_by_construction_but_abs_is_safe(registry):
    """Even if a SHORT signal has stop below entry by mistake, the engine uses
    abs(entry-stop) and still produces a valid positive distance rather than a
    negative risk figure."""
    engine = RiskEngine(RiskConfig(daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="NQ", direction=SignalDirection.SHORT,
                 entry=23450.0, stop=23400.0)  # "wrong side" for SHORT, but still != entry
    decision = engine.evaluate(sig, AccountState(equity=100_000.0))
    assert decision.stop_distance_points == pytest.approx(50.0)
    assert decision.risk_per_contract > 0


def test_zero_equity_percent_mode_is_rejected(registry):
    cfg = RiskConfig(risk_mode="percent", risk_pct=0.01)
    engine = RiskEngine(cfg, registry)
    decision = engine.evaluate(_signal(instrument="NQ"), AccountState(equity=0.0))
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.reason_codes == [RiskReasonCode.ZERO_RISK_BUDGET]


def test_unknown_instrument_is_rejected(registry):
    engine = RiskEngine(RiskConfig(), registry)
    decision = engine.evaluate(_signal(instrument="RTY"), AccountState(equity=100_000.0))
    assert decision.outcome is RiskOutcome.REJECTED
    assert decision.reason_codes == [RiskReasonCode.UNKNOWN_INSTRUMENT]


# --------------------------------------------------------------------------- #
# deterministic results
# --------------------------------------------------------------------------- #
def test_same_inputs_produce_identical_decision_across_fresh_engines(registry):
    cfg = RiskConfig(risk_mode="percent", risk_pct=0.01, daily_loss_limit_dollar=5000)
    sig = _signal(instrument="NQ")
    account = AccountState(equity=100_000.0, daily_realized_pnl=-50.0, trades_today=1)
    d1 = RiskEngine(cfg, _hypothetical_registry()).evaluate(sig, account)
    d2 = RiskEngine(cfg, _hypothetical_registry()).evaluate(sig, account)
    assert d1 == d2


def test_repeated_calls_on_same_engine_are_identical(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=250.0, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    sig = _signal(instrument="ES")
    account = AccountState(equity=75_000.0)
    results = [engine.evaluate(sig, account) for _ in range(5)]
    assert all(r == results[0] for r in results)


# --------------------------------------------------------------------------- #
# no-lookahead / purity
# --------------------------------------------------------------------------- #
def test_decision_depends_only_on_given_account_state_not_hidden_state(registry):
    """A later call with an updated (e.g. "future") AccountState must not
    retroactively change a decision computed with an earlier AccountState —
    the engine holds no state of its own between calls."""
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=800.0, daily_loss_limit_dollar=1000.0)
    engine = RiskEngine(cfg, registry)
    sig = _signal(instrument="NQ")

    early_account = AccountState(equity=100_000.0, daily_realized_pnl=0.0, trades_today=0)
    early_decision = engine.evaluate(sig, early_account)
    assert early_decision.outcome is RiskOutcome.APPROVED

    # simulate time passing: the account now reflects a big realized loss
    later_account = AccountState(equity=100_000.0, daily_realized_pnl=-500.0, trades_today=4)
    later_decision = engine.evaluate(sig, later_account)
    assert later_decision.outcome is RiskOutcome.REJECTED

    # re-evaluating with the ORIGINAL (early) account again gives the SAME
    # result as before — proving the engine never absorbed the later state
    replay_decision = engine.evaluate(sig, early_account)
    assert replay_decision == early_decision


def test_engine_never_mutates_its_inputs(registry):
    cfg = RiskConfig(risk_mode="percent", risk_pct=0.01, daily_loss_limit_dollar=5000)
    engine = RiskEngine(cfg, registry)
    sig = _signal(instrument="NQ")
    account = AccountState(equity=100_000.0)
    before = (sig.entry, sig.stop, account.equity, account.trades_today)
    engine.evaluate(sig, account)
    after = (sig.entry, sig.stop, account.equity, account.trades_today)
    assert before == after


# --------------------------------------------------------------------------- #
# risk_reward is carried through unchanged (no recomputation/homogenization)
# --------------------------------------------------------------------------- #
def test_risk_reward_carried_through_unchanged(registry):
    engine = RiskEngine(RiskConfig(daily_loss_limit_dollar=5000), registry)
    sig = _signal(instrument="NQ", risk_reward=[1.23, 4.56])
    decision = engine.evaluate(sig, AccountState(equity=100_000.0))
    assert decision.risk_reward == [1.23, 4.56]


def test_daily_caps_off_when_none(registry):
    cfg = RiskConfig(risk_mode="fixed", fixed_risk_dollar=800.0,
                     max_trades_per_day=None, daily_loss_limit_dollar=None)
    engine = RiskEngine(cfg, registry)
    account = AccountState(equity=100_000.0, trades_today=50, daily_realized_pnl=-9000.0)
    decision = engine.evaluate(_signal(instrument="NQ"), account)
    assert decision.outcome is RiskOutcome.APPROVED
