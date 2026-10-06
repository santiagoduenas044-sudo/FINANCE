"""Tests for the Bot Runtime (Phase 9): start/pause/stop REALLY control the
trading pipeline, not just a UI label. No internet (Discord uses a mocked
transport, never real network).
"""
from datetime import date, datetime, timezone

import pytest

from signal_engine.config.schema import DiscordOutputConfig
from signal_engine.data.contracts import ContractRegistry
from signal_engine.output.discord import DiscordOutputAdapter
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.runtime.engine import BotRuntime
from signal_engine.runtime.models import BotStatus, ComponentStatus
from signal_engine.setups.engine import SetupEngine
from signal_engine.signals.engine import SignalEngine
from tests.conftest import FakeProvider, make_rth_1m, weekdays

FAKE_WEBHOOK = "https://discord.example.invalid/webhook/does-not-exist"


class RecordingTransport:
    def __init__(self, status=204):
        self.calls = []
        self.status = status

    def __call__(self, url, data, timeout):
        self.calls.append(data)
        return self.status


class FixedClock:
    def __init__(self, start=None):
        self._now = start or datetime(2026, 6, 15, 14, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self._now

    def advance(self, **kwargs):
        from datetime import timedelta
        self._now += timedelta(**kwargs)


def _pipeline(data_engine):
    raw = make_rth_1m(weekdays(date(2026, 6, 8), 2), seed=7)
    provider = FakeProvider({("ES", "1m", "ESM26"): raw})
    lc = data_engine.load_contract(provider, "ES", "ESM26", "1m")
    data_engine.build_timeframes(lc)
    return lc


def _runtime(engine, dispatcher=None, now_fn=None):
    registry = ContractRegistry.load()
    se = SetupEngine(engine.cfg, registry)
    sig_eng = SignalEngine(engine.cfg.signals)
    kwargs = {}
    if dispatcher is not None:
        kwargs["output_dispatcher"] = dispatcher
    if now_fn is not None:
        kwargs["now_fn"] = now_fn
    return BotRuntime(engine, se, sig_eng, registry, **kwargs)


# --------------------------------------------------------------------------- #
# start / pause / stop / status
# --------------------------------------------------------------------------- #
def test_initial_status_is_offline(engine):
    rt = _runtime(engine)
    assert rt.status is BotStatus.OFFLINE


def test_start_transitions_to_online_and_logs_event(engine):
    clock = FixedClock()
    rt = _runtime(engine, now_fn=clock)
    event = rt.start()
    assert rt.status is BotStatus.ONLINE
    assert rt.started_at == clock()
    assert event.kind == "BOT_STARTED"
    assert any(e.kind == "BOT_STARTED" for e in rt.events)


def test_pause_transitions_from_online_to_paused(engine):
    rt = _runtime(engine)
    rt.start()
    event = rt.pause()
    assert rt.status is BotStatus.PAUSED
    assert event.kind == "BOT_PAUSED"


def test_stop_transitions_to_offline_from_any_state(engine):
    rt = _runtime(engine)
    rt.start()
    rt.pause()
    event = rt.stop()
    assert rt.status is BotStatus.OFFLINE
    assert event.kind == "BOT_STOPPED"


def test_mark_error_sets_error_status_and_reason_visibly(engine):
    rt = _runtime(engine)
    rt.start()
    event = rt.mark_error("simulated critical failure")
    assert rt.status is BotStatus.ERROR
    assert rt.last_error == "simulated critical failure"       # never hidden
    assert event.detail == "simulated critical failure"


def test_start_from_error_recovers(engine):
    rt = _runtime(engine)
    rt.start()
    rt.mark_error("boom")
    assert rt.status is BotStatus.ERROR
    rt.start()
    assert rt.status is BotStatus.ONLINE
    assert rt.last_error is None


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_updates_timestamp_deterministically(engine):
    clock = FixedClock()
    rt = _runtime(engine, now_fn=clock)
    assert rt.last_heartbeat_at is None
    rt.heartbeat()
    assert rt.last_heartbeat_at == clock()
    clock.advance(seconds=30)
    rt.heartbeat()
    assert rt.last_heartbeat_at == clock()


def test_run_cycle_always_updates_heartbeat_even_when_refused(engine):
    clock = FixedClock()
    rt = _runtime(engine, now_fn=clock)
    lc = _pipeline(engine)
    rt.run_cycle("ES", lc.timeframes)   # OFFLINE -> refused, but heartbeat still ticks
    assert rt.last_heartbeat_at == clock()


# --------------------------------------------------------------------------- #
# no signals while paused / offline — the REAL control, not a UI toggle
# --------------------------------------------------------------------------- #
def test_run_cycle_refuses_when_offline(engine):
    rt = _runtime(engine)
    lc = _pipeline(engine)
    result = rt.run_cycle("ES", lc.timeframes)
    assert result.ran is False
    assert "OFFLINE" in result.reason
    assert rt.last_run_at is None
    assert len(rt.recent_signals) == 0


def test_run_cycle_refuses_when_paused(engine):
    rt = _runtime(engine)
    rt.start()
    rt.pause()
    lc = _pipeline(engine)
    result = rt.run_cycle("ES", lc.timeframes)
    assert result.ran is False
    assert "PAUSED" in result.reason
    assert rt.last_run_at is None
    assert len(rt.recent_signals) == 0


def test_run_cycle_produces_results_when_online(engine):
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)
    result = rt.run_cycle("ES", lc.timeframes)
    assert result.ran is True
    assert rt.last_run_at is not None
    assert (result.signals_produced + result.no_trades_produced) == len(rt.recent_signals)


