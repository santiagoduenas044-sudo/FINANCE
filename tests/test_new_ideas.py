"""21-oct set ups: target at the next level, exit by time, trade with the plan."""
from types import SimpleNamespace

import numpy as np
import pandas as pd

from signal_engine.config.schema import GhostConfig
from signal_engine.ghost.brain import GhostBrain
from signal_engine.ghost.levels import Level, next_level, plan_side
from signal_engine.ghost.models import NoTradeReason
from signal_engine.paper.tracker import PaperTradeTracker
from signal_engine.runtime.shadows import load_specs
from tests.test_phase15_authority import _exec_ta_with_atr, _fake_signal, _mk_state

NY = "America/New_York"


def _t(s):
    return pd.Timestamp(s, tz=NY)


def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def test_next_level_skips_round_numbers_and_the_near_ones():
    lv = [Level(5010, ["máximo de la noche"]), Level(5050, ["número redondo"]),
          Level(5060, ["máximo de ayer"]), Level(5200, ["cierre de ayer"])]
    assert next_level("LONG", 5000, lv, 30, 80).price == 5060      # 5010 too close, 5050 round
    assert next_level("LONG", 5000, lv, 70, 80) is None
    assert next_level("SHORT", 5000, [Level(4950, ["mínimo de ayer"])], 30, 80).price == 4950


def _decide(cfg, bars, direction="LONG", when="2026-10-16 11:00"):
    brain = GhostBrain(GhostConfig(min_confluence_score=0.0, block_htf_conflict=False,
                                   block_against_structure=False, min_stop_atr_mult=0.0,
                                   max_stop_atr_mult=0.0, **cfg))
    e, r = 5000, 20
    sig = _fake_signal(direction, instrument="NQ", entry=e, stop=e - r if direction == "LONG" else e + r,
                       tps=(e + 3 * r,) if direction == "LONG" else (e - 3 * r,))
    ctx = SimpleNamespace(execution=SimpleNamespace(bars=bars), decision_time=_t(when))
    return brain._decide_signal(sig, _mk_state(), _exec_ta_with_atr(3.0), ctx, False, False,
                                has_active=None)


def _days(trend_up: bool):
    """Yesterday's session and this morning, trending one way."""
    idx = pd.date_range("2026-10-15 09:31", "2026-10-16 11:00", freq="1min", tz=NY)
    k = np.arange(len(idx))
    c = 5000 + (0.05 if trend_up else -0.05) * k
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c}, index=idx)


def test_level_target_moves_the_target_before_the_level():
    idx = pd.date_range("2026-10-15 09:31", "2026-10-16 11:00", freq="1min", tz=NY)
    c = np.full(len(idx), 4995.0)
    c[100] = 5052.0                                   # yesterday's high: 5053, 2.65 stops away
    bars = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c}, index=idx)
    d = _decide({"level_target": True}, bars)
    assert d.signal.take_profits == [5051.0] and "target_level=5053.00" in d.detail
    assert _decide({}, bars).signal.take_profits == [5060]                    # off: 3x


def test_follow_plan_blocks_the_other_side():
    up = _days(True)
    assert plan_side(up, _t("2026-10-16 11:00")) == "LONG"
    assert NoTradeReason.AGAINST_PLAN in _decide({"follow_plan": True}, up, "SHORT").no_trade_reasons
    assert NoTradeReason.AGAINST_PLAN not in _decide({"follow_plan": True}, up, "LONG").no_trade_reasons
    assert NoTradeReason.AGAINST_PLAN not in _decide({}, up, "SHORT").no_trade_reasons


def test_time_exit_closes_a_trade_that_did_not_move(tmp_path):
    tr = PaperTradeTracker(store_dir=tmp_path, time_exit_minutes=90, time_exit_progress=0.33)
    t = tr.open_trade(signal_id="x", instrument="NQ", direction="LONG", setup="zone_retrace",
                      entry=5000.0, stop=4980.0, take_profits=[5060.0], opened_at=_t("2026-10-16 10:00"))
    for m in range(1, 90):                             # wanders, never +20 (a third of 60)
        tr.update_symbol("NQ", _bar(5002, 5010, 4995, 5004), _t("2026-10-16 10:00") + pd.Timedelta(minutes=m))
    assert t.is_open
    tr.update_symbol("NQ", _bar(5004, 5008, 5000, 5006), _t("2026-10-16 11:30"))
    assert not t.is_open and t.exit_reason.value == "EXPIRED" and t.exit_price == 5006.0
    # one that moved a third of the way stays
    tr2 = PaperTradeTracker(store_dir=tmp_path / "b", time_exit_minutes=90)
    t2 = tr2.open_trade(signal_id="y", instrument="NQ", direction="LONG", setup="zone_retrace",
                        entry=5000.0, stop=4980.0, take_profits=[5060.0], opened_at=_t("2026-10-16 10:00"))
    tr2.update_symbol("NQ", _bar(5000, 5025, 4999, 5010), _t("2026-10-16 10:30"))
    tr2.update_symbol("NQ", _bar(5010, 5012, 5005, 5008), _t("2026-10-16 11:31"))
    assert t2.is_open


def test_the_three_set_ups_are_set_up_1_plus_one_change():
    specs = {s.setup: s for s in load_specs("ghost")}
    assert specs[19].overrides == {"ghost": {"level_target": True}} and specs[19].strategy == "zones"
    assert specs[20].overrides["ghost"]["time_exit_minutes"] == 90
    assert specs[21].overrides == {"ghost": {"follow_plan": True}}
    assert [specs[n].label for n in (19, 20, 21)] == \
        ["Set up 19 · FVG 15 NIVEL", "Set up 20 · FVG 15 TIEMPO", "Set up 21 · FVG 15 PLAN"]
