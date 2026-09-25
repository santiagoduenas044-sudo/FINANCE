"""End-of-day close at a configured time, and re-running a closed trade."""
import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig
from signal_engine.runtime.engine import eod_due

ET = "America/New_York"
_spec = importlib.util.spec_from_file_location(
    "correct_trade", Path(__file__).resolve().parents[1] / "scripts" / "correct_trade.py")
correct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(correct)


def _t(hhmm):
    return pd.Timestamp(f"2026-09-24 {hhmm}", tz=ET)


def test_eod_waits_for_the_close_time_but_never_leaves_a_trade_overnight():
    assert eod_due("ETH", _t("16:01"), None)                    # legacy: at RTH end
    assert not eod_due("ETH", _t("16:01"), "16:55")
    assert eod_due("ETH", _t("16:55"), "16:55")
    assert eod_due("ETH", _t("18:05"), "16:55")                 # missed bar: still closes
    assert eod_due("ETH", _t("08:00"), "16:55")
    assert not eod_due("RTH", _t("15:59"), "16:55")


def test_live_strategy_closes_at_1655():
    assert EngineConfig.load(strategy="zones").ghost.eod_close_time == "16:55"


def _bars(rows):
    idx = pd.DatetimeIndex([_t(h) for h, *_ in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx,
                        columns=["open", "high", "low", "close"])


def test_replay_turns_the_4pm_flat_into_the_real_result():
    # Idea #4: short 30,755.50, stop 30,792.12, TP1 30,722.12, TP 30,622.
    old = {"signal_id": "s4", "instrument": "NQ", "direction": "SHORT",
           "setup": "zone_retrace", "entry": 30755.5, "initial_risk": 36.62,
           "take_profits": [30722.12, 30622.0], "opened_at": _t("15:20").isoformat(),
           "invalidation_price": 30792.12, "context": {"idea_number": 4}}
    bars = _bars([("15:40", 30755, 30760, 30750, 30757),
                  ("16:00", 30757, 30758, 30756, 30757),   # old flat exit bar
                  ("16:10", 30757, 30757, 30700, 30705),   # TP1: half banked
                  ("16:40", 30705, 30740, 30681, 30735),   # rally, BE not touched
                  ("16:55", 30735, 30740, 30730, 30736)])  # new flat
    cfg = EngineConfig.load(strategy="zones")
    new = correct.replay(old, bars, cfg)
    assert new.exit_reason.value == "EOD" and new.closed_at == _t("16:55")
    assert new.pnl_points == pytest.approx(0.5 * 33.38 + 0.5 * (30755.5 - 30736))

    legacy = cfg.model_copy(update={"ghost": cfg.ghost.model_copy(
        update={"eod_close_time": None})})
    assert correct.replay(old, bars, legacy).closed_at == _t("16:00")


def test_trade_chart_is_a_png():
    pytest.importorskip("matplotlib")
    from signal_engine.output.trade_chart import render_trade_png
    bars = _bars([("15:40", 1, 2, 0.5, 1.5), ("15:41", 1.5, 2.5, 1, 2)])
    png = render_trade_png(bars, {"entry": 1.5, "stop": 3.0, "tp1": 1.0, "tp2": 0.5,
                                  "opened_at": _t("15:40"), "closed_at": _t("15:41"),
                                  "exit_price": 1.2, "pnl_points": 0.3, "r_multiple": 0.2},
                           marks=[(_t("15:41"), "cierre anterior", "#9e9e9e")])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