def test_pausing_mid_run_stops_further_cycles_from_acting(engine):
    """Explicit end-to-end proof: pause() really changes what run_cycle() does
    on its NEXT invocation, not just a label — this is the requirement's core
    safety property."""
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)
    r1 = rt.run_cycle("ES", lc.timeframes)
    assert r1.ran is True
    count_after_first = len(rt.recent_signals)

    rt.pause()
    r2 = rt.run_cycle("ES", lc.timeframes)
    assert r2.ran is False
    assert len(rt.recent_signals) == count_after_first   # nothing new added


# --------------------------------------------------------------------------- #
# error state: visible, with reason, not hidden
# --------------------------------------------------------------------------- #
def test_run_cycle_error_sets_error_status_with_visible_reason(engine, monkeypatch):
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)

    def boom(ctx):
        raise RuntimeError("setup engine exploded")
    monkeypatch.setattr(rt.setup_engine, "detect", boom)

    result = rt.run_cycle("ES", lc.timeframes)
    assert result.ran is False
    assert "setup engine exploded" in result.reason
    assert rt.status is BotStatus.ERROR
    assert "setup engine exploded" in rt.last_error
    assert rt.component_health["analysis"].status is ComponentStatus.DOWN


def test_error_status_appears_in_status_dict(engine, monkeypatch):
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)
    monkeypatch.setattr(rt.setup_engine, "detect",
                        lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    rt.run_cycle("ES", lc.timeframes)
    status = rt.to_status_dict()
    assert status["status"] == "ERROR"
    assert "boom" in status["last_error"]


# --------------------------------------------------------------------------- #
# Discord startup notification
# --------------------------------------------------------------------------- #
def test_start_sends_discord_notification():
    from signal_engine.data.engine import DataEngine
    data_engine = DataEngine()
    registry = ContractRegistry.load()
    se = SetupEngine(data_engine.cfg, registry)
    sig_eng = SignalEngine(data_engine.cfg.signals)
    transport = RecordingTransport()
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True),
                                   webhook_url=FAKE_WEBHOOK, transport=transport,
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt = BotRuntime(data_engine, se, sig_eng, registry, output_dispatcher=dispatcher)

    rt.start()

    assert len(transport.calls) == 1
    import json
    payload = json.loads(transport.calls[0])
    assert payload["embeds"][0]["title"] == "🟢 GHOST encendido"
    assert "footer" not in payload["embeds"][0] and "fields" not in payload["embeds"][0]


