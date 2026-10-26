"""One Discord channel per set up: created by the bot, every message of a set
up goes to its own channel, the rest stays in the general one."""
import json

import pytest

from signal_engine.output import discord_channels as dch
from signal_engine.output.events import OutputEvent


class FakeDiscord:
    """The few REST calls create_channels makes, in memory."""

    def __init__(self):
        self.channels, self.hooks, self.calls, self.next = [], {}, [], 100

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if path.startswith("/channels/") and path.endswith("/webhooks"):
            ch = path.split("/")[2]
            if method == "GET":
                return self.hooks.get(ch, [])
            self.next += 1
            hook = {"id": str(self.next), "token": f"tok{self.next}", "name": body["name"]}
            self.hooks.setdefault(ch, []).append(hook)
            return hook
        if method == "PATCH" and path.startswith("/channels/"):
            ch = next(c for c in self.channels if c["id"] == path.split("/")[2])
            ch.update(body)
            return ch
        if method == "GET" and path.startswith("/channels/"):
            return {"guild_id": "g1"}
        if path == "/guilds/g1/channels":
            if method == "GET":
                return list(self.channels)
            self.next += 1
            ch = {"id": str(self.next), **body}
            self.channels.append(ch)
            return ch
        raise AssertionError(path)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_CHANNEL_ID", "555")
    dch._cache.update({"mtime": None, "data": {}})
    return tmp_path / "discord_channels.json"


def test_the_bot_makes_one_channel_per_set_up_that_is_on(store):
    api = FakeDiscord()
    r = dch.create_channels(api=api, path=store)
    on = [s for s in dch.setups() if s["on"]]
    assert r["total"] == len(on) and len(r["made"]) == len(on)
    names = [c["name"] for c in api.channels if c["type"] == 0]
    assert "set-up-1-fvg-15" in names and "set-up-5-fvg-15-calma" in names and "set-up-14-fvg-1h" in names
    assert not any("apertura" in n or "fvg-1-" in n for n in names)          # retired 26-oct
    assert all(c["parent_id"] == api.channels[0]["id"] for c in api.channels if c["type"] == 0)
    saved = json.loads(store.read_text())
    assert saved["setups"]["5"]["webhook"].startswith("https://discord.com/api/webhooks/")
    # again: nothing new, nothing duplicated
    before = len(api.channels)
    assert dch.create_channels(api=api, path=store)["made"] == []
    assert len(api.channels) == before


def test_messages_go_to_their_set_up(store):
    def note(key):
        return OutputEvent.from_note("t", "x", 1, key)
    assert dch.setup_of(note("setup:zonas_1m_noche:abc:open"), 1) == 4
    assert dch.setup_of(note("setup:apertura_30m:close:APERTURA-2026-10-07"), 2) == 6
    assert dch.setup_of(OutputEvent.from_trade_opened({"signal_id": "s"}), 1) == 1
    assert dch.setup_of(OutputEvent.from_trade_idea({"event": "idea", "zone_id": "z"}), 1) == 1
    assert dch.setup_of(note("open:APERTURA-2026-10-07"), 2) == 2        # ORBIT itself
    assert dch.setup_of(note("verse:2026-10-07"), 1) is None             # general channel
    assert dch.setup_of(OutputEvent.from_daily_record("x", "2026-10-07"), 1) is None


def test_adapter_writes_in_the_set_up_channel_or_the_general_one(monkeypatch):
    from signal_engine.config.schema import DiscordOutputConfig
    from signal_engine.output.discord import DiscordOutputAdapter
    sent = []
    a = DiscordOutputAdapter(DiscordOutputConfig(enabled=True), webhook_url="https://general",
                             transport=lambda url, data, t: sent.append(url) or 204,
                             sleep=lambda s: None,
                             route=lambda e: "https://set-up-4" if "setup:" in e.dedup_key else None)
    a.emit(OutputEvent.from_note("t", "x", 1, "setup:zonas_1m_noche:abc:open"))
    a.emit(OutputEvent.from_daily_record("hoy", "2026-10-07"))
    assert sent == ["https://set-up-4", "https://general"]


def test_missing_permissions_say_how_to_fix_it(store):
    def no(method, path, body=None):
        raise dch.ChannelError("Al bot le falta permiso. En Discord: Ajustes del servidor → Roles")
    with pytest.raises(dch.ChannelError, match="Gestionar|permiso"):
        dch.create_channels(api=no, path=store)
    assert "permiso" in dch.status(store)["error"]


def test_history_goes_to_each_set_up_channel(store):
    store.write_text(json.dumps({"setups": {"4": {"webhook": "https://hook4", "name": "set-up-4"}}}))
    T = [{"src": "ghost:zonas_1m_noche", "day": f"2026-10-0{i % 9 + 1}", "net_usd": 805.0 if i % 2 else -330.0,
          "opened_at": "2026-10-06T21:30:00-04:00", "side": "LONG", "entry": 1.0, "exit": 2.0, "why": "TP2"}
         for i in range(20)]
    log = {"sources": [{"id": "ghost:zonas_1m_noche", "setup": 4, "name": "Set up 4 · FVG 1",
                        "kind": "variant"},
                       {"id": "ghost:zonas_1m", "setup": 3, "name": "Set up 3 · FVG 1 DÍA", "kind": "variant"}],
           "trades": T}
    posts = []
    sent = dch.send_history(log, post=lambda url, p: posts.append((url, p)) or 204, path=store, pause=0)
    assert sent == {"4": {"trades": 20, "messages": 3}}               # no channel for 3: skipped
    assert all(url == "https://hook4" for url, _ in posts)
    head = posts[0][1]["embeds"][0]
    assert head["title"] == "📒 Historial · Set up 4 · FVG 1"
    assert "20 trades · ganó 10 · +$4,750" in head["description"]
    assert "✅ 06-10 21:30 · COMPRA 1.00 → 2.00 · objetivo · **+$805**" in posts[1][1]["embeds"][0]["description"]


def test_channels_with_the_old_name_are_renamed(store):
    api = FakeDiscord()
    dch.create_channels(api=api, path=store)
    old = next(c for c in api.channels if c.get("name") == "set-up-5-fvg-15-calma")
    old["name"] = "set-up-5-iman-15-calma"                                # as it was made on 9-oct
    dch.create_channels(api=api, path=store)
    assert old["name"] == "set-up-5-fvg-15-calma"
    assert ("PATCH", f"/channels/{old['id']}") in api.calls
