"""Prove the DataProvider can be swapped without affecting the rest of the
system: a CSV-backed provider and an in-memory provider yield identical
normalized/validated/annotated output for the same underlying data. Adding
Databento later is the same contract."""
from datetime import date

from pandas.testing import assert_frame_equal

from signal_engine.data.providers.csv_provider import CsvProvider
from tests.conftest import FakeProvider, make_rth_1m

SYMBOL = "ES"
CONTRACT = "ESM26"


def test_csv_and_memory_providers_match(engine, tmp_path):
    raw = make_rth_1m([date(2026, 6, 15)], seed=11)

    # write CSV in the CsvProvider layout: <root>/<SYMBOL>/<CONTRACT>_<tf>.csv
    d = tmp_path / SYMBOL
    d.mkdir(parents=True)
    csv = raw.copy()
    csv.index.name = "datetime"
    csv.to_csv(d / f"{CONTRACT}_1m.csv")

    csv_provider = CsvProvider(tmp_path)
    mem_provider = FakeProvider({(SYMBOL, "1m", CONTRACT): raw})

    lc_csv = engine.load_contract(csv_provider, SYMBOL, CONTRACT, "1m")
    lc_mem = engine.load_contract(mem_provider, SYMBOL, CONTRACT, "1m")

    assert_frame_equal(lc_csv.bars, lc_mem.bars)

    # and downstream timeframes match too
    engine.build_timeframes(lc_csv)
    engine.build_timeframes(lc_mem)
    for tf in lc_csv.timeframes:
        assert_frame_equal(lc_csv.timeframes[tf], lc_mem.timeframes[tf])


def test_csv_provider_lists_contracts(tmp_path):
    d = tmp_path / SYMBOL
    d.mkdir(parents=True)
    (d / "ESM26_1m.csv").write_text("datetime,open,high,low,close,volume\n")
    (d / "ESU26_1m.csv").write_text("datetime,open,high,low,close,volume\n")
    provider = CsvProvider(tmp_path)
    assert provider.list_contracts(SYMBOL) == ["ESM26", "ESU26"]
