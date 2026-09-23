"""Read-only Discord command bot (!status / !trades / !why / !help).

The load-bearing property is READ-ONLY: anyone who can post in the channel can
issue a command, so no command may start, stop, pause or trade. The spy runtime
below raises on every mutating method and every command runs against it.
"""
import logging

import pandas as pd
import pytest

from signal_engine.output import discord_bot
from signal_engine.output.discord_bot import build_reply, start_command_bot
from signal_engine.paper.tracker import PaperTradeTracker

T0 = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")


class _Runtime:
    """Just the read surface the bot uses, plus tripwires on every mutator."""
    def __init__(self, tracker=None, reads=None, payload=None):
        self.paper_tracker = tracker
        self.ghost_market_read = reads or {}
        self._payload = payload or {
            "status": "ONLINE", "uptime": "2h 5m", "feed": "CONNECTED",
            "trades_today": 1, "closed_today": 1, "realised_points": 40.0,
            "win_rate": "100%", "open_r": None, "open_trades": []}

    def status_digest_payload(self):
        return dict(self._payload)

    def _tripwire(self, *a, **k):
        raise AssertionError("a Discord command mutated the runtime")

    start = stop = pause = resume = run_cycle = heartbeat = _tripwire
    maybe_send_status_digest = _notify_trade_updates = _tripwire


def _closed_tracker():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="w", instrument="NQ", direction="LONG", setup="orb",
                      entry=20000.0, stop=19980.0, take_profits=[20040.0],
                      opened_at=T0)
    t.status = t.status.__class__.ACTIVE
    tr.update_symbol("NQ", pd.Series({"open": 20000.0, "high": 20041.0,
                                      "low": 19999.0, "close": 20040.0}),
                     T0 + pd.Timedelta(minutes=5))
    return tr


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["hola", "", "   ", "status", "!unknown", "!!status",
                                  "check !status"])
def test_non_commands_get_no_reply(text):
    assert build_reply(text, _Runtime()) is None


@pytest.mark.parametrize("text", ["!status", "!estado", "!STATUS", "  !status  ",
                                  "!status please"])
def test_status_and_its_aliases_answer(text):
    assert "GHOST" in build_reply(text, _Runtime())


@pytest.mark.parametrize("cmd", ["!why", "!mercado", "!porque"])
def test_why_aliases(cmd):
    assert "Lo que veo" in build_reply(cmd, _Runtime())


@pytest.mark.parametrize("cmd", ["!help", "!ayuda"])
def test_help_lists_every_command(cmd):
    out = build_reply(cmd, _Runtime())
    for name in ("!status", "!trades", "!why", "!help"):
        assert name in out
    assert "solo lectura" in out


# --------------------------------------------------------------------------- #
# READ-ONLY — the property that makes an open command channel safe
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cmd", ["!status", "!estado", "!trades", "!why",
                                 "!mercado", "!help", "!ayuda"])
def test_no_command_mutates_the_runtime(cmd):
    rt = _Runtime(tracker=_closed_tracker(),
                  reads={"NQ": ["1H alcista.", "Régimen: tendencia alcista."]})
    reply = build_reply(cmd, rt)          # tripwires raise AssertionError
    assert reply and "No pude leer" not in reply


def test_no_command_changes_trade_state():
    tr = _closed_tracker()
    before = [t.to_dict() for t in tr.closed_trades + tr.open_trades]
    rt = _Runtime(tracker=tr)
    for cmd in ("!status", "!trades", "!why", "!help"):
        build_reply(cmd, rt)
    assert [t.to_dict() for t in tr.closed_trades + tr.open_trades] == before


# --------------------------------------------------------------------------- #
# content
# --------------------------------------------------------------------------- #
def test_status_shows_state_day_result_and_open_positions():
    rt = _Runtime(payload={
        "status": "ONLINE", "uptime": "3h 12m", "feed": "CONNECTED",
        "trades_today": 2, "closed_today": 1, "realised_points": 14.5,
        "win_rate": "100%", "open_r": 0.8,
        "open_trades": ["NQ LONG @ 20,000.00 → 20,016.00 (+16.00 pts, +0.80R)"]})
    out = build_reply("!status", rt)
    assert "ONLINE" in out and "3h 12m" in out and "CONNECTED" in out
    assert "+14.50 pts" in out and "+0.80R en total" in out
    assert "NQ LONG @ 20,000.00" in out


def test_status_with_nothing_open_says_so():
    assert "_ninguna_" in build_reply("!status", _Runtime())


def test_trades_reports_result_in_r_and_points():
    out = build_reply("!trades", _Runtime(tracker=_closed_tracker()))
    assert "✅ NQ LONG orb" in out and "+2.00R" in out and "+40.00 pts" in out


