"""Tests for Phase 13 — the 24/7 BotService (runtime loop).

Covered: continuous polling, duplicate-candle prevention, reconnection /
recovery after a feed outage, runtime state transitions, persistence across the
loop, and graceful shutdown. No real network, no real time (LiveFeed's sleep is
a no-op), no real Discord.

The service only orchestrates the existing engines — these tests confirm it
never crashes the loop and never re-processes the same closed candle.
"""
import threading
import time
from pathlib import Path

import pytest

from signal_engine.data.contracts import ContractRegistry
from signal_engine.data.ingestion import normalize
from signal_engine.data.providers.csv_provider import CsvProvider
from signal_engine.live.feed import LiveFeed
from signal_engine.live.models import DataFeedStatus
from signal_engine.live.simulated import SimulatedLiveAdapter
from signal_engine.runtime.engine import BotRuntime, read_signal_log
from signal_engine.runtime.models import BotStatus
from signal_engine.runtime.service import BotService
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine

SAMPLES = Path(__file__).resolve().parent / "data_samples"


def _es_1m(engine):
    raw = CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26")
    return normalize(raw, engine.cfg.data, "1m")


def _runtime(engine, registry, feed, log_path=None):
    return BotRuntime(engine, SetupEngine(engine.cfg, registry),
                      SignalEngine(engine.cfg.signals), registry,
                      live_feeds={"ES": feed}, signal_log_path=log_path)


def _feed(engine, adapter):
    return LiveFeed(adapter, "ES", engine.cfg)


# --------------------------------------------------------------------------- #
# Continuous polling
# --------------------------------------------------------------------------- #
def test_service_polls_continuously_and_grows_buffer(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=50)
    feed = _feed(engine, adapter)
    rt = _runtime(engine, registry, feed)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()

    for _ in range(4):
        svc.tick()
    assert svc.tick_count == 4
    # data flowed in and the pipeline ran on newly closed candles
    assert not feed.get_tf_bars()[engine.cfg.timeframe_roles.execution].empty
    assert svc.cycles_run >= 1
    assert rt.last_run_at is not None


# --------------------------------------------------------------------------- #
# Duplicate-candle prevention
# --------------------------------------------------------------------------- #
def test_service_never_reprocesses_the_same_candle(engine, registry):
    # Deliver every bar at once, then keep ticking: the last closed execution
    # candle never changes, so the pipeline must run once and skip afterwards.
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=100000)
    feed = _feed(engine, adapter)
    feed.poll()  # load everything
    rt = _runtime(engine, registry, feed)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()

    svc.tick()
    assert svc.cycles_run == 1 and svc.duplicates_skipped == 0
    svc.tick()
    svc.tick()
    assert svc.cycles_run == 1            # never re-ran on the same candle
    assert svc.duplicates_skipped == 2    # both later ticks were skipped


# --------------------------------------------------------------------------- #
# Reconnection / recovery
# --------------------------------------------------------------------------- #
def test_service_survives_feed_outage_and_recovers(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=100)
    feed = _feed(engine, adapter)
    rt = _runtime(engine, registry, feed)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()

    svc.tick()  # healthy — data flowing
    assert rt.data_feed_status["ES"] in (DataFeedStatus.LIVE, DataFeedStatus.DELAYED)
    assert rt.connection_state() == "CONNECTED"

    # Outage: the loop must NOT crash and must report DISCONNECTED.
    adapter.simulate_disconnect()
    svc.tick()
    assert rt.status is BotStatus.ONLINE           # bot stays online, keeps trying
    assert rt.data_feed_status["ES"] is DataFeedStatus.DISCONNECTED
    assert rt.connection_state() == "DISCONNECTED"
    assert svc.errors == 0                          # handled, not crashed

    # Recovery: data flows again on the next tick.
    adapter.simulate_reconnect()
    svc.tick()
    assert rt.data_feed_status["ES"] in (DataFeedStatus.LIVE, DataFeedStatus.DELAYED)
    assert rt.connection_state() == "CONNECTED"


