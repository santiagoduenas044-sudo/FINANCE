from datetime import timedelta

import pandas as pd

from signal_engine.config.schema import DataConfig
from signal_engine.data.ingestion import normalize


def _raw(times):
    idx = pd.DatetimeIndex(pd.to_datetime(times), name="raw_ts")
    return pd.DataFrame(
        {"open": 1.0, "high": 1.5, "low": 0.5, "close": 1.2, "volume": 10.0},
        index=idx,
    )


def test_dst_offsets_winter_vs_summer():
    cfg = DataConfig(input_timestamp_convention="close")
    raw = _raw(["2026-01-15 12:00:00", "2026-07-15 12:00:00"])
    out = normalize(raw, cfg, "1m")
    offs = [t.utcoffset() for t in out.index]
    assert offs[0] == timedelta(hours=-5)   # EST
    assert offs[1] == timedelta(hours=-4)   # EDT


def test_close_labeling_adds_one_interval():
    cfg = DataConfig(input_timestamp_convention="open")
    raw = _raw(["2026-06-15 09:30:00"])
    out = normalize(raw, cfg, "1m")
    # open 09:30 -> close label 09:31
    assert out.index[0] == pd.Timestamp("2026-06-15 09:31", tz="America/New_York")


def test_nonexistent_spring_forward_shifted():
    # 2026-03-08 02:00 -> 03:00 spring forward; 02:30 is nonexistent
    cfg = DataConfig(input_timestamp_convention="close",
                     dst_nonexistent="shift_forward")
    raw = _raw(["2026-03-08 02:30:00"])
    out = normalize(raw, cfg, "1m")
    # shifted forward out of the gap
    assert out.index[0].utcoffset() == timedelta(hours=-4)


def test_tz_aware_input_converted():
    cfg = DataConfig(input_timestamp_convention="close")
    idx = pd.DatetimeIndex(pd.to_datetime(["2026-06-15 16:00:00+00:00"]), name="raw_ts")
    raw = pd.DataFrame({"open": 1.0, "high": 1.5, "low": 0.5, "close": 1.2, "volume": 10.0}, index=idx)
    out = normalize(raw, cfg, "1m")
    # 16:00 UTC -> 12:00 ET (EDT)
    assert out.index[0] == pd.Timestamp("2026-06-15 12:00", tz="America/New_York")