def test_trades_newest_first_and_capped():
    tr = PaperTradeTracker()
    for i in range(8):
        t = tr.open_trade(signal_id=f"s{i}", instrument="NQ", direction="LONG",
                          setup=f"setup{i}", entry=20000.0, stop=19980.0,
                          take_profits=[20020.0],
                          opened_at=T0 + pd.Timedelta(minutes=10 * i))
        t.status = t.status.__class__.ACTIVE
        tr.update_symbol("NQ", pd.Series({"open": 20000.0, "high": 20021.0,
                                          "low": 19999.0, "close": 20020.0}),
                         T0 + pd.Timedelta(minutes=10 * i + 5))
    lines = build_reply("!trades", _Runtime(tracker=tr)).splitlines()[1:]
    assert len(lines) == 5
    assert "setup7" in lines[0] and "setup3" in lines[-1]


def test_trades_empty_state():
    assert "todavía no hay trades" in build_reply("!trades", _Runtime())
    assert "todavía no hay trades" in build_reply("!trades",
                                                  _Runtime(tracker=PaperTradeTracker()))


def test_why_shows_the_market_read():
    out = build_reply("!why", _Runtime(reads={"NQ": ["1H alcista.", "Régimen: rango."]}))
    assert "Lo que veo en NQ" in out and "Régimen: rango." in out


def test_why_includes_the_thesis_of_an_open_trade():
    tr = PaperTradeTracker()
    t = tr.open_trade(signal_id="o", instrument="NQ", direction="SHORT", setup="orb",
                      entry=20000.0, stop=20020.0, take_profits=[19980.0],
                      opened_at=T0, context={"thesis": {
                          "por_eso": "SHORT. Ruptura por abajo.",
                          "espero": "Bajar hasta TP1 19,980.00."}})
    t.status = t.status.__class__.ACTIVE
    out = build_reply("!why", _Runtime(tracker=tr, reads={"NQ": ["1H bajista."]}))
    assert "Trade abierto NQ SHORT" in out
    assert "Ruptura por abajo" in out and "Bajar hasta TP1" in out


def test_why_before_any_analysis_explains_why_it_is_empty():
    assert "ONLINE" in build_reply("!why", _Runtime())


def test_a_failing_read_answers_politely_instead_of_crashing_the_bot():
    class Broken(_Runtime):
        def status_digest_payload(self):
            raise RuntimeError("boom")
    assert "No pude leer" in build_reply("!status", Broken())


def test_reply_never_exceeds_discords_message_limit():
    rt = _Runtime(reads={"NQ": ["x" * 900, "y" * 900, "z" * 900]})
    assert len(build_reply("!why", rt)) <= 2000


# --------------------------------------------------------------------------- #
# startup — opt-in, optional dependency, secret handling
# --------------------------------------------------------------------------- #
def test_no_token_means_no_bot(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert start_command_bot(_Runtime()) is None


def test_blank_token_means_no_bot(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "   ")
    assert start_command_bot(_Runtime()) is None


def test_missing_discord_py_disables_the_bot_without_breaking_startup(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "discord":
            raise ImportError("no discord")
        return real_import(name, *a, **k)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert start_command_bot(_Runtime()) is None


def test_token_is_never_logged(monkeypatch, caplog):
    started = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            self.name, self.daemon = name, daemon
        def start(self):
            started.append(self)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "SUPER-SECRET-TOKEN-123")
    monkeypatch.setattr(discord_bot.threading, "Thread", FakeThread)
    with caplog.at_level(logging.DEBUG):
        t = start_command_bot(_Runtime())
    assert t is started[0] and t.daemon is True
    assert "SUPER-SECRET-TOKEN-123" not in caplog.text


def test_channel_filter_parses_a_numeric_id(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_CHANNEL_ID", " 123456789012345678 ")
    assert discord_bot._channel_filter() == 123456789012345678


def test_channel_filter_ignores_garbage_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("DISCORD_BOT_CHANNEL_ID", "general")
    with caplog.at_level(logging.WARNING):
        assert discord_bot._channel_filter() is None
    assert "DISCORD_BOT_CHANNEL_ID" in caplog.text


def test_channel_filter_unset(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_CHANNEL_ID", raising=False)
    assert discord_bot._channel_filter() is None


# --------------------------------------------------------------------------- #
# against a real runtime, not just the stub
# --------------------------------------------------------------------------- #
def test_status_against_a_real_runtime(engine, registry):
    from signal_engine.runtime.engine import BotRuntime
    from signal_engine.setups.engine import SetupEngine
    from signal_engine.signals.engine import SignalEngine
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    paper_tracker=_closed_tracker())
    rt.start()
    out = build_reply("!status", rt)
    assert "ONLINE" in out and "win rate 100%" in out
    assert "✅" in build_reply("!trades", rt)
