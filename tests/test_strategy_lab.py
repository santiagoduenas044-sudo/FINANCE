"""Strategy lab: exits on 1-minute bars (stop, target, gap, BE+, 16:55 flat)
and signals that never use a bar before it has closed."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("strategy_lab", ROOT / "scripts" / "strategy_lab.py")
lab = importlib.util.module_from_spec(_spec)
sys.modules["strategy_lab"] = lab          # dataclasses look the module up by name
_spec.loader.exec_module(lab)


def _market(rows, start="2025-03-03 10:00"):
    idx = pd.date_range(start, periods=len(rows), freq="1min")
    df = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])
    df["volume"] = 100.0
    return lab.Market(df)


def _one(mk, sig, **kw):
    trades = lab.run(mk, [sig], 0.0, "rth", **kw)
    assert len(trades) == 1
    return trades[0][1]


def test_target_stop_and_same_minute_rule():
    rows = [(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110)]
    mk = _market(rows)
    ready = mk.t[0]                                   # enters at the open of minute 1
    assert _one(mk, lab.Signal(ready, True, 95.0, k=2.0)) == 10.0        # target 110
    rows = [(100, 100, 100, 100), (100, 111, 94, 100)]                   # both in one minute
    assert _one(_market(rows), lab.Signal(_market(rows).t[0], True, 95.0, k=2.0)) == -5.0


def test_gap_through_the_stop_fills_at_the_open():
    rows = [(100, 100, 100, 100), (100, 101, 99, 100), (90, 91, 88, 89)]
    mk = _market(rows)
    assert _one(mk, lab.Signal(mk.t[0], True, 95.0, k=2.0)) == -10.0


def test_breakeven_plus_after_70_percent():
    # long 100, stop 95, target 110: 70% = 107. Reaches 108, then falls back.
    rows = [(100, 100, 100, 100), (100, 108, 99, 107), (107, 107, 90, 91)]
    mk = _market(rows)
    assert _one(mk, lab.Signal(mk.t[0], True, 95.0, k=2.0), be_at=0.7, be_plus=2.0) == 2.0
    assert _one(mk, lab.Signal(mk.t[0], True, 95.0, k=2.0)) == -5.0     # without the rule


def test_flat_at_1655():
    rows = [(100, 100, 100, 100)] + [(100, 101, 99, 100.5)] * 100      # 15:20 → 17:00
    mk = _market(rows, start="2025-03-03 15:20")
    trades = lab.run(mk, [lab.Signal(mk.t[0], True, 90.0, k=3.0)], 0.0, "rth")
    # entry 15:21, nothing hit: closed on the 16:54 minute, the last before 16:55
    assert trades == [("2025-03-03", 0.5)]


def test_zone_signals_wait_for_the_bar_to_close():
    rng = np.random.default_rng(3)
    c = 20000 + np.cumsum(rng.normal(0, 4, 3000))
    o = np.r_[c[0], c[:-1]]
    rows = list(zip(o, np.maximum(o, c) + 2, np.minimum(o, c) - 2, c))
    mk = _market(rows, start="2025-03-03 09:30")
    bars = mk.bars("5min")
    for s in lab.zones(mk, "5min", 2.0):
        opened = bars.index[bars["end"].to_numpy() == s.ready]
        assert len(opened) == 1                        # ready = the close of a real bar
