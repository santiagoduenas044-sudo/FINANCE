"""Phase 14 — PaperTrade memory tests (simulated; no broker, no orders).

Covers the whole lifecycle and bookkeeping: creation, active state, P&L in
points, R multiple, TP1, TP2, SL, invalidation, MFE/MAE, duration, duplicate
protection, persistence + restart recovery, and the conservative OHLC-ambiguity
rule.
"""
import pandas as pd
import pytest

from signal_engine.paper.models import ExitReason, TradeStatus
from signal_engine.paper.tracker import PaperTradeTracker

TS = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def _long(tr, entry=5000, stop=4995, tps=(5010, 5020), inval=4995, setup="orb"):
    return tr.open_trade(signal_id="x", instrument="ES", direction="LONG", setup=setup,
                         entry=entry, stop=stop, take_profits=list(tps), opened_at=TS,
                         invalidation_price=inval, context={"regime": "TREND_UP", "session": "RTH"})


def test_creation_active_and_initial_risk():
    tr = PaperTradeTracker()
    t = _long(tr)
    assert t.status is TradeStatus.ACTIVE and t.is_open
    assert t.initial_risk == 5 and t.current_price == 5000


def test_duplicate_protection():
    tr = PaperTradeTracker()
    _long(tr)
    dup = _long(tr)              # same instrument/direction/setup while open
    assert dup is None
    assert len(tr.active()) == 1


def test_pnl_points_and_r_multiple():
    tr = PaperTradeTracker()
    t = _long(tr)
    tr.update_on_bar(t, _bar(5000, 5008, 4999, 5007), TS + pd.Timedelta(minutes=5))
    assert t.pnl_points == pytest.approx(7.0)
    assert t.r_multiple == pytest.approx(7.0 / 5)


