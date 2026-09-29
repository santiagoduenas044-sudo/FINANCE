"""Strategy lab: exits on 1-minute bars (stop, target, gap, BE+, 16:55 flat)
and signals that never use a bar before it has closed."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

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
    assert trades == [("2025-03-03", 0.5, 10.0, 15)]       # (date, points, stop pts, hour)


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


def test_fixed_dollar_risk_sizes_in_micros():
    trades = [("d", 50.0, 25.0), ("d", -25.0, 25.0), ("d", 10.0, 3.0), ("d", -1.0, 200.0)]
    # $300 / (25 pts x $2) = 6 MNQ; a 3-pt stop is capped at 15 MNQ; a 200-pt stop does not fit
    assert [x[:2] for x in lab.sized_usd(trades, "riesgo", 2.0, 300.0)] == [
        ("d", 600.0), ("d", -300.0), ("d", 300.0)]
    assert lab.sized_usd(trades[:1], "5mnq", 10.0, 300.0) == [("d", 500.0, 25.0)]


def test_results_by_entry_hour():
    trades = [("d", 100.0, 20.0, 9), ("d", -50.0, 30.0, 9), ("d", 30.0, 10.0, 13)]
    h = lab.by_hour(trades)
    assert list(h) == [9, 13]
    assert h[9]["n"] == 2 and h[9]["net"] == 50.0 and h[9]["stop"] == 25.0


def test_run_skips_stops_wider_than_the_sizing_allows():
    rows = [(100, 100, 100, 100), (100, 106, 99, 105), (105, 111, 104, 110)]
    mk = _market(rows)
    assert lab.run(mk, [lab.Signal(mk.t[0], True, 95.0, k=2.0)], 0.0, "rth", max_risk=4.0) == []


def _random_market(n=3000, seed=3, start="2025-03-03 09:30"):
    rng = np.random.default_rng(seed)
    c = 20000 + np.cumsum(rng.normal(0, 4, n))
    o = np.r_[c[0], c[:-1]]
    return _market(list(zip(o, np.maximum(o, c) + 2, np.minimum(o, c) - 2, c)), start=start)


def test_dynamic_stop_sits_behind_the_last_hour_and_the_origin():
    mk = _random_market()
    b = mk.bars("1min")
    H, L, end = b["high"].to_numpy(), b["low"].to_numpy(), b["end"].to_numpy()
    sigs = lab.zones(mk, "1min", 3.0, stop="dinamico", buffer=3.0)
    assert sigs
    for s in sigs:
        i = int(np.flatnonzero(end == s.ready)[0])
        if s.long:
            assert s.stop <= L[max(0, i - 60):i + 1].min() - 3.0 + 1e-9
        else:
            assert s.stop >= H[max(0, i - 60):i + 1].max() + 3.0 - 1e-9


def test_box_breakout_enters_on_the_close_outside_with_stop_across():
    # 25 quiet 5-minute candles (warm-up + a 60-min box), then one closes above it
    rows = []
    for _ in range(25 * 5):
        rows.append((100, 101, 99, 100))
    rows += [(100, 104, 100, 103)] * 4 + [(103, 106, 102, 105)]
    mk = _market(rows, start="2025-03-03 09:30")
    sigs = lab.boxes(mk, 12, 2.0, False, 3.5)
    assert len(sigs) == 1 and sigs[0].long and sigs[0].stop == 98.0
    assert pd.Timestamp(sigs[0].ready) == pd.Timestamp("2025-03-03 09:30") + pd.Timedelta(minutes=130)


def test_break_of_yesterdays_high():
    day1 = pd.date_range("2025-03-03 09:30", "2025-03-03 15:59", freq="1min")
    day2 = pd.date_range("2025-03-04 09:30", "2025-03-04 10:29", freq="1min")
    idx = day1.append(day2)
    df = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 100.0},
                      index=idx)
    df.loc["2025-03-04 10:00":, ["open", "high", "low", "close"]] = [102.0, 104.0, 101.5, 103.0]
    mk = lab.Market(df)
    sigs = lab.levels(mk, "ayer", 2.0, False)
    assert len(sigs) == 1 and sigs[0].long
    assert pd.Timestamp(sigs[0].ready) == pd.Timestamp("2025-03-04 10:05")
    assert sigs[0].stop == 100.5                        # behind the breakout candle


def test_big_companies_filter_follows_their_emas():
    mk = _random_market(n=600)
    idx = pd.date_range("2025-03-03 09:30", periods=100, freq="5min")
    up = pd.Series(np.linspace(100, 150, 100), index=idx)
    closes = pd.DataFrame({s: up for s in ("A", "B", "C", "D")})
    side = lab.breadth(mk, closes)
    t = np.datetime64("2025-03-03 12:00")
    assert side(t) == 1.0
    assert side(np.datetime64("2025-03-04 03:00")) == 0.0          # no fresh stock data
    sigs = [lab.Signal(t, True, 90.0, k=2.0), lab.Signal(t, False, 200.0, k=2.0)]
    assert [s.long for s in lab.only_with(sigs, side)] == [True]


def test_stock_splits_are_undone():
    idx = pd.date_range("2024-06-07 15:58", periods=2, freq="1min").append(
        pd.date_range("2024-06-10 09:30", periods=2, freq="1min"))
    df = pd.DataFrame({"open": [1200.0, 1200, 121, 121], "high": [1201.0, 1201, 122, 122],
                       "low": [1199.0, 1199, 120, 120], "close": [1200.0, 1200, 121, 121]},
                      index=idx)
    out = lab._unsplit(df)
    assert out["close"].tolist() == [120.0, 120.0, 121.0, 121.0]


def test_megacap_download_splits_by_ticker():
    spec = importlib.util.spec_from_file_location("fetch_databento", ROOT / "scripts" / "fetch_databento.py")
    fd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fd)
    assert fd.stock_list("grandes")[:3] == ("NVDA", "MSFT", "AAPL")
    assert fd.stock_list("nvda, aapl") == ("NVDA", "AAPL")
    df = pd.DataFrame({"symbol": ["NVDA", "AAPL", "NVDA"], "close": [1.0, 2.0, 3.0]})
    parts = fd.split_by_symbol(df, ("NVDA", "AAPL", "TSLA"))
    assert sorted(parts) == ["AAPL", "NVDA"] and len(parts["NVDA"]) == 2


def test_exit_at_a_set_time():
    rows = [(100, 100, 100, 100)] + [(100, 101, 99, 100.5)] * 200      # 10:00 → 13:20
    mk = _market(rows)
    trades = lab.run(mk, [lab.Signal(mk.t[0], True, 90.0, k=3.0, until="12:00")], 0.0, "rth")
    assert trades == [("2025-03-03", 0.5, 10.0, 10)]                      # closed before 12:00


def _orb_day(range_pts, breakout_close):
    """09:30-09:45 range of `range_pts`, then a 5-minute close at `breakout_close`."""
    rows = [(100, 100 + range_pts, 100, 100)] * 15
    rows += [(100, max(100, breakout_close), min(100, breakout_close), breakout_close)] * 5
    rows += [(breakout_close, breakout_close + 1, breakout_close - 1, breakout_close)] * 30
    return _market(rows, start="2025-03-03 09:30")


def test_opening_range_stop_limits_and_one_side():
    mk = _orb_day(70, 180)                               # range 100-170, closes at 180: stop 80 pts
    assert lab.orb(mk, 15, 1.5, "opuesto")[0].stop == 100
    assert lab.orb(mk, 15, 1.5, "opuesto", max_stop=60) == []                 # day skipped
    assert lab.orb(mk, 15, 1.5, "opuesto", max_stop=50, cap="recortar")[0].stop == 130
    assert lab.orb(mk, 15, 1.5, "opuesto", side="venta") == []
    assert lab.orb(mk, 15, 1.5, "opuesto", side="compra")[0].long


def test_losing_streak():
    assert lab.streak([10, -1, -2, 5, -1, -1, -1, 3]) == (3, -3.0)


def test_price_scaling_uses_the_previous_close():
    idx = pd.date_range("2025-03-03 09:30", periods=3, freq="1min").append(
        pd.date_range("2025-03-04 09:30", periods=3, freq="1min"))
    df = pd.DataFrame({"open": 30000.0, "high": 30001.0, "low": 29999.0, "close": 30000.0,
                       "volume": 1.0}, index=idx)
    mk = lab.Market(df)
    assert list(mk.scale_at(idx[-1:])) == [1.0]                   # points: no scaling
    mk.scaled = True
    assert mk.scale_at(idx[-1:])[0] == pytest.approx(2.0)         # 30,000 / 15,000


def test_ghost_like_stop_is_20_to_35_points():
    mk = _random_market()
    b = mk.bars("1min")
    close = dict(zip(b["end"].to_numpy(), b["close"].to_numpy()))
    sigs = lab.zones(mk, "1min", 3.0, stop="ghost")
    assert sigs
    for s in sigs:
        assert 20 - 1e-9 <= abs(close[s.ready] - s.stop) <= 35 + 1e-9


def test_profit_locks_steps_and_partial():
    from types import SimpleNamespace
    L = lab
    # long at 100, stop 90, target 130; runs to 127, then falls back to 95
    m = SimpleNamespace(h=np.array([101, 110, 122, 125, 127, 120, 112.0]),
                        l=np.array([99, 100, 108, 118, 119, 100, 95.0]),
                        o=np.array([100, 101, 110, 122, 125, 120, 110.0]))
    m.c = m.o
    assert L._exit(m, 0, 7, True, 90, 130, 100)[1] == 110                   # flat at the end
    assert L._exit(m, 0, 7, True, 90, 130, 100, [(0.7, 0.0)])[1] == 102     # BE+2
    steps = [(0.5, 0.0), (0.7, 0.3), (0.9, 0.6)]
    assert L._exit(m, 0, 7, True, 90, 130, 100, steps)[1] == 120            # 60% of the way + 2
    assert L._exit(m, 0, 7, True, 90, 130, 100, None, 2.0, (0.7, 0.5))[1] == 111.5


def test_night_sessions_and_protection_study_names():
    ts = lambda hm: pd.Timestamp(f"2026-06-10 {hm}")          # noqa: E731
    assert lab.entry_open(ts("10:00"), "rth") and not lab.entry_open(ts("10:00"), "noche")
    assert lab.entry_open(ts("20:00"), "noche") and not lab.entry_open(ts("20:00"), "rth")
    assert not lab.entry_open(ts("16:30"), "noche") and not lab.entry_open(ts("15:45"), "noche")
    assert lab.entry_open(ts("03:00"), "all")
    names = [n for n, _ in lab.PROTECTIONS]
    assert len(names) == 7 and names[0] == "sin protección"
    assert "Zonas 15 min · de día · sin protección" in lab.BOT_LIKE
