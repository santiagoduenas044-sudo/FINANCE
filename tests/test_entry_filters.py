"""19-oct filters: no entrar tarde, stop cap, serrucho; and the 1-hour set ups."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd

from signal_engine.config.schema import EngineConfig, GhostConfig
from signal_engine.ghost.brain import GhostBrain
from signal_engine.ghost.levels import efficiency
from signal_engine.ghost.models import NoTradeReason
from signal_engine.runtime.engine import _idea_reject_reason
from signal_engine.runtime.shadows import load_specs
from tests.test_phase15_authority import _exec_ta_with_atr, _fake_signal, _mk_state

NY = "America/New_York"
NOW = pd.Timestamp("2026-10-16 11:00", tz=NY)


def _bars(trend: bool):
    """Two hours of 1m closes: a steady climb, or a saw going back and forth."""
    idx = pd.date_range(NOW - pd.Timedelta(minutes=179), NOW, freq="1min", tz=NY)
    k = np.arange(len(idx))
    c = 5000 + (0.5 * k if trend else 15 * np.sin(k / 4.0))
    return pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c}, index=idx)


def _decide(cfg: dict, *, mid=None, entry=5000, stop=4980, bars=None, instrument="NQ"):
    brain = GhostBrain(GhostConfig(min_confluence_score=0.0, block_htf_conflict=False,
                                   block_against_structure=False, min_stop_atr_mult=0.0,
                                   max_stop_atr_mult=0.0, **cfg))
    sig = _fake_signal("LONG", instrument=instrument, entry=entry, stop=stop,
                       tps=(entry + 3 * (entry - stop),))
    if mid is not None:
        sig = replace(sig, market_context={"zone_mid": mid})
    ctx = SimpleNamespace(execution=SimpleNamespace(bars=_bars(True) if bars is None else bars),
                          decision_time=NOW)
    return brain._decide_signal(sig, _mk_state(), _exec_ta_with_atr(3.0), ctx,
                                False, False, has_active=None).no_trade_reasons


def test_late_entry_is_skipped():
    # stop 20 pts: 0.75x = 15 pts past the zone's middle is the limit
    assert NoTradeReason.LATE_ENTRY in _decide({"max_chase_r": 0.75}, mid=4980)    # 20 pts late
    assert NoTradeReason.LATE_ENTRY not in _decide({"max_chase_r": 0.75}, mid=4990)  # 10 pts
    assert NoTradeReason.LATE_ENTRY not in _decide({}, mid=4980)                    # off


def test_stop_over_the_cap_is_skipped():
    big = _decide({"max_stop_points": {"NQ": 50.0}}, entry=5000, stop=4940)          # 60 pts
    assert NoTradeReason.STOP_TOO_WIDE in big
    assert NoTradeReason.STOP_TOO_WIDE not in _decide({"max_stop_points": {"NQ": 50.0}},
                                                       entry=5000, stop=4960)


def test_choppy_market_is_skipped():
    assert efficiency(_bars(True), NOW) > 0.9
    assert efficiency(_bars(False), NOW) < 0.1
    assert efficiency(_bars(True).iloc[:10], NOW) is None                 # not enough bars
    assert NoTradeReason.CHOPPY in _decide({"chop_min_er": 0.1}, bars=_bars(False))
    assert NoTradeReason.CHOPPY not in _decide({"chop_min_er": 0.1}, bars=_bars(True))


def test_the_messages_say_why():
    assert "tarde" in _idea_reject_reason(["late_entry"])
    assert "serrucho" in _idea_reject_reason(["choppy_market"])
    assert "stop" in _idea_reject_reason(["stop_too_wide"])


def test_filters_are_on_where_they_belong():
    g = EngineConfig.load(strategy="zones").ghost
    assert (g.max_chase_r, g.chop_min_er, g.max_stop_points["NQ"]) == (0.75, 0.1, 50.0)
    old = EngineConfig.load(strategy="zones_fijo").ghost                    # v4 as it was
    assert (old.max_chase_r, old.chop_min_er, old.clear_path, old.max_stop_points["NQ"]) \
        == (None, None, None, 0.0)
    specs = {s.setup: s for s in load_specs("ghost")}
    for n in (4, 13):
        assert specs[n].overrides["ghost"]["max_chase_r"] == 0.75
        assert specs[n].overrides["ghost"]["chop_min_er"] == 0.1


def test_one_hour_set_ups():
    specs = {s.setup: s for s in load_specs("ghost")}
    assert specs[14].label == "Set up 14 · FVG 1H" and specs[15].label == "Set up 15 · FVG 1H DÍA"
    cfg = EngineConfig.load(strategy="zones_1h")
    z = cfg.setups.zone_retrace
    assert (z.timeframe, z.zone_timeframe, z.atr_timeframe) == ("1H", "1H", "1H")
    assert cfg.timeframe_roles.structure == "1H" and cfg.timeframe_roles.execution == "1m"
    assert cfg.ghost.max_stop_points["NQ"] == 0.0                  # no 50-pt cap on 1-hour stops
    assert cfg.ghost.partial_at_progress == 0.5 and cfg.ghost.chop_min_er == 0.1
    assert specs[14].overrides["ghost"]["valid_sessions"] == ["RTH", "ETH"]
    assert cfg.ghost.valid_sessions == ["RTH"]                      # Set up 15: day only
