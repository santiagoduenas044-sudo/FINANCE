"""Databento bars convert to the CSV the audit replays (naive ET, bar open)."""
import importlib.util
from pathlib import Path

import pandas as pd

from signal_engine.config.schema import EngineConfig
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider

_spec = importlib.util.spec_from_file_location(
    "fetch_databento", Path(__file__).resolve().parents[1] / "scripts" / "fetch_databento.py")
fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fetch)


def test_databento_bars_load_as_bot_bars(tmp_path):
    # Databento stamps bars by their OPEN, in UTC. 13:30 UTC = 09:30 ET (EDT).
    idx = pd.date_range("2026-06-10 13:30", periods=3, freq="1min", tz="UTC", name="ts_event")
    df = pd.DataFrame({"rtype": 33, "open": [1.0, 2, 3], "high": [2.0, 3, 4],
                       "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5],
                       "volume": [10, 20, 30], "symbol": "NQ.v.0"}, index=idx)
    fetch.to_bot_csv(df, tmp_path / "NQ" / "DATABENTO_1m.csv")
    raw = CsvProvider(tmp_path).get_raw_bars("NQ", "1m", contract="DATABENTO")
    bars = normalize(raw, EngineConfig.load().data, "1m")
    # close-labeled internally: the 09:30 ET bar closes at 09:31 ET
    assert bars.index[0] == pd.Timestamp("2026-06-10 09:31", tz="America/New_York")
    assert list(bars["close"]) == [1.5, 2.5, 3.5]
