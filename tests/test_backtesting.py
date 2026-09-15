"""Tests for the Backtesting Engine (Phase 8).

Setup Engine -> Signal Engine -> (optional) Risk Engine -> trade resolution -> metrics

No internet. No execution/broker. Dollar-pipeline tests use a LOCAL, in-memory
`ContractsFile(verified=True, ...)` (same pattern as Phases 6.5/7) — never a
real CME verification, never touches contracts.yaml.
"""
from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import ContractSpec, ContractsFile, RiskConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.models import SESSION, TRADING_DATE
from signal_engine.risk.engine import RiskEngine
from signal_engine.risk.models import AccountState, RiskOutcome
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from signal_engine.signals.models import Signal, SignalDirection
from signal_engine.backtesting.dollars import compute_dollar_result
from signal_engine.backtesting.engine import BacktestEngine
from signal_engine.backtesting.metrics import compute_metrics
from signal_engine.backtesting.models import ClosedTrade, CostModel, TradeOutcome
from signal_engine.backtesting.simulator import resolve_trade_outcome
from signal_engine.risk.models import RiskDecision, RiskReasonCode
from tests.conftest import FakeProvider, make_rth_1m, weekdays

TZ = "America/New_York"


def _bars(times, ohlc):
    idx = pd.DatetimeIndex([pd.Timestamp(t, tz=TZ) for t in times], name="timestamp")
    df = pd.DataFrame(ohlc, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 100.0
    return df


def _signal(direction=SignalDirection.LONG, entry=100.0, stop=98.0,
           take_profits=None, risk_reward=None, expiration=None,
           timestamp=None) -> Signal:
    ts = timestamp or pd.Timestamp("2026-06-15 10:00", tz=TZ)
    return Signal(
        signal_id="s1", instrument="NQ", setup="orb", direction=direction,
        timestamp=ts, timeframe="5m", entry=entry, stop=stop,
        take_profits=take_profits if take_profits is not None else [104.0],
        risk_reward=risk_reward if risk_reward is not None else [2.0],
        reasons=["test"], expiration=expiration,
    )


# --------------------------------------------------------------------------- #
# simulator: WIN / LOSS / EXPIRED / OPEN / same-bar tie-break
# --------------------------------------------------------------------------- #
def test_win_resolved_at_first_tp_touch():
    sig = _signal()
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05", "2026-06-15 10:10"],
                [(100, 101, 99, 100), (100, 103, 99, 101), (101, 105, 100, 104)])
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.WIN
    assert trade.r_multiple == 2.0
    assert trade.bars_held == 2
    assert trade.exit_timestamp == bars.index[-1]


def test_loss_resolved_at_first_stop_touch():
    sig = _signal()
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 101, 99, 100), (100, 101, 97, 98)])
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.LOSS
    assert trade.r_multiple == -1.0
    assert trade.bars_held == 1


def test_open_when_data_runs_out_unresolved():
    sig = _signal()
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 101, 99, 100), (100, 102, 99.5, 101)])
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.OPEN
    assert trade.r_multiple is None
    assert trade.exit_timestamp is None


def test_expired_when_horizon_passes_unresolved():
    sig = _signal(expiration=pd.Timestamp("2026-06-15 10:05", tz=TZ))
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05", "2026-06-15 10:10"],
                [(100, 101, 99, 100), (100, 102, 99.5, 101), (101, 110, 101, 109)])
    trade = resolve_trade_outcome(sig, bars)   # would WIN at 10:10 but that's past expiration
    assert trade.outcome is TradeOutcome.EXPIRED
    assert trade.exit_timestamp == sig.expiration


def test_both_touched_same_bar_assumes_stop_first_conservatively():
    sig = _signal()   # entry 100, stop 98, tp 104
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 101, 99, 100), (100, 105, 97, 101)])   # bar hits both 98 and 104
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.LOSS
    assert trade.r_multiple == -1.0


def test_short_direction_resolves_symmetrically():
    sig = _signal(direction=SignalDirection.SHORT, entry=100.0, stop=102.0,
                 take_profits=[96.0], risk_reward=[2.0])
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 101, 99, 100), (99, 100, 95, 96)])
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.WIN
    assert trade.r_multiple == 2.0


def test_no_take_profit_never_wins_only_loss_or_open():
    sig = _signal(take_profits=[], risk_reward=[])
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 200, 99, 150), (150, 200, 97, 98)])   # huge favorable move, then stop
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.LOSS   # no TP to hit, only the stop matters


