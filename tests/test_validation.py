import pandas as pd
import pytest

from signal_engine.config.schema import ValidationConfig
from signal_engine.data.validation import Severity, validate

TZ = "America/New_York"


def _df(index, o, h, l, c, v):
    idx = pd.DatetimeIndex(index, tz=TZ, name="timestamp")
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": v}, index=idx)


def test_detects_duplicates():
    df = _df(["2026-06-15 09:31", "2026-06-15 09:31"],
             [1, 1], [2, 2], [0.5, 0.5], [1.5, 1.5], [5, 5])
    _, rep = validate(df, ValidationConfig(), pd.Timedelta("1min"))
    codes = {i.code for i in rep.issues}
    assert "duplicate_timestamps" in codes
    assert not rep.ok


def test_detects_out_of_order():
    df = _df(["2026-06-15 09:32", "2026-06-15 09:31"],
             [1, 1], [2, 2], [0.5, 0.5], [1.5, 1.5], [5, 5])
    _, rep = validate(df, ValidationConfig(), pd.Timedelta("1min"))
    assert "out_of_order" in {i.code for i in rep.issues}


def test_detects_ohlcv_integrity():
    # high < low  and negative volume
    df = _df(["2026-06-15 09:31", "2026-06-15 09:32"],
             [1, 1], [0.4, 2], [0.5, 0.5], [1.5, 1.5], [5, -3])
    _, rep = validate(df, ValidationConfig(), pd.Timedelta("1min"))
    assert "ohlcv_integrity" in {i.code for i in rep.issues}
    assert not rep.ok


def test_drop_invalid_policy():
    df = _df(["2026-06-15 09:31", "2026-06-15 09:32"],
             [1, 1], [0.4, 2], [0.5, 0.5], [1.5, 1.5], [5, 3])  # first row bad high<low
    cfg = ValidationConfig(drop_invalid=True)
    out, rep = validate(df, cfg, pd.Timedelta("1min"))
    assert len(out) == 1
    assert rep.n_dropped == 1


def test_raise_on_error():
    df = _df(["2026-06-15 09:31"], [1], [0.4], [0.5], [1.5], [5])  # high<low
    cfg = ValidationConfig(raise_on_error=True)
    with pytest.raises(ValueError):
        validate(df, cfg, pd.Timedelta("1min"))


def test_valid_data_ok():
    df = _df(["2026-06-15 09:31", "2026-06-15 09:32"],
             [1, 1.5], [2, 2], [0.5, 1.0], [1.5, 1.8], [5, 6])
    _, rep = validate(df, ValidationConfig(), pd.Timedelta("1min"))
    assert rep.ok