def test_pause_and_stop_also_notify_discord():
    from signal_engine.data.engine import DataEngine
    data_engine = DataEngine()
    registry = ContractRegistry.load()
    se = SetupEngine(data_engine.cfg, registry)
    sig_eng = SignalEngine(data_engine.cfg.signals)
    transport = RecordingTransport()
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True, deduplication_enabled=False),
                                   webhook_url=FAKE_WEBHOOK, transport=transport,
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt = BotRuntime(data_engine, se, sig_eng, registry, output_dispatcher=dispatcher)

    rt.start()
    rt.pause()
    rt.stop()
    assert len(transport.calls) == 3


def test_discord_failure_does_not_break_lifecycle_control():
    """A Discord outage must never prevent start/pause/stop from working."""
    from signal_engine.data.engine import DataEngine
    data_engine = DataEngine()
    registry = ContractRegistry.load()
    se = SetupEngine(data_engine.cfg, registry)
    sig_eng = SignalEngine(data_engine.cfg.signals)

    def broken_transport(url, data, timeout):
        raise ConnectionError("network is down")
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True, retry_count=0),
                                   webhook_url=FAKE_WEBHOOK, transport=broken_transport,
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt = BotRuntime(data_engine, se, sig_eng, registry, output_dispatcher=dispatcher)

    rt.start()   # must not raise
    assert rt.status is BotStatus.ONLINE
    assert rt.component_health["discord"].status is ComponentStatus.OK  # adapter itself swallowed it


def test_webhook_never_appears_in_status_dict_or_events():
    from signal_engine.data.engine import DataEngine
    data_engine = DataEngine()
    registry = ContractRegistry.load()
    se = SetupEngine(data_engine.cfg, registry)
    sig_eng = SignalEngine(data_engine.cfg.signals)
    discord = DiscordOutputAdapter(DiscordOutputConfig(enabled=True),
                                   webhook_url=FAKE_WEBHOOK, transport=RecordingTransport(),
                                   sleep=lambda s: None)
    dispatcher = OutputDispatcher([discord])
    rt = BotRuntime(data_engine, se, sig_eng, registry, output_dispatcher=dispatcher)
    rt.start()
    assert FAKE_WEBHOOK not in str(rt.to_status_dict())
    assert FAKE_WEBHOOK not in str(rt.events)
    assert FAKE_WEBHOOK not in repr(discord)


# --------------------------------------------------------------------------- #
# component health / instrument status
# --------------------------------------------------------------------------- #
def test_risk_component_disabled_without_risk_engine(engine):
    rt = _runtime(engine)
    assert rt.component_health["risk"].status is ComponentStatus.DISABLED


def test_discord_component_disabled_without_dispatcher(engine):
    rt = _runtime(engine)
    assert rt.component_health["discord"].status is ComponentStatus.DISABLED


def test_instrument_status_populated_after_cycle(engine):
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)
    rt.run_cycle("ES", lc.timeframes)
    assert "ES" in rt.instrument_status
    assert rt.instrument_status["ES"].last_price is not None


# --------------------------------------------------------------------------- #
# deterministic behavior
# --------------------------------------------------------------------------- #
def test_deterministic_cycle_results_across_fresh_runtimes(engine):
    lc = _pipeline(engine)

    def run():
        rt = _runtime(engine, now_fn=FixedClock())
        rt.start()
        return rt.run_cycle("ES", lc.timeframes)

    r1, r2 = run(), run()
    assert r1.signals_produced == r2.signals_produced
    assert r1.no_trades_produced == r2.no_trades_produced


def test_status_dict_is_json_serializable(engine):
    import json
    rt = _runtime(engine)
    rt.start()
    lc = _pipeline(engine)
    rt.run_cycle("ES", lc.timeframes)
    json.dumps(rt.to_status_dict())            # must not raise
    json.dumps(rt.recent_signals_dict())       # must not raise