def test_ignores_bars_at_or_before_signal_timestamp():
    """A bar AT signal.timestamp (or earlier) must never be used for resolution
    — only strictly-after bars count."""
    sig = _signal()
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05"],
                [(100, 200, 50, 104), (100, 101, 99.5, 100)])   # the TP-hitting bar IS the signal bar
    trade = resolve_trade_outcome(sig, bars)
    assert trade.outcome is TradeOutcome.OPEN   # the only qualifying future bar doesn't resolve it


# --------------------------------------------------------------------------- #
# metrics: hand-crafted trade sequences
# --------------------------------------------------------------------------- #
def _trade(outcome, r, ts, instrument="NQ", setup="orb",
          direction=SignalDirection.LONG) -> ClosedTrade:
    return ClosedTrade(
        signal_id=f"id-{ts}", instrument=instrument, setup=setup, direction=direction,
        entry_timestamp=pd.Timestamp(ts, tz=TZ), exit_timestamp=None,
        entry=100.0, stop=98.0, take_profit=104.0, outcome=outcome,
        r_multiple=r, bars_held=1,
    )


def test_metrics_win_rate_and_expectancy():
    trades = [
        _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00"),
        _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:05"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:10"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:15"),
    ]
    m = compute_metrics(trades, no_trade_count=3)
    assert m.resolved_trades == 4
    assert m.wins == 2 and m.losses == 2
    assert m.win_rate == pytest.approx(0.5)
    assert m.avg_win_r == pytest.approx(2.0)
    assert m.avg_loss_r == pytest.approx(1.0)
    assert m.profit_factor == pytest.approx(4.0 / 2.0)
    assert m.expectancy_r == pytest.approx((2 + 2 - 1 - 1) / 4)
    assert m.net_r == pytest.approx(2.0)
    assert m.no_trade_count == 3
    assert m.total_signals == 4


def test_metrics_expired_and_open_excluded_from_win_rate():
    trades = [
        _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00"),
        _trade(TradeOutcome.EXPIRED, None, "2026-06-15 10:05"),
        _trade(TradeOutcome.OPEN, None, "2026-06-15 10:10"),
    ]
    m = compute_metrics(trades)
    assert m.resolved_trades == 1
    assert m.win_rate == 1.0
    assert m.expired == 1
    assert m.open_trades == 1


def test_metrics_max_drawdown_r():
    # equity curve in R: +2, +2, -1, -1, -1 -> peak 4, trough 1 -> dd = 3
    trades = [
        _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00"),
        _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:05"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:10"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:15"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:20"),
    ]
    m = compute_metrics(trades)
    assert m.max_drawdown_r == pytest.approx(3.0)


def test_metrics_consecutive_streaks():
    trades = [
        _trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:00"),
        _trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:05"),
        _trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:10"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:15"),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:20"),
        _trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:25"),
    ]
    m = compute_metrics(trades)
    assert m.max_consecutive_wins == 3
    assert m.max_consecutive_losses == 2


def test_metrics_group_by_instrument_setup_direction():
    trades = [
        _trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:00", instrument="ES", setup="orb",
              direction=SignalDirection.LONG),
        _trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:05", instrument="NQ", setup="vwap_reclaim",
              direction=SignalDirection.SHORT),
    ]
    m = compute_metrics(trades)
    assert set(m.by_instrument) == {"ES", "NQ"}
    assert set(m.by_setup) == {"orb", "vwap_reclaim"}
    assert set(m.by_direction) == {"LONG", "SHORT"}
    assert m.by_instrument["ES"].wins == 1
    assert m.by_direction["SHORT"].losses == 1


def test_metrics_empty_trades_no_crash():
    m = compute_metrics([])
    assert m.resolved_trades == 0
    assert m.win_rate == 0.0
    assert m.profit_factor == 0.0
    assert m.max_drawdown_r == 0.0


