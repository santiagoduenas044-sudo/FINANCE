"""Dashboard backtest endpoints: options, validation, a real run, results."""
import json
import shutil
import time
from pathlib import Path

from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.api.backtest import BacktestManager

ROOT = Path(__file__).resolve().parents[1]


def _app(tmp_path, runtime=None):
    # a private "repo root": the real scripts/ + config, sample data as NQ
    (tmp_path / "market_data" / "ES").mkdir(parents=True)
    shutil.copy(ROOT / "tests" / "data_samples" / "ES" / "ESM26_1m.csv",
                tmp_path / "market_data" / "ES" / "SAMPLE_1m.csv")
    (tmp_path / "scripts").symlink_to(ROOT / "scripts")
    (tmp_path / "signal_engine").symlink_to(ROOT / "signal_engine")
    mgr = BacktestManager(tmp_path)
    return TestClient(create_app(runtime, backtests=mgr)), mgr


def test_backtest_runs_from_the_dashboard(tmp_path):
    client, mgr = _app(tmp_path)
    opts = client.get("/api/backtest/options").json()
    assert {"symbol": "ES", "data": "SAMPLE", "first": "2026-06-07", "last": "2026-06-10",
            "size_mb": opts["datasets"][0]["size_mb"]} == opts["datasets"][0]
    assert "zones" in opts["strategies"]

    bad = client.post("/api/backtest/run", json={"symbol": "ES", "data": "../x",
                                                  "start": "2026-06-10", "end": "2026-06-10"})
    assert bad.status_code == 400

    r = client.post("/api/backtest/run", json={"symbol": "ES", "data": "SAMPLE",
                                                "start": "2026-06-10", "end": "2026-06-10",
                                                "strategy": "zones"})
    assert r.status_code == 200 and r.json()["state"] == "running"
    deadline = time.time() + 240
    while mgr.running() and time.time() < deadline:
        time.sleep(1)
    st = client.get("/api/backtest/status").json()
    assert st["state"] == "done", st
    runs = client.get("/api/backtest/runs").json()["runs"]
    assert runs[0]["meta"]["symbol"] == "ES" and runs[0]["total"]["n"] >= 1
    full = client.get(f"/api/backtest/runs/{runs[0]['id']}").json()
    assert {"equity", "policies", "trades", "by_hour"} <= set(full)
    assert client.get("/api/backtest/runs/..%2Fetc").status_code == 404
