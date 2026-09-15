"""Tests for the control/status API (Phase 9). Uses FastAPI's TestClient —
no real network, no real Discord. Confirms the dashboard cannot execute
orders (no such endpoint exists) and never leaks secrets in responses.
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from signal_engine.api.app import create_app
from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.engine import DataEngine
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from tests.conftest import FakeProvider, make_rth_1m, weekdays

FAKE_WEBHOOK = "https://discord.example.invalid/webhook/does-not-exist"


def _pipeline(data_engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 2), seed=7)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)
    return lc


@pytest.fixture
def client(engine):
    registry = ContractRegistry.load()
    se = SetupEngine(engine.cfg, registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    rt = BotRuntime(engine, se, sig_eng, registry)
    app = create_app(rt)
    return TestClient(app), rt


def test_status_endpoint_returns_offline_initially(client):
    c, rt = client
    resp = c.get("/api/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "OFFLINE"


def test_start_endpoint_transitions_status(client):
    c, rt = client
    resp = c.post("/api/control/start")
    assert resp.status_code == 200
    body = resp.json()
    assert body["previous_status"] == "OFFLINE"
    assert body["new_status"] == "ONLINE"
    assert c.get("/api/status").json()["status"] == "ONLINE"


def test_pause_endpoint_requires_online(client):
    c, rt = client
    resp = c.post("/api/control/pause")
    assert resp.status_code == 409
    c.post("/api/control/start")
    resp2 = c.post("/api/control/pause")
    assert resp2.status_code == 200
    assert c.get("/api/status").json()["status"] == "PAUSED"


def test_stop_endpoint_always_succeeds(client):
    c, rt = client
    resp = c.post("/api/control/stop")
    assert resp.status_code == 200
    assert c.get("/api/status").json()["status"] == "OFFLINE"


def test_unknown_action_rejected(client):
    c, rt = client
    resp = c.post("/api/control/launch_missiles")
    assert resp.status_code == 400


def test_no_order_execution_endpoint_exists(client):
    """The dashboard must never be able to place orders. There is no
    dedicated order/trade route (404/405), and the generic control endpoint
    explicitly rejects anything other than start/pause/stop (400) — either
    way, no order is ever reachable."""
    c, rt = client
    for path in ("/api/order", "/api/execute", "/api/trade", "/api/orders"):
        resp = c.post(path)
        assert resp.status_code in (404, 405)
    for action in ("execute", "buy", "sell", "place_order"):
        resp = c.post(f"/api/control/{action}")
        assert resp.status_code == 400   # validated and rejected, never executed


def test_recent_signals_endpoint(client, engine):
    c, rt = client
    rt.start()
    lc = _pipeline(engine)
    rt.run_cycle("ES", lc.timeframes)
    resp = c.get("/api/signals/recent")
    assert resp.status_code == 200
    assert "signals" in resp.json()


def test_recent_signals_limit_validated(client):
    c, rt = client
    assert c.get("/api/signals/recent?limit=0").status_code == 400
    assert c.get("/api/signals/recent?limit=500").status_code == 400
    assert c.get("/api/signals/recent?limit=10").status_code == 200


def test_risk_today_endpoint(client):
    c, rt = client
    resp = c.get("/api/risk/today")
    assert resp.status_code == 200
    body = resp.json()
    assert "trades_today" in body and "daily_realized_pnl" in body


def test_index_page_served(client):
    c, rt = client
    resp = c.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_webhook_never_appears_in_any_api_response(engine):
    registry = ContractRegistry.load()
    se = SetupEngine(engine.cfg, registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True),
                                   webhook_url=FAKE_WEBHOOK,
                                   transport=lambda url, data, timeout: 204,
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt = BotRuntime(engine, se, sig_eng, registry, output_dispatcher=dispatcher)
    app = create_app(rt)
    c = TestClient(app)

    c.post("/api/control/start")
    status_text = c.get("/api/status").text
    signals_text = c.get("/api/signals/recent").text
    assert FAKE_WEBHOOK not in status_text
    assert FAKE_WEBHOOK not in signals_text


def test_control_actions_are_deterministic(client):
    c, rt = client
    r1 = c.post("/api/control/start").json()
    r2 = c.post("/api/control/stop").json()
    r3 = c.post("/api/control/start").json()
    assert r1["new_status"] == r3["new_status"] == "ONLINE"
    assert r2["new_status"] == "OFFLINE"