def test_metrics_no_losses_infinite_profit_factor():
    trades = [_trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:00")]
    m = compute_metrics(trades)
    assert m.profit_factor == float("inf")


def test_metrics_dollar_fields_none_without_dollar_results():
    trades = [_trade(TradeOutcome.WIN, 1.0, "2026-06-15 10:00")]
    m = compute_metrics(trades)
    assert m.net_pnl_dollars is None
    assert m.dollar_metrics_reason is not None
    assert "VERIFIED" in m.dollar_metrics_reason


# --------------------------------------------------------------------------- #
# dollar pipeline: gated by verified registry / APPROVED RiskDecision
# --------------------------------------------------------------------------- #
def _approved_decision(contracts=2) -> RiskDecision:
    return RiskDecision(outcome=RiskOutcome.APPROVED, instrument="NQ",
                        reason_codes=[RiskReasonCode.NONE], reasons=[],
                        suggested_contracts=contracts, risk_per_contract=40.0)


def _rejected_decision() -> RiskDecision:
    return RiskDecision(outcome=RiskOutcome.REJECTED, instrument="NQ",
                        reason_codes=[RiskReasonCode.UNVERIFIED_CONTRACT_SPEC],
                        reasons=["unverified"])


_SPEC = ContractSpec(symbol="NQ", name="NQ", multiplier=20.0, tick_size=0.25,
                     tick_value=5.0, months=["H", "M", "U", "Z"])


def test_dollar_result_computed_when_approved_and_resolved():
    trade = _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00")
    trade = ClosedTrade(**{**trade.__dict__, "entry": 100.0, "stop": 98.0, "take_profit": 104.0})
    result = compute_dollar_result(trade, _approved_decision(2), _SPEC, CostModel(slippage_ticks=0.0))
    assert result is not None
    assert result.suggested_contracts == 2
    # LONG win: (104 - 100) * 20 * 2 contracts = 160
    assert result.gross_pnl == pytest.approx(160.0)


def test_dollar_result_none_when_rejected():
    trade = _trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00")
    result = compute_dollar_result(trade, _rejected_decision(), _SPEC, CostModel())
    assert result is None


def test_dollar_result_none_when_unresolved():
    trade = _trade(TradeOutcome.OPEN, None, "2026-06-15 10:00")
    result = compute_dollar_result(trade, _approved_decision(), _SPEC, CostModel())
    assert result is None


def test_dollar_result_applies_slippage_unfavorably():
    trade = ClosedTrade(**{**_trade(TradeOutcome.LOSS, -1.0, "2026-06-15 10:00").__dict__,
                           "entry": 100.0, "stop": 98.0})
    result = compute_dollar_result(trade, _approved_decision(1), _SPEC,
                                   CostModel(slippage_ticks=2.0))
    # LONG loss: entry filled worse (higher) by slippage, exit filled worse (lower)
    assert result.entry_fill > 100.0
    assert result.exit_fill < 98.0


def test_dollar_result_includes_commission_both_sides():
    trade = ClosedTrade(**{**_trade(TradeOutcome.WIN, 2.0, "2026-06-15 10:00").__dict__,
                           "entry": 100.0, "stop": 98.0, "take_profit": 104.0})
    result = compute_dollar_result(trade, _approved_decision(3), _SPEC,
                                   CostModel(slippage_ticks=0.0, commission_per_contract_per_side=1.5))
    assert result.commission_total == pytest.approx(1.5 * 2 * 3)
    assert result.net_pnl == pytest.approx(result.gross_pnl - result.commission_total)


# --------------------------------------------------------------------------- #
# BacktestEngine end-to-end
# --------------------------------------------------------------------------- #
def _pipeline(engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 3), seed=7)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = engine.load_contract(provider, "ES", "ESM26", "1m")
    engine.build_timeframes(lc)
    return lc


def test_engine_runs_r_only_with_real_unverified_registry(engine):
    """The real, on-disk (unverified) registry must still let R-multiple
    backtesting run fully — the CME gate only blocks the DOLLAR pipeline."""
    real_registry = ContractRegistry.load()
    assert real_registry.verified is False
    se = SetupEngine(engine.cfg, real_registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    lc = _pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])

    bt = BacktestEngine(engine, se, sig_eng, real_registry)   # no RiskEngine
    result = bt.run("ES", lc.timeframes, dts)

    assert result.metrics.total_signals > 0
    assert result.metrics.resolved_trades > 0
    assert result.metrics.net_pnl_dollars is None
    assert result.metrics.dollar_metrics_reason is not None
    for t in result.trades:
        assert t.dollar is None   # no RiskEngine supplied -> never attempted


