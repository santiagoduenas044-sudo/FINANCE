"""The public website: users with passwords, signed sessions, read-only
viewers, throttled logins, and never public without a lock."""
from fastapi.testclient import TestClient

from signal_engine.api.accounts import AccountError, Accounts, check_password, hash_password
from signal_engine.api.app import create_app
from signal_engine.api.backtest import BacktestManager
from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from tests.test_bot_runtime import _runtime


def _client(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    app = create_app(_runtime(DataEngine(EngineConfig.load(strategy="zones"))),
                     settings=ServerSettings(), backtests=BacktestManager(tmp_path))
    return TestClient(app), Accounts(tmp_path)


def test_passwords_are_hashed_and_checked():
    h = hash_password("correcto123")
    assert "correcto123" not in h and check_password("correcto123", h)
    assert not check_password("otra-cosa", h)


def test_no_users_no_token_stays_open_but_never_through_funnel(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch)
    assert c.get("/api/status").status_code == 200
    r = c.get("/api/status", headers={"Tailscale-Funnel-Request": "?1"})
    assert r.status_code == 403 and "usuario" in r.json()["detail"]


def test_login_session_and_roles(tmp_path, monkeypatch):
    c, acc = _client(tmp_path, monkeypatch)
    acc.add("santi", "clave-segura-1", "owner")
    acc.add("amigo", "clave-amigo-1", "viewer")
    assert c.get("/api/status").status_code == 401                 # locked by the first user
    assert c.get("/login").status_code == 200 and c.get("/").status_code == 200
    assert c.post("/api/login", json={"user": "santi", "password": "mal"}).status_code == 401
    r = c.post("/api/login", json={"user": "santi", "password": "clave-segura-1"})
    assert r.status_code == 200 and "ghost_session" in r.cookies
    assert "httponly" in r.headers["set-cookie"].lower()
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/me").json()["role"] == "owner"
    assert c.post("/api/users", json={"user": "otro", "password": "corta", "role": "viewer"}).status_code == 400
    assert c.get("/api/users").status_code == 200
    # a viewer sees everything but changes nothing
    c.post("/api/logout")
    c.cookies.clear()
    assert c.get("/api/status").status_code == 401
    c.post("/api/login", json={"user": "amigo", "password": "clave-amigo-1"})
    assert c.get("/api/status").status_code == 200 and c.get("/api/bots").status_code == 200
    assert c.post("/api/control/start").status_code == 403
    assert c.post("/api/system/update").status_code == 403
    assert c.get("/api/users").status_code == 403
    assert c.get("/api/practice/link").status_code == 403
    # a deleted user's session stops working at once
    acc.remove("amigo")
    assert c.get("/api/status").status_code == 401


def test_owner_token_still_works_and_cookies_cannot_be_forged(tmp_path, monkeypatch):
    c, acc = _client(tmp_path, monkeypatch)
    acc.add("santi", "clave-segura-1", "owner")
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok")
    assert c.get("/api/status?token=tok").status_code == 200
    good = acc.issue("santi")
    body, _sig = good.rsplit(".", 1)
    c.cookies.set("ghost_session", body + ".forged")
    assert c.get("/api/status").status_code == 401


def test_logins_are_throttled(tmp_path, monkeypatch):
    c, acc = _client(tmp_path, monkeypatch)
    acc.add("santi", "clave-segura-1", "owner")
    for _ in range(5):
        assert c.post("/api/login", json={"user": "santi", "password": "x"}).status_code == 401
    r = c.post("/api/login", json={"user": "santi", "password": "clave-segura-1"})
    assert r.status_code == 429


def test_last_owner_cannot_be_deleted(tmp_path):
    acc = Accounts(tmp_path)
    acc.add("santi", "clave-segura-1", "owner")
    try:
        acc.remove("santi")
        raise AssertionError("should refuse")
    except AccountError:
        pass