def test_tp1_partial_then_tp2_terminal():
    tr = PaperTradeTracker()
    t = _long(tr, tps=(5010, 5020))
    tr.update_on_bar(t, _bar(5000, 5012, 5006, 5011), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.TP1_HIT and t.tp1_hit and t.is_open  # still holding TP2
    tr.update_on_bar(t, _bar(5011, 5021, 5010, 5020), TS + pd.Timedelta(minutes=10))
    assert t.status is TradeStatus.TP2_HIT and not t.is_open
    assert t.exit_reason is ExitReason.TP2 and t.r_multiple == pytest.approx(4.0)


def test_single_target_tp1_is_terminal():
    tr = PaperTradeTracker()
    t = _long(tr, tps=(5010,))
    tr.update_on_bar(t, _bar(5000, 5011, 4999, 5010), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.TP1_HIT and not t.is_open
    assert t.exit_reason is ExitReason.TP1


def test_stop_loss():
    tr = PaperTradeTracker()
    t = _long(tr, inval=0.0)
    tr.update_on_bar(t, _bar(5000, 5001, 4994, 4996), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.STOPPED and t.exit_reason is ExitReason.STOP
    assert t.r_multiple == pytest.approx(-1.0)


def test_invalidation_distinct_and_tighter_than_stop():
    tr = PaperTradeTracker()
    t = _long(tr, stop=4990, inval=4997)   # inval between entry and stop -> hit first
    tr.update_on_bar(t, _bar(5000, 5002, 4996, 4998), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.INVALIDATED and t.exit_reason is ExitReason.INVALIDATION


def test_ohlc_ambiguity_is_conservative_stop_wins():
    """A bar spanning both stop and TP: order unknown intrabar -> protective
    level wins (never over-report a win)."""
    tr = PaperTradeTracker()
    t = _long(tr, inval=0.0)
    tr.update_on_bar(t, _bar(5000, 5011, 4994, 5000), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.STOPPED


def test_mfe_mae_tracking():
    tr = PaperTradeTracker()
    t = _long(tr, tps=(5030,))
    tr.update_on_bar(t, _bar(5000, 5009, 4998, 5003), TS + pd.Timedelta(minutes=5))  # +9 / -2
    tr.update_on_bar(t, _bar(5003, 5006, 4996, 5001), TS + pd.Timedelta(minutes=10)) # -4 low
    assert t.mfe_points == pytest.approx(9.0)
    assert t.mae_points == pytest.approx(-4.0)
    assert t.mfe_r == pytest.approx(9.0 / 5) and t.mae_r == pytest.approx(-4.0 / 5)


def test_short_direction_full_cycle():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="s", instrument="NQ", direction="SHORT", setup="orb",
                      entry=20000, stop=20010, take_profits=[19980], opened_at=TS)
    tr.update_on_bar(t, _bar(20000, 20005, 19979, 19981), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.TP1_HIT and t.exit_reason is ExitReason.TP1
    assert t.r_multiple == pytest.approx(2.0)


def test_pending_fill_then_active():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="p", instrument="ES", direction="LONG", setup="orb",
                      entry=5000, stop=4995, take_profits=[5010], opened_at=TS, fill="limit")
    assert t.status is TradeStatus.PENDING
    tr.update_on_bar(t, _bar(4999, 5002, 4998, 5001), TS + pd.Timedelta(minutes=5))  # touches entry
    assert t.status is TradeStatus.ACTIVE


def test_pending_cancelled_if_invalidated_before_fill():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="p", instrument="ES", direction="LONG", setup="orb",
                      entry=5005, stop=4995, take_profits=[5015], opened_at=TS,
                      invalidation_price=4990, fill="limit")
    tr.update_on_bar(t, _bar(4994, 4996, 4989, 4991), TS + pd.Timedelta(minutes=5))
    assert t.status is TradeStatus.CANCELLED and t.exit_reason is ExitReason.CANCELLED


def test_duration_recorded():
    tr = PaperTradeTracker()
    t = _long(tr, inval=0.0)
    tr.update_on_bar(t, _bar(5000, 5001, 4994, 4996), TS + pd.Timedelta(minutes=30))
    assert t.duration_seconds == pytest.approx(1800.0)


def test_update_symbol_ignores_old_bars():
    tr = PaperTradeTracker()
    t = _long(tr)
    tr.update_symbol("ES", _bar(5000, 5006, 4999, 5005), TS + pd.Timedelta(minutes=5))
    held = t.bars_held
    tr.update_symbol("ES", _bar(5000, 5006, 4999, 5005), TS + pd.Timedelta(minutes=5))  # same time
    assert t.bars_held == held   # not double-counted


def test_persistence_and_restart_recovery(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path)
    a = _long(tr)                                   # stays open
    tr.update_on_bar(a, _bar(5000, 5008, 4999, 5006), TS + pd.Timedelta(minutes=5))
    b = tr.open_trade(signal_id="b", instrument="NQ", direction="SHORT", setup="orb",
                      entry=20000, stop=20010, take_profits=[19990], opened_at=TS)
    tr.update_on_bar(b, _bar(20000, 20005, 19989, 19991), TS + pd.Timedelta(minutes=5))  # TP -> closed

    fresh = PaperTradeTracker(store_dir=tmp_path)
    assert len(fresh.active()) == 1 and len(fresh.closed_trades) == 1
    assert fresh.active()[0].pnl_points == pytest.approx(6.0)
    assert fresh.closed_trades[0].exit_reason is ExitReason.TP1


def test_stats_aggregation(tmp_path):
    tr = PaperTradeTracker()
    w = _long(tr, tps=(5010,))
    tr.update_on_bar(w, _bar(5000, 5011, 4999, 5010), TS + pd.Timedelta(minutes=5))   # +2R win
    l = tr.open_trade(signal_id="l", instrument="NQ", direction="LONG", setup="vwap_reclaim",
                      entry=20000, stop=19990, take_profits=[20030], opened_at=TS)
    tr.update_on_bar(l, _bar(20000, 20001, 19989, 19991), TS + pd.Timedelta(minutes=5))  # -1R loss
    s = tr.stats()
    assert s["total_trades"] == 2 and s["wins"] == 1 and s["losses"] == 1
    assert s["win_rate"] == 0.5
    assert set(s["by_direction"].keys()) == {"LONG"}
    assert "orb" in s["by_setup"] and "vwap_reclaim" in s["by_setup"]