def test_engine_computes_dollars_with_hypothetical_verified_registry(engine):
    hyp = ContractRegistry(ContractsFile(
        verified=True, verification={"source": "test-only, NOT real CME verification"},
        contracts={"ES": ContractSpec(symbol="ES", name="ES", multiplier=50.0,
                                      tick_size=0.25, tick_value=12.5,
                                      months=["H", "M", "U", "Z"])},
    ))
    se = SetupEngine(engine.cfg, hyp)
    sig_eng = SignalEngine(engine.cfg.signals)
    risk_eng = RiskEngine(RiskConfig(risk_mode="fixed", fixed_risk_dollar=5000.0,
                                     daily_loss_limit_dollar=10_000_000,
                                     max_trades_per_day=1000), hyp)
    account = AccountState(equity=1_000_000.0)
    lc = _pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])

    bt = BacktestEngine(engine, se, sig_eng, hyp, risk_engine=risk_eng, account=account)
    result = bt.run("ES", lc.timeframes, dts)

    assert result.metrics.resolved_trades > 0
    assert result.metrics.net_pnl_dollars is not None
    assert result.metrics.dollar_metrics_reason is None
    resolved = [t for t in result.trades if t.outcome in (TradeOutcome.WIN, TradeOutcome.LOSS)]
    assert all(t.dollar is not None for t in resolved)
    assert all(t.risk_decision is not None for t in result.trades)


def test_engine_deterministic_across_fresh_runs(engine):
    real_registry = ContractRegistry.load()
    lc = _pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])

    def run_once():
        se = SetupEngine(engine.cfg, real_registry)
        sig_eng = SignalEngine(engine.cfg.signals)
        bt = BacktestEngine(engine, se, sig_eng, real_registry)
        return bt.run("ES", lc.timeframes, dts)

    r1, r2 = run_once(), run_once()
    key = lambda r: [(t.signal_id, t.outcome, t.r_multiple) for t in r.trades]
    assert key(r1) == key(r2)
    assert r1.metrics == r2.metrics


# --------------------------------------------------------------------------- #
# no-lookahead: the backtest's signal-generation loop must not leak future
# data into decisions within a truncated window — mirrors the pattern already
# established for Setup/Signal Engine no-lookahead tests.
# --------------------------------------------------------------------------- #
def test_no_lookahead_signal_generation_matches_truncated_replay(engine):
    real_registry = ContractRegistry.load()
    se_full = SetupEngine(engine.cfg, real_registry)
    lc = _pipeline(engine)
    dts = list(lc.timeframes["5m"].index[8:])

    bt_full = BacktestEngine(engine, se_full, SignalEngine(engine.cfg.signals), real_registry)
    full_result = bt_full.run("ES", lc.timeframes, dts)

    # pick a cutoff partway through and replay ONLY truncated data up to it
    cutoff = dts[len(dts) // 2]
    tf_trunc = {tf: df[df.index <= cutoff] for tf, df in lc.timeframes.items()}
    dts_trunc = [d for d in dts if d <= cutoff]

    se_trunc = SetupEngine(engine.cfg, real_registry)
    bt_trunc = BacktestEngine(engine, se_trunc, SignalEngine(engine.cfg.signals), real_registry)
    trunc_result = bt_trunc.run("ES", lc.timeframes, dts_trunc)   # note: full tf_bars dict passed,
    # but decision_times only up to cutoff — exercises analysis_context's own
    # as-of slicing exactly like live use would at each of those bars.

    full_up_to_cutoff = [
        (t.signal_id, t.entry, t.stop) for t in full_result.trades
        if t.entry_timestamp <= cutoff
    ]
    trunc_signals = [(t.signal_id, t.entry, t.stop) for t in trunc_result.trades]
    assert full_up_to_cutoff == trunc_signals
    assert len(trunc_signals) > 0


def test_trade_resolution_uses_future_bars_legitimately_not_a_lookahead_bug(engine):
    """Sanity check on the DESIGN distinction: signal generation (tested above)
    never sees the future, but resolving what happened to an already-fired
    signal correctly and necessarily does look at subsequent bars."""
    sig = _signal()
    bars = _bars(["2026-06-15 10:00", "2026-06-15 10:05", "2026-06-15 10:10"],
                [(100, 101, 99, 100), (100, 103, 99, 101), (101, 105, 100, 104)])
    trade = resolve_trade_outcome(sig, bars)
    assert trade.exit_timestamp > sig.timestamp
    assert trade.outcome is TradeOutcome.WIN
