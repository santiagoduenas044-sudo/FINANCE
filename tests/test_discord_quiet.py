"""BOT_DISCORD=corto (the default): the channel gets the heads-up before a
trade, each trade's entry and exit, the day's close, the daily verse/fact and
errors."""
from datetime import datetime, timezone

import pytest

from signal_engine.output.discord import DiscordOutputAdapter, worth_sending
from signal_engine.output.events import OutputEvent

NOW = datetime(2026, 10, 7, 14, 0, tzinfo=timezone.utc)
TRADE = {"signal_id": "abc", "instrument": "NQ", "direction": "LONG", "entry": 1.0, "stop": 0.0}


@pytest.fixture(autouse=True)
def _short(monkeypatch):
    monkeypatch.delenv("BOT_DISCORD", raising=False)


def _note(key):
    return OutputEvent.from_note("t", "x", 1, key)


@pytest.mark.parametrize("event", [
    OutputEvent.from_trade_idea({"event": "idea", "zone_id": "z1", "number": 7}),       # heads-up
    OutputEvent.from_trade_idea({"event": "cancelled", "zone_id": "z1", "number": 7}),
    OutputEvent.from_trade_opened(TRADE),
    OutputEvent.from_trade_closed({**TRADE, "exit_reason": "STOP", "r_multiple": -1.0}),
    OutputEvent.from_daily_record("hoy", "2026-10-07"),
    OutputEvent.from_runtime_status("BOT_ERROR", "sin datos", NOW),
    _note("setup:zonas_1m_noche:abc123:open"),            # GHOST set ups
    _note("setup:zonas_1m_noche:abc123:close"),
    _note("setup:apertura_30m:open:APERTURA-2026-10-07"),  # ORBIT set ups
    _note("setup:apertura_30m:close:APERTURA-2026-10-07"),
    _note("open:APERTURA-2026-10-07"), _note("close:APERTURA-2026-10-07"),  # Set up 2
    _note("verse:2026-10-07"), _note("fact:2026-10-07"),
])
def test_what_still_goes_out(event):
    assert worth_sending(event)


@pytest.mark.parametrize("event", [
    OutputEvent.from_runtime_status("BOT_STARTED", "Bot started", NOW),
    OutputEvent.from_runtime_status("BOT_STOPPED", "Bot stopped.", NOW),
    OutputEvent.from_trade_progress(TRADE, 0.5),
    OutputEvent.from_trade_tp1(TRADE),
    OutputEvent.from_day_done({"day": "2026-10-07"}),
    _note("box:2026-10-07"), _note("be:APERTURA-2026-10-07"), _note("online:1"),
    _note("setup:apertura_30m:be:APERTURA-2026-10-07"),
])
def test_what_stays_in_the_dashboard(event):
    assert not worth_sending(event)


def test_todo_sends_everything(monkeypatch):
    monkeypatch.setenv("BOT_DISCORD", "todo")
    assert worth_sending(OutputEvent.from_trade_progress(TRADE, 0.5))
    assert worth_sending(_note("box:2026-10-07"))


def test_the_adapter_does_not_send_what_is_kept_out(monkeypatch):
    from signal_engine.config.schema import DiscordOutputConfig
    calls = []
    monkeypatch.setenv("TEST_HOOK", "https://discord.invalid/x")
    a = DiscordOutputAdapter(DiscordOutputConfig(enabled=True, webhook_env="TEST_HOOK"),
                             transport=lambda url, data, timeout: calls.append(data) or 204,
                             sleep=lambda s: None)
    assert a.emit(OutputEvent.from_runtime_status("BOT_STARTED", "x", NOW)) is False
    assert a.emit(OutputEvent.from_trade_opened(TRADE)) is True
    assert len(calls) == 1
