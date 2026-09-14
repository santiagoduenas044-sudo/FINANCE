from datetime import date

import pandas as pd
from pandas.testing import assert_frame_equal

from signal_engine.data import storage
from tests.conftest import FakeProvider, make_rth_1m

SYMBOL = "ES"
CONTRACT = "ESM26"


def _run(engine, provider):
    lc = engine.load_contract(provider, SYMBOL, CONTRACT, "1m")
    engine.build_timeframes(lc)
    return lc


def test_roundtrip_parquet(engine, tmp_path):
    raw = make_rth_1m([date(2026, 6, 15)], seed=7)
    provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc = _run(engine, provider)
    engine.cfg.storage.root = str(tmp_path / "store")
    engine.store_timeframes(lc)
    for tf, df in lc.timeframes.items():
        back = storage.read_bars(engine.cfg.storage.root, SYMBOL, CONTRACT, tf)
        # parquet does not preserve the index freq attribute (metadata, not data)
        assert_frame_equal(df, back, check_freq=False)


def test_content_hash_reproducible(engine):
    """Same inputs + config => identical normalized data & content hash."""
    raw = make_rth_1m([date(2026, 6, 15)], seed=7)
    p1 = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    p2 = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc1 = _run(engine, p1)
    lc2 = _run(engine, p2)
    for tf in lc1.timeframes:
        assert_frame_equal(lc1.timeframes[tf], lc2.timeframes[tf])
        assert storage.content_hash(lc1.timeframes[tf]) == storage.content_hash(lc2.timeframes[tf])


def test_meta_records_hash(engine, tmp_path):
    raw = make_rth_1m([date(2026, 6, 15)], seed=7)
    provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})
    lc = _run(engine, provider)
    engine.cfg.storage.root = str(tmp_path / "store")
    engine.store_timeframes(lc)
    meta = storage.read_meta(engine.cfg.storage.root, SYMBOL, CONTRACT, "5m")
    assert meta["content_hash"] == storage.content_hash(lc.timeframes["5m"])
    assert meta["rows"] == len(lc.timeframes["5m"])