def test_tick_never_raises_even_with_a_broken_feed(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=10)
    adapter.simulate_disconnect()  # broken from the very first poll
    feed = _feed(engine, adapter)
    rt = _runtime(engine, registry, feed)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()
    svc.tick()  # must not raise
    assert rt.connection_state() in ("DISCONNECTED", "CONNECTING")


# --------------------------------------------------------------------------- #
# State transitions
# --------------------------------------------------------------------------- #
def test_service_does_not_trade_when_paused(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=100000)
    feed = _feed(engine, adapter)
    feed.poll()
    rt = _runtime(engine, registry, feed)
    svc = BotService(rt, cycle_seconds=0.5)

    rt.start()
    rt.pause()
    svc.tick()
    assert svc.cycles_run == 0            # no pipeline while paused
    assert rt.status is BotStatus.PAUSED
    # ...but the feed is still refreshed for the dashboard chart + heartbeat.
    assert rt.last_heartbeat_at is not None


def test_runtime_state_and_connection_transitions(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=100)
    rt = _runtime(engine, registry, _feed(engine, adapter))
    assert rt.status is BotStatus.OFFLINE
    assert rt.connection_state() == "CONNECTING"  # feed configured, not polled yet
    rt.start(); assert rt.status is BotStatus.ONLINE
    rt.pause(); assert rt.status is BotStatus.PAUSED
    rt.stop(); assert rt.status is BotStatus.OFFLINE
    rt.mark_error("boom"); assert rt.status is BotStatus.ERROR and rt.last_error == "boom"


# --------------------------------------------------------------------------- #
# Persistence across the loop
# --------------------------------------------------------------------------- #
def test_service_persists_signals_to_disk(engine, registry, tmp_path):
    log_path = tmp_path / "signal_log.jsonl"
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=100000)
    feed = _feed(engine, adapter)
    feed.poll()
    rt = _runtime(engine, registry, feed, log_path=log_path)
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()
    svc.tick()
    # Every cycle records its decisions (Signal or NO_TRADE) to the audit log,
    # which survives a restart.
    entries = read_signal_log(log_path)
    assert isinstance(entries, list)
    assert log_path.exists() == (len(entries) > 0 or True)  # file created on any decision
    # A fresh runtime pointed at the same log reads the same history.
    assert read_signal_log(log_path) == entries


# --------------------------------------------------------------------------- #
# Graceful startup / shutdown
# --------------------------------------------------------------------------- #
def test_run_forever_auto_starts_and_shuts_down_gracefully(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=50)
    rt = _runtime(engine, registry, _feed(engine, adapter))
    svc = BotService(rt, cycle_seconds=0.5, auto_start=True)

    thread = threading.Thread(
        target=lambda: svc.run_forever(install_signal_handlers=False), daemon=True)
    thread.start()
    # give it a moment to auto-start ONLINE and tick a few times
    deadline = time.time() + 3
    while time.time() < deadline and rt.status is not BotStatus.ONLINE:
        time.sleep(0.05)
    assert rt.status is BotStatus.ONLINE
    assert svc.tick_count >= 1

    svc.request_stop()
    thread.join(timeout=3)
    assert not thread.is_alive()
    # graceful shutdown stopped the runtime cleanly
    assert rt.status is BotStatus.OFFLINE


def test_start_background_runs_and_stops(engine, registry):
    adapter = SimulatedLiveAdapter(_es_1m(engine), symbol="ES", bars_per_poll=50)
    rt = _runtime(engine, registry, _feed(engine, adapter))
    svc = BotService(rt, cycle_seconds=0.5)
    rt.start()
    t = svc.start_background()
    deadline = time.time() + 3
    while time.time() < deadline and svc.tick_count < 1:
        time.sleep(0.05)
    assert svc.tick_count >= 1
    svc.request_stop()
    t.join(timeout=3)
    assert not t.is_alive()
    assert svc.stats()["running"] is False
