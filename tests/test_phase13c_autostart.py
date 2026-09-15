"""Tests for Phase 13c — automatic 24/7 startup (RUNNING/ONLINE without a manual
START), while keeping every manual control and safety guarantee.

Demonstrates:
  1. autostart enabled  -> BotService comes up ONLINE,
  2. autostart disabled -> current behaviour preserved (OFFLINE),
  3. manual STOP still stops the bot,
  4. manual PAUSE still works,
  5. a restarted process recovers to ONLINE (models a launchd relaunch),
  6. no duplicate candle processing after autostart,
  7. invalid config never produces an unsafe start,
plus the BOT_AUTOSTART alias and the ensure-autostart.sh mechanism the macOS
installer uses.

No engine logic is touched — autostart is just the runtime being told to
start() once at boot.
"""
import subprocess
from pathlib import Path

import pytest

from signal_engine.config.runtime_settings import ServerSettings
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.live.feed import LiveFeed
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.runtime.engine import BotRuntime
from signal_engine.runtime.models import BotStatus
from signal_engine.runtime.service import BotService
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "tests" / "data_samples"


def _feed(engine, bars_per_poll=100000, preload=True):
    raw = CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26")
    hist = normalize(raw, engine.cfg.data, "1m")
    f = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=bars_per_poll),
                "ES", engine.cfg)
    if preload:
        f.poll()
    return f


def _runtime(engine, registry, feed):
    return BotRuntime(engine, SetupEngine(engine.cfg, registry),
                      SignalEngine(engine.cfg.signals), registry,
                      live_feeds={"ES": feed})


def _service(rt, auto_start):
    return BotService(rt, cycle_seconds=0.5, auto_start=auto_start)


# --------------------------------------------------------------------------- #
# 1 + 2: autostart on/off
# --------------------------------------------------------------------------- #
def test_autostart_enabled_comes_up_online(engine, registry):
    rt = _runtime(engine, registry, _feed(engine))
    svc = _service(rt, auto_start=True)
    assert rt.status is BotStatus.OFFLINE          # before the service runs
    svc.start_background()
    try:
        assert rt.status is BotStatus.ONLINE       # started automatically, no manual START
    finally:
        svc.request_stop()
        if svc._thread:
            svc._thread.join(timeout=3)


def test_autostart_disabled_stays_offline(engine, registry):
    rt = _runtime(engine, registry, _feed(engine))
    svc = _service(rt, auto_start=False)
    svc.start_background()
    try:
        assert rt.status is BotStatus.OFFLINE      # unchanged legacy behaviour
    finally:
        svc.request_stop()
        if svc._thread:
            svc._thread.join(timeout=3)


