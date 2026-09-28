"""Updating and restarting the bot from the dashboard (no Terminal needed)."""
import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.api.backtest import BacktestManager

ROOT = Path(__file__).resolve().parents[1]


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin"})


def _repos(tmp_path):
    """origin (bare) + the Mac's clone, on branch 'work'."""
    origin, mac, dev = tmp_path / "origin.git", tmp_path / "mac", tmp_path / "dev"
    _git(tmp_path, "init", "-q", "--bare", "-b", "work", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    (dev / "a.txt").write_text("1")
    _git(dev, "checkout", "-q", "-b", "work")
    _git(dev, "add", ".")
    _git(dev, "commit", "-q", "-m", "primera")
    _git(dev, "push", "-q", "origin", "work")
    _git(tmp_path, "clone", "-q", "-b", "work", str(origin), str(mac))
    return mac, dev


def _client(root, supervised=True):
    calls = []
    app = create_app(None, backtests=BacktestManager(root), restart_fn=lambda: calls.append(1),
                     supervised=supervised)
    return TestClient(app), calls


def test_update_pulls_new_commits_and_restarts(tmp_path):
    mac, dev = _repos(tmp_path)
    client, restarts = _client(mac)
    assert client.get("/api/system/version").json()["subject"] == "primera"

    r = client.post("/api/system/update").json()
    assert r["updated"] is False and restarts == []          # nothing new: no restart

    (dev / "a.txt").write_text("2")
    _git(dev, "commit", "-q", "-am", "salida 3× el stop")
    _git(dev, "push", "-q", "origin", "work")
    r = client.post("/api/system/update").json()
    assert r["updated"] and r["commits"][0].endswith("salida 3× el stop")
    assert r["restarting"] is True and restarts == [1]
    assert (mac / "a.txt").read_text() == "2"


def test_restart_refused_during_a_backtest_or_outside_launchd(tmp_path):
    mac, _ = _repos(tmp_path)
    client, restarts = _client(mac, supervised=False)
    r = client.post("/api/system/restart")
    assert r.status_code == 409 and "launchd" in r.json()["detail"] and restarts == []

    client, restarts = _client(mac)
    mgr = BacktestManager(mac)
    mgr.running = lambda: True                                # a backtest in progress
    app = create_app(None, backtests=mgr, restart_fn=lambda: restarts.append(1), supervised=True)
    r = TestClient(app).post("/api/system/restart")
    assert r.status_code == 409 and "backtest" in r.json()["detail"] and restarts == []


def test_backtest_reports_are_readable_from_the_phone(tmp_path):
    root = tmp_path / "repo"
    (root / "data_store" / "backtests").mkdir(parents=True)
    (root / "scripts").symlink_to(ROOT / "scripts")
    run = {"meta": {"symbol": "NQ", "strategy": "zones", "point_value": 10.0, "position": "5 MNQ"},
           "total": {"n": 40, "wr": 0.4, "avg_pts": 1.0, "pts": 40.0, "dd_pts": 30.0, "pf": 1.1},
           "equity": [{"t": "2025-01-02T10:00:00", "cum_pts": 5.0}]}
    (root / "data_store" / "backtests" / "r1.json").write_text(json.dumps(run))
    client, _ = _client(root)
    text = client.get("/api/backtest/runs/r1/report?kind=summary").json()["text"]
    assert "+$400" in text and "5 MNQ" in text
    assert client.get("/api/backtest/runs/r1/report?kind=filters").status_code == 404
    assert client.get("/api/backtest/runs/..%2Fx/report").status_code == 404


def test_strategy_lab_runs_from_the_phone(tmp_path):
    import time
    from signal_engine.api.lab import LabManager
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "strategy_lab.py").write_text(
        "import sys, time\ntime.sleep(1.5)\nprint('✔ Zonas 15 min', sys.argv[1:])\n")
    restarts = []
    app = create_app(None, backtests=BacktestManager(root), lab=LabManager(root),
                     restart_fn=lambda: restarts.append(1), supervised=True)
    c = TestClient(app)
    assert c.get("/api/lab/status").json()["state"] == "idle"
    assert c.post("/api/lab/run?position=10nq").status_code == 400
    assert c.post("/api/lab/run?position=1nq&sessions=all").json()["state"] == "running"
    r = c.post("/api/system/restart")                        # would kill the lab
    assert r.status_code == 409 and "laboratorio" in r.json()["detail"] and restarts == []
    deadline = time.time() + 30
    while c.get("/api/lab/status").json()["state"] == "running" and time.time() < deadline:
        time.sleep(0.3)
    st = c.get("/api/lab/status").json()
    assert st["state"] == "done" and "✔ Zonas 15 min" in st["text"] and "'1nq'" in st["text"]
    # a fresh manager (after a restart) still shows the last report
    assert "✔ Zonas 15 min" in LabManager(root).status()["text"]
