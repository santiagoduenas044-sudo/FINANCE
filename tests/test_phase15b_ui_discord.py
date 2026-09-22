"""Phase 15b — richer PaperTrade payload for the dashboard tables + Discord
env-flag activation (BOT_DISCORD_ENABLED).

No new external APIs, no orders. Discord routing still enforced through GHOST
authority (Phase 15): only APPROVED decisions reach it.
"""
from pathlib import Path

import pandas as pd
import pytest

from signal_engine.config.schema import DiscordOutputConfig, EngineConfig
from signal_engine.output.dispatcher import OutputDispatcher
from signal_engine.paper.tracker import PaperTradeTracker

SAMPLES = Path(__file__).resolve().parent / "data_samples"


# --------------------------------------------------------------------------- #
# PaperTrade payload — everything the dashboard tables need in one dict
# --------------------------------------------------------------------------- #
def _bar(o, h, l, c):
    return pd.Series({"open": o, "high": h, "low": l, "close": c})


def test_paper_trade_dict_carries_tp1_tp2_pnl_r_mfe_mae_for_dashboard():
    """The Recent Signals + Paper Trades tables read directly from this dict —
    it must include TP1/TP2, live P&L points, R multiple, and MFE/MAE."""
    tr = PaperTradeTracker()
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    t = tr.open_trade(signal_id="s1", instrument="ES", direction="LONG",
                      setup="orb", entry=5000, stop=4995,
                      take_profits=[5010, 5020, 5030], opened_at=ts)
    tr.update_on_bar(t, _bar(5000, 5008, 4999, 5006), ts + pd.Timedelta(minutes=5))
    d = t.to_dict()
    for k in ("tp1", "tp2", "tp3", "tp1_r", "tp2_r",
              "pnl_points", "r_multiple", "mfe_r", "mae_r",
              "distance_to_tp1", "distance_to_tp2", "distance_to_stop"):
        assert k in d, f"dashboard column missing: {k}"
    # concrete values
    assert d["tp1"] == 5010 and d["tp2"] == 5020 and d["tp3"] == 5030
    assert d["tp1_r"] == pytest.approx(2.0)
    assert d["pnl_points"] == pytest.approx(6.0)     # 5006 - 5000
    assert d["r_multiple"] == pytest.approx(1.2)     # 6 / 5
    assert d["mfe_r"] == pytest.approx(8.0 / 5)      # high 5008 vs entry


def test_short_paper_trade_dict_signs_and_tps():
    tr = PaperTradeTracker()
    ts = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")
    t = tr.open_trade(signal_id="s2", instrument="NQ", direction="SHORT",
                      setup="orb", entry=20000, stop=20010,
                      take_profits=[19980, 19960], opened_at=ts)
    tr.update_on_bar(t, _bar(20000, 20002, 19992, 19993), ts + pd.Timedelta(minutes=5))
    d = t.to_dict()
    assert d["tp1"] == 19980 and d["tp2"] == 19960
    assert d["pnl_points"] == pytest.approx(7.0)     # 20000 - 19993 (short)
    assert d["r_multiple"] > 0


# --------------------------------------------------------------------------- #
# Discord env flag — flip via BOT_DISCORD_ENABLED without editing YAML
# --------------------------------------------------------------------------- #
def test_env_flag_turns_discord_on(monkeypatch):
    monkeypatch.setenv("BOT_DISCORD_ENABLED", "true")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")
    cfg = EngineConfig.load()
    cfg.output.discord.enabled = False   # YAML off; the env flag must override it
    d = OutputDispatcher.from_config(cfg)
    assert len(d.adapters) == 1 and d.adapters[0].name == "discord"
    # The adapter's own emit-time guard must NOT drop messages: the override
    # made it enabled at construction.
    assert d.adapters[0].config.enabled is True


def test_env_flag_turns_discord_off_even_if_yaml_enabled(monkeypatch):
    monkeypatch.setenv("BOT_DISCORD_ENABLED", "false")
    cfg = EngineConfig.load()
    cfg.output.discord.enabled = True   # pretend YAML said true
    d = OutputDispatcher.from_config(cfg)
    assert d.adapters == []


def test_no_env_flag_falls_back_to_yaml(monkeypatch):
    monkeypatch.delenv("BOT_DISCORD_ENABLED", raising=False)
    cfg = EngineConfig.load()
    cfg.output.discord.enabled = False   # with no env flag, YAML decides
    d = OutputDispatcher.from_config(cfg)
    assert d.adapters == []


def test_ghost_authority_still_gates_discord_when_env_flag_on(monkeypatch, engine, registry):
    """Even with the env flag on, GHOST authority still applies — a rejected
    signal produces zero Discord sends. Repeats the Phase 15 authority test
    with the new activation path."""
    import json
    from signal_engine.config.schema import GhostConfig
    from signal_engine.data.ingestion import normalize
    from signal_engine.data.providers.csv_provider import CsvProvider
    from signal_engine.ghost.brain import GhostBrain
    from signal_engine.live.feed import LiveFeed
    from signal_engine.live.simulated import SimulatedLiveAdapter
    from signal_engine.output.discord import DiscordOutputAdapter
    from signal_engine.paper.tracker import PaperTradeTracker
    from signal_engine.runtime.engine import BotRuntime
    from signal_engine.setups.engine import SetupEngine
    from signal_engine.signals.engine import SignalEngine

    monkeypatch.setenv("BOT_DISCORD_ENABLED", "true")

    sent = []
    def tport(url, data, timeout):
        p = json.loads(data)
        embed = p.get("embeds", [{}])[0]
        fields = {f["name"]: f["value"] for f in embed.get("fields", [])}
        if fields.get("Direction") in ("LONG", "SHORT"):
            sent.append(p)
        return 204
    # Build discord adapter from config via dispatcher (uses env flag path).
    cfg = EngineConfig.load()
    dispatcher = OutputDispatcher.from_config(cfg)
    assert len(dispatcher.adapters) == 1
    dispatcher.adapters[0]._transport = tport     # inject transport

    hist = normalize(CsvProvider(SAMPLES).get_raw_bars("ES", "1m", contract="ESM26"),
                     engine.cfg.data, "1m")
    feed = LiveFeed(SimulatedLiveAdapter(hist, symbol="ES", bars_per_poll=5),
                    "ES", engine.cfg)
    # Impossible confluence => every base signal rejected.
    ghost = GhostBrain(GhostConfig(min_confluence_score=99999.0))
    rt = BotRuntime(engine, SetupEngine(engine.cfg, registry),
                    SignalEngine(engine.cfg.signals), registry,
                    live_feeds={"ES": feed}, ghost=ghost,
                    paper_tracker=PaperTradeTracker(),
                    output_dispatcher=dispatcher)
    rt.start()
    for _ in range(150):
        rt.run_cycle("ES")
    assert sent == []   # GHOST still gates everything, env flag or not
