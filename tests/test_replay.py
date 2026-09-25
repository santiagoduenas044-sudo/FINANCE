"""Bar replay data: a window around a day, resampled, stamped by bar open."""
import pandas as pd
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.api.backtest import BacktestManager


def test_replay_window_resamples_and_rejects_bad_input(tmp_path):
    idx = pd.date_range("2024-03-04 09:30", "2024-03-06 16:00", freq="1min")
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1},
                      index=idx.rename("datetime"))
    df.iloc[:5, 1] = [3, 4, 5, 6, 7]                       # highs of the first 5 minutes
    (tmp_path / "market_data" / "NQ").mkdir(parents=True)
    df.to_csv(tmp_path / "market_data" / "NQ" / "DATABENTO_1m.csv")
    c = TestClient(create_app(None, backtests=BacktestManager(tmp_path)))

    r = c.get("/api/replay/bars", params={"date": "2024-03-05", "tf": "5m",
                                          "days_before": 1, "days_after": 1}).json()
    first = r["bars"][0]
    assert first["h"] == 7.0 and first["v"] == 5.0          # 09:30-09:34 in one bar
    # stamped by bar OPEN, 09:30 New York (EST in March 4 -> UTC-5)
    assert first["t"] == int(pd.Timestamp("2024-03-04 14:30", tz="UTC").timestamp())
    assert (tmp_path / "market_data" / "NQ" / "DATABENTO_1m.parquet").exists()
    assert c.get("/api/replay/bars", params={"date": "2024-03-05", "tf": "7m"}).status_code == 400
    assert c.get("/api/replay/bars", params={"date": "x", "data": "../etc"}).status_code == 400