def test_run_forever_autostart_online(engine, registry):
    import threading, time
    rt = _runtime(engine, registry, _feed(engine, bars_per_poll=50))
    svc = _service(rt, auto_start=True)
    t = threading.Thread(target=lambda: svc.run_forever(install_signal_handlers=False),
                         daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and rt.status is not BotStatus.ONLINE:
        time.sleep(0.05)
    assert rt.status is BotStatus.ONLINE
    svc.request_stop(); t.join(timeout=3)
    assert rt.status is BotStatus.OFFLINE          # graceful shutdown stopped it


# --------------------------------------------------------------------------- #
# 3 + 4: manual STOP / PAUSE still work
# --------------------------------------------------------------------------- #
def test_manual_stop_after_autostart(engine, registry):
    rt = _runtime(engine, registry, _feed(engine))
    svc = _service(rt, auto_start=True)
    svc.start_background()
    try:
        assert rt.status is BotStatus.ONLINE
        rt.stop()                                  # manual STOP
        assert rt.status is BotStatus.OFFLINE
        before = svc.cycles_run
        svc.tick()                                 # must not trade while OFFLINE
        assert svc.cycles_run == before
    finally:
        svc.request_stop()
        if svc._thread:
            svc._thread.join(timeout=3)


def test_manual_pause_after_autostart(engine, registry):
    rt = _runtime(engine, registry, _feed(engine))
    svc = _service(rt, auto_start=True)
    svc.start_background()
    try:
        rt.pause()                                 # manual PAUSE
        assert rt.status is BotStatus.PAUSED
        before = svc.cycles_run
        svc.tick()
        assert svc.cycles_run == before            # no trading while paused
    finally:
        svc.request_stop()
        if svc._thread:
            svc._thread.join(timeout=3)


# --------------------------------------------------------------------------- #
# 5: a restarted process recovers to ONLINE
# --------------------------------------------------------------------------- #
def test_restarted_process_recovers_online(engine, registry):
    # First lifecycle, then STOP (as if the user stopped it).
    rt1 = _runtime(engine, registry, _feed(engine))
    svc1 = _service(rt1, auto_start=True)
    svc1.start_background(); svc1.request_stop()
    if svc1._thread:
        svc1._thread.join(timeout=3)
    rt1.stop()
    assert rt1.status is BotStatus.OFFLINE

    # launchd relaunch = a brand-new process/runtime with autostart -> ONLINE,
    # never stuck waiting for a manual START.
    rt2 = _runtime(engine, registry, _feed(engine))
    svc2 = _service(rt2, auto_start=True)
    svc2.start_background()
    try:
        assert rt2.status is BotStatus.ONLINE
    finally:
        svc2.request_stop()
        if svc2._thread:
            svc2._thread.join(timeout=3)


# --------------------------------------------------------------------------- #
# 6: no duplicate candle processing after autostart
# --------------------------------------------------------------------------- #
def test_no_duplicate_cycles_after_autostart(engine, registry):
    rt = _runtime(engine, registry, _feed(engine))     # fully preloaded, exhausted
    svc = _service(rt, auto_start=True)
    svc.start_background()
    svc.request_stop()
    if svc._thread:
        svc._thread.join(timeout=3)
    # drive ticks deterministically (loop stopped): first runs, rest skip
    svc.tick(); svc.tick(); svc.tick()
    assert svc.cycles_run == 1
    assert svc.duplicates_skipped >= 1


# --------------------------------------------------------------------------- #
# 7: invalid config never starts unsafely
# --------------------------------------------------------------------------- #
def test_remote_without_token_refuses_to_start(monkeypatch):
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    with pytest.raises(RuntimeError):
        ServerSettings(mode="remote").validate()   # run_dashboard calls this before serving


def test_invalid_autostart_value_is_safe_false(monkeypatch):
    monkeypatch.setenv("BOT_AUTO_START", "definitely-not-a-bool")
    monkeypatch.delenv("BOT_AUTOSTART", raising=False)
    assert ServerSettings.from_env().auto_start is False   # never forced on by garbage


# --------------------------------------------------------------------------- #
# BOT_AUTOSTART alias
# --------------------------------------------------------------------------- #
def test_autostart_alias_accepted(monkeypatch):
    monkeypatch.delenv("BOT_AUTO_START", raising=False)
    monkeypatch.setenv("BOT_AUTOSTART", "true")
    assert ServerSettings.from_env().auto_start is True


def test_canonical_wins_over_alias(monkeypatch):
    monkeypatch.setenv("BOT_AUTO_START", "false")
    monkeypatch.setenv("BOT_AUTOSTART", "true")
    assert ServerSettings.from_env().auto_start is False


# --------------------------------------------------------------------------- #
# ensure-autostart.sh — the mechanism the macOS installer uses
# --------------------------------------------------------------------------- #
ENSURE = ROOT / "deploy" / "macos" / "ensure-autostart.sh"


def _run_ensure(path: Path):
    return subprocess.run(["bash", str(ENSURE), str(path)],
                          capture_output=True, text=True, check=True)


def test_ensure_autostart_flips_false_to_true_preserving_other_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_MODE=local\nBOT_AUTO_START=false\nDASHBOARD_TOKEN=abc\n")
    _run_ensure(env)
    lines = env.read_text().splitlines()
    assert "BOT_AUTO_START=true" in lines
    assert "BOT_MODE=local" in lines
    assert "DASHBOARD_TOKEN=abc" in lines          # untouched


def test_ensure_autostart_appends_when_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_MODE=local\n")
    _run_ensure(env)
    assert "BOT_AUTO_START=true" in env.read_text().splitlines()


def test_ensure_autostart_is_noop_when_already_true(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BOT_AUTO_START=true\nBOT_MODE=local\n")
    _run_ensure(env)
    assert env.read_text() == "BOT_AUTO_START=true\nBOT_MODE=local\n"


def test_ensure_autostart_creates_file_when_absent(tmp_path):
    env = tmp_path / ".env"
    _run_ensure(env)
    assert env.read_text().strip() == "BOT_AUTO_START=true"
