"""GHOST on 15-minute zones (stop 20-35 pts from the entry, one target at 3x,
no breakeven), charts with !status and stop moves, and won/lost/net totals."""
from types import SimpleNamespace

import pandas as pd
import pytest

from signal_engine.config.schema import EngineConfig, ZoneRetraceParams

T0 = pd.Timestamp("2026-06-10 10:00", tz="America/New_York")


def _bars(rows, start, freq):
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def test_live_config_is_15_minute_zones_without_breakeven():
    cfg = EngineConfig.load(strategy="zones")
    z = cfg.setups.zone_retrace
    assert (z.timeframe, z.zone_timeframe, cfg.timeframe_roles.structure) == ("15m", "15m", "15m")
    assert cfg.timeframe_roles.execution == "1m"             # stop/target still checked every minute
    assert z.stop_from == "fill" and z.fixed_target_r == 3.0 and z.single_target
    assert (z.min_stop_points, z.max_stop_points, z.confirm_bars) == (20.0, 35.0, 2)
    assert cfg.ghost.be_at_progress is None
    # same rules as the lab: no volatility veto, no pause after a stop
    assert cfg.ghost.max_atr_percentile >= 1.0 and cfg.ghost.cooldown_bars == 0


def test_stop_and_target_from_the_entry():
    from signal_engine.data.context import AnalysisContext, TimeframeView
    from signal_engine.features.analyzer import MarketAnalysis
    from signal_engine.setups.zone_retrace import ZoneRetraceDetector

    # bullish 15m gap 100-110; the move started at 94 (origin 93 with the buffer);
    # the next 15m candle dips into the zone and closes up, past the middle
    z15 = _bars([(95, 100, 94, 99), (99, 118, 98, 117), (117, 125, 110, 124)],
                T0 - pd.Timedelta(minutes=45), "15min")
    conf = _bars([(104, 112, 103, 108)], z15.index[-1] + pd.Timedelta(minutes=15), "15min")
    ex = pd.concat([z15, conf])
    ctx = AnalysisContext("NQ", ex.index[-1], "RTH", htf=TimeframeView("1H", z15.iloc[:0]),
                          structure=TimeframeView("15m", ex), execution=TimeframeView("1m", ex))
    p = ZoneRetraceParams(enabled=True, timeframe="15m", zone_timeframe="15m",
                          fixed_target_r=3.0, single_target=True, stop_from="fill")
    (c,) = ZoneRetraceDetector(p).detect(ctx, MarketAnalysis("NQ", ex.index[-1], {}))
    # entry 108, origin 93: 15 pts -> raised to the 20-pt floor; target 3 x 20
    assert c.entry == 108 and c.stop_loss == 88 and c.take_profits == [168]


def test_record_shows_won_lost_and_net():
    from signal_engine.output.record import won_lost
    assert won_lost([10.0, -4.0, 6.0], 10.0) == "ganado +$160 · perdido −$40 · **neto +$120**"


def test_status_comes_with_a_chart():
    from signal_engine.output.discord_bot import build_files

    rt = SimpleNamespace(status_charts=lambda: [b"png1", b"png2"])
    assert build_files("!status", rt) == [b"png1", b"png2"]
    assert build_files("!record", rt) == []


def test_status_chart_of_the_market_when_flat():
    pytest.importorskip("matplotlib")
    from signal_engine.runtime.engine import BotRuntime

    bars = _bars([(100 + i, 102 + i, 99 + i, 101 + i) for i in range(120)], T0, "1min")
    feed = SimpleNamespace(get_tf_bars=lambda: {"1m": bars})
    cfg = EngineConfig.load(strategy="zones")
    rt = SimpleNamespace(data_engine=SimpleNamespace(cfg=cfg), live_feeds={"NQ": feed},
                         paper_tracker=None)
    (png,) = BotRuntime.status_charts(rt)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_replay_keeps_its_own_folder(tmp_path):
    from signal_engine.runtime.bootstrap import build_runtime

    cfg = EngineConfig.load(strategy="zones")
    rt, _ = build_runtime([], config=cfg, signal_log_path=tmp_path / "s.jsonl",
                          data_dir=tmp_path)
    assert rt.ghost_log_path == tmp_path / "ghost_log.jsonl"
    assert rt.paper_tracker._store_dir == tmp_path / "paper"
    assert rt.idea_board is not None


def test_ghost_porque_lists_each_idea_with_its_reason(tmp_path):
    import json
    from signal_engine.runtime.probar import read_log

    rows = [
        {"kind": "SIGNAL", "setup": "zone_retrace", "timestamp": "2026-09-28T10:15:00-04:00",
         "direction": "SHORT", "entry": 24500.0, "stop": 24530.0, "take_profits": [24410.0]},
        {"kind": "NO_TRADE", "setup": "zone_retrace", "timestamp": "2026-09-29T09:45:00-04:00",
         "reasons": ["excessive_volatility"], "candidate_direction": "LONG"},
        {"kind": "NO_TRADE", "setup": "zone_retrace", "timestamp": "2026-09-29T11:00:00-04:00",
         "reasons": ["daily_limit"], "candidate_direction": "SHORT"},
        {"kind": "NO_TRADE", "setup": "zone_retrace", "timestamp": "2026-09-29T11:01:00-04:00",
         "reasons": ["duplicate"], "candidate_direction": "SHORT"},
    ]
    log = tmp_path / "signal_log.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows))
    text = read_log(2, log)
    assert "SHORT → TOMADO  entrada 24,500.00 · stop 24,530.00 · objetivo 24,410.00" in text
    assert "LONG  → descartada: volatilidad excesiva en ese momento" in text
    assert "límite del día" in text
    assert "ya la había evaluado" not in text                 # repeats are left out
    assert read_log(1, log).count("descartada") == 2 and "TOMADO" not in read_log(1, log)
