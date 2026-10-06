"""Running in the cloud (Docker): an in-app update restarts ORBIT through a
file, and the dashboard stays up when Yahoo is down at boot."""
import yaml

from signal_engine.api import maintenance
from signal_engine.config.schema import EngineConfig
from signal_engine.data.engine import DataEngine
from signal_engine.runtime import bootstrap


def test_in_docker_an_update_tells_orbit_through_a_file(tmp_path, monkeypatch):
    flag = tmp_path / "data_store" / "orbit.restart"
    monkeypatch.setattr(maintenance, "ORBIT_RESTART_FLAG", flag)
    monkeypatch.setenv("BOT_IN_DOCKER", "1")
    assert maintenance.restart_orbit() == "se reinicia en unos segundos" and flag.exists()


def test_yahoo_down_at_boot_keeps_the_real_feed(monkeypatch):
    def down(self):
        raise ConnectionError("no route")
    monkeypatch.setattr(bootstrap.YahooDelayedAdapter, "connect", down)
    feed, info = bootstrap.build_live_feed("NQ", DataEngine(EngineConfig.load()),
                                           allow_simulated_fallback=False)
    assert feed is not None and info.delayed and not info.simulated     # retried every cycle


def test_compose_runs_ghost_orbit_and_https():
    d = yaml.safe_load(open("docker-compose.yml"))
    s = d["services"]
    assert set(s) == {"ghost", "orbit", "caddy"}
    assert s["orbit"]["command"][-1] == "signal_engine.subbots.orbit"
    for name in ("ghost", "orbit"):
        assert s[name]["restart"] == "unless-stopped" and ".:/app" in s[name]["volumes"]
    assert "443:443" in s["caddy"]["ports"]
