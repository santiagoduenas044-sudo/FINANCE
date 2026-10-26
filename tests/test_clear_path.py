"""Espacio libre: no trade with a support / resistance in the first half of the
way to the target."""
from types import SimpleNamespace

import pandas as pd

from signal_engine.config.schema import EngineConfig, GhostConfig
from signal_engine.ghost.brain import GhostBrain
from signal_engine.ghost.levels import Level, in_the_way, levels_at
from signal_engine.ghost.models import NoTradeReason
from signal_engine.runtime.engine import _idea_reject_reason
from signal_engine.runtime.shadows import load_specs
from tests.test_phase15_authority import _exec_ta_with_atr, _fake_signal, _mk_state

NY = "America/New_York"
NOW = pd.Timestamp("2026-10-09 10:00", tz=NY)


def test_a_level_in_the_first_half_blocks():
    res = [Level(5012.0, ["máximo de ayer"])]
    assert in_the_way("LONG", 5000, 5030, res, 0.5).price == 5012.0   # 12 of 30 pts
    assert in_the_way("LONG", 5000, 5030, res, 0.3) is None           # 9 pts: clear
    assert in_the_way("LONG", 5000, 5060, res, 0.5).price == 5012.0
    assert in_the_way("LONG", 5000, 5020, res, 0.5) is None           # 12 of 20: past half
    # a short right on top of a support
    sup = [Level(4993.0, ["mínimo de la noche"])]
    assert in_the_way("SHORT", 5000, 4970, sup, 0.5).price == 4993.0
    assert in_the_way("LONG", 5000, 5030, sup, 0.5) is None           # behind a long


def test_round_numbers_alone_and_the_entry_itself_do_not_count():
    assert in_the_way("LONG", 4990, 5030, [Level(5000.0, ["número redondo"])], 0.5) is None
    both = [Level(5000.0, ["número redondo", "máximo de ayer"])]
    assert in_the_way("LONG", 4990, 5030, both, 0.5) is not None
    assert in_the_way("LONG", 5000, 5030, [Level(5001.5, ["máximo de ayer"])], 0.5) is None


def _bars():
    """Yesterday 4950–5012 closing 4990; tonight 4993–5001; now 5000."""
    def seg(a, b, price, hi=None, lo=None):
        idx = pd.date_range(a, b, freq="1min", tz=NY)
        df = pd.DataFrame({"open": price, "high": price + 0.5, "low": price - 0.5,
                           "close": price}, index=idx)
        if hi is not None:
            df.iloc[len(df) // 2, df.columns.get_loc("high")] = hi
        if lo is not None:
            df.iloc[len(df) // 3, df.columns.get_loc("low")] = lo
        return df
    return pd.concat([seg("2026-10-08 09:31", "2026-10-08 16:00", 4990.0, hi=5012.0, lo=4950.0),
                      seg("2026-10-08 18:01", "2026-10-09 09:59", 4997.0, hi=5001.0, lo=4993.0),
                      seg("2026-10-09 10:00", "2026-10-09 10:00", 5000.0)])


def test_levels_come_from_yesterday_and_the_night():
    names = {n for x in levels_at(_bars(), NOW) for n in x.names}
    assert {"máximo de ayer", "mínimo de ayer", "mínimo de la noche"} <= names
    assert levels_at(pd.DataFrame(), NOW) == []


def _decide(direction, room):
    brain = GhostBrain(GhostConfig(min_confluence_score=0.0, block_htf_conflict=False,
                                   block_against_structure=False, clear_path=room))
    sig = _fake_signal(direction, entry=5000, stop=4990 if direction == "LONG" else 5010,
                       tps=(5030,) if direction == "LONG" else (4970,))
    ctx = SimpleNamespace(execution=SimpleNamespace(bars=_bars()), decision_time=NOW)
    return brain._decide_signal(sig, _mk_state(), _exec_ta_with_atr(3.0), ctx,
                                False, False, has_active=None)


def test_ghost_skips_a_long_under_yesterdays_high_and_a_short_on_the_night_low():
    d = _decide("LONG", 0.5)
    assert NoTradeReason.LEVEL_IN_THE_WAY in d.no_trade_reasons
    assert "level=5012.00 máximo de ayer" in d.detail
    assert NoTradeReason.LEVEL_IN_THE_WAY in _decide("SHORT", 0.5).no_trade_reasons
    assert NoTradeReason.LEVEL_IN_THE_WAY not in _decide("LONG", None).no_trade_reasons


def test_the_idea_message_names_the_level():
    text = _idea_reject_reason(["level_in_the_way"], "hard guard(s) triggered level=5012.00 máximo de ayer")
    assert text == ("reaccionó, pero tenía un soporte o resistencia en el camino al objetivo "
                    "(5,012 · máximo de ayer)")


def test_no_running_set_up_uses_it_since_26_oct():
    """The filter still works when a strategy asks for it (tests above), but
    no running set up does: it let through 1 trade in two weeks."""
    assert EngineConfig.load(strategy="zones").ghost.clear_path is None
    for sp in load_specs("ghost"):
        if not sp.retired:
            g = EngineConfig.load(strategy=sp.strategy, overrides=sp.overrides or None).ghost
            assert g.clear_path is None, sp.label
