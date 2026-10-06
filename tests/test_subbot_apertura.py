"""APERTURA sub-bot: the opening-range breakout run bar by bar, exactly as
the strategy lab measured it (box 09:30-09:45, entry on the next open after
a 5-minute close outside, stop across the box, 1.5x target, BE+2 at 70%,
flat 16:55), plus restart safety and its Discord messages."""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from signal_engine.subbots.opening_range import OpeningRangeBot, OrbSettings

ROOT = Path(__file__).resolve().parents[1]
TZ = "America/New_York"


def _lab():
    spec = importlib.util.spec_from_file_location("strategy_lab", ROOT / "scripts" / "strategy_lab.py")
    lab = importlib.util.module_from_spec(spec)
    sys.modules["strategy_lab"] = lab
    spec.loader.exec_module(lab)
    return lab


def _day(rows_by_minute: dict, day="2025-03-03", default=(100, 101, 99, 100)):
    """Close-labeled ET 1-minute bars 09:01-17:00; rows_by_minute maps a
    CLOSE label 'HH:MM' to (open, high, low, close)."""
    idx = pd.date_range(f"{day} 09:01", f"{day} 17:00", freq="1min", tz=TZ)
    rows = [rows_by_minute.get(t.strftime("%H:%M"), default) for t in idx]
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def _feed(bot, bars, window=400):
    for i in range(1, len(bars) + 1):
        bot.on_bars(bars.iloc[max(0, i - window):i])


def _bot(tmp_path, **kw):
    sent = []
    bot = OpeningRangeBot(tmp_path, settings=OrbSettings(**kw),
                          publish=lambda **m: sent.append(m))
    return bot, sent


def _box(hi=110, lo=90):
    """09:31-09:45 inside [lo, hi]."""
    return {f"09:{m:02d}": (100, hi, lo, 100) for m in range(31, 46)}


def test_long_breakout_hits_the_target(tmp_path):
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)                 # 5-min close above 110
    rows["09:51"] = (111, 112, 110, 111)                 # entry at this open: 111
    rows["10:05"] = (111, 142, 111, 140)                 # stop 90 -> risk 21, target 142.5
    rows["10:06"] = (140, 143, 139, 142)
    bot, sent = _bot(tmp_path)
    _feed(bot, _day(rows))
    t = bot.closed[-1]
    assert t["long"] and t["entry"] == 111 and t["stop_initial"] == 90
    assert t["why"] == "objetivo" and t["exit"] == 142.5
    assert t["pts"] == 31.5 and t["usd"] == pytest.approx((31.5 - 1.02) * 2)
    titles = [m["title"] for m in sent]
    assert titles[0].startswith("📦") and any("COMPRA" in x for x in titles)
    assert any("stop a BE+2" in x for x in titles) and any("— objetivo · +31.50 pts" in x for x in titles)


def test_breakeven_plus_two_after_seventy_percent(tmp_path):
    rows = _box()
    rows["09:55"] = (100, 100, 88, 89)                   # 5-min close below 90: short
    rows["09:56"] = (89, 90, 88, 89)                     # entry 89, stop 110 (21), tgt 57.5
    rows["10:10"] = (89, 89, 64, 66)                     # 70% = 67.0 reached
    rows["10:11"] = (66, 95, 65, 94)                     # back up through entry-2
    bot, _ = _bot(tmp_path)
    _feed(bot, _day(rows))
    t = bot.closed[-1]
    assert not t["long"] and t["why"] == "BE+2" and t["exit"] == 87 and t["pts"] == 2


def test_stop_counts_first_and_gaps_fill_at_the_open(tmp_path):
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    rows["09:51"] = (111, 112, 110, 111)
    rows["10:00"] = (85, 86, 80, 82)                     # gaps under the 90 stop
    bot, _ = _bot(tmp_path)
    _feed(bot, _day(rows))
    t = bot.closed[-1]
    assert t["why"] == "stop" and t["exit"] == 85 and t["pts"] == -26


def test_flat_at_1655(tmp_path):
    rows = {f"{h:02d}:{m:02d}": (111, 112, 110, 111)       # drifts sideways all day
            for h in range(9, 18) for m in range(60)}
    rows.update(_box())
    rows["09:50"] = (100, 112, 100, 111)
    bot, _ = _bot(tmp_path)
    _feed(bot, _day(rows))
    t = bot.closed[-1]
    assert t["why"] == "cierre 16:55" and pd.Timestamp(t["closed_at"]).strftime("%H:%M") == "16:55"


def test_no_breakout_and_too_wide_box(tmp_path):
    bot, sent = _bot(tmp_path)
    _feed(bot, _day(_box()))
    assert bot.closed == [] and any("no hubo ruptura" in m["title"] for m in sent)
    bot2, sent2 = _bot(tmp_path / "b")
    _feed(bot2, _day(_box(hi=260, lo=90)))
    assert bot2.closed == [] and "no opera" in sent2[0]["title"]


def test_restart_after_the_breakout_does_not_chase_it(tmp_path):
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    bars = _day(rows)
    bot, sent = _bot(tmp_path)
    bot.on_bars(bars[bars.index.strftime("%H:%M") <= "10:30"])   # first call at 10:30
    assert bot.st.trade is None and bot.st.done and sent == []
    _feed(bot, bars)
    assert bot.closed == []


def test_state_survives_a_restart(tmp_path):
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    bars = _day(rows)
    bot, _ = _bot(tmp_path)
    _feed(bot, bars[bars.index.strftime("%H:%M") <= "10:00"])
    assert bot.st.trade is not None
    again, _ = _bot(tmp_path)                            # new process, same folder
    assert again.st.trade is not None and again.st.trade["entry"] == 100   # 09:51 open
    _feed(again, bars)
    assert len(again.closed) == 1


def test_matches_the_strategy_lab_day_by_day(tmp_path):
    """Same random market, lab vs sub-bot: identical trades."""
    lab = _lab()
    rng = np.random.default_rng(7)
    frames = []
    for day in pd.bdate_range("2025-03-03", periods=12):
        idx = pd.date_range(f"{day.date()} 09:00", f"{day.date()} 16:59", freq="1min")
        c = 20000 + np.cumsum(rng.normal(0, 6, len(idx)))
        o = np.r_[c[0], c[:-1]]
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + rng.uniform(0, 3, len(idx)),
                                    "low": np.minimum(o, c) - rng.uniform(0, 3, len(idx)),
                                    "close": c, "volume": 100.0}, index=idx))
    m1 = pd.concat(frames)                                     # open-labeled, naive (lab)
    mk = lab.Market(m1)
    lab_trades = lab.run(mk, lab.orb(mk, 15, 1.5, "opuesto"), 1.02, "rth", be_at=0.7)

    live = m1.copy()                                            # close-labeled, ET (bot)
    live.index = (live.index + pd.Timedelta(minutes=1)).tz_localize(TZ)
    bot, _ = _bot(tmp_path)
    for day, g in live.groupby(live.index.date):
        _feed(bot, g[["open", "high", "low", "close"]])
    assert len(bot.closed) == len(lab_trades) > 3
    for mine, theirs in zip(bot.closed, lab_trades):
        assert mine["day"] == theirs[0]
        assert mine["net_pts"] == pytest.approx(theirs[1], abs=0.011)


def test_note_reaches_discord_as_an_embed():
    from signal_engine.output.discord import build_discord_payload
    from signal_engine.output.events import OutputEvent
    p = build_discord_payload(OutputEvent.from_note("📦 APERTURA — caja lista", "texto",
                                                    0x3498DB, "box:2025-03-03"))
    assert p["embeds"][0]["title"] == "📦 APERTURA — caja lista"
    assert p["embeds"][0]["description"] == "texto"


def test_orbit_command_reads_its_saved_record(tmp_path, monkeypatch):
    from signal_engine.output.discord_bot import build_reply
    from signal_engine.subbots import orbit
    monkeypatch.setattr(orbit, "STORE_DIR", tmp_path)
    bot = OpeningRangeBot(tmp_path, name="ORBIT")
    bot._append_trade({"day": "2025-03-03", "long": True, "entry": 100.0, "exit": 115.0,
                       "why": "objetivo", "usd": 27.96})
    reply = build_reply("!orbit", object())               # GHOST's runtime is not involved
    assert "ORBIT" in reply and "+$28" in reply


def test_orbit_writes_only_to_its_own_channel(monkeypatch):
    from signal_engine.subbots import orbit
    monkeypatch.delenv("DISCORD_WEBHOOK_ORBIT", raising=False)
    monkeypatch.setenv("BOT_PUSH", "off")
    assert not hasattr(orbit.make_publisher(), "__self__")      # nothing set: only its log
    monkeypatch.delenv("BOT_PUSH")
    assert [a.name for a in orbit.make_publisher().__self__.adapters] == ["push"]   # the app only
    monkeypatch.setenv("DISCORD_WEBHOOK_ORBIT", "https://discord.invalid/x")
    adapters = orbit.make_publisher().__self__.adapters
    assert [a.name for a in adapters] == ["discord", "push"]
    assert adapters[0].config.webhook_env == "DISCORD_WEBHOOK_ORBIT" and adapters[0].configured


def test_orbit_polls_and_survives_a_bad_poll(tmp_path):
    from signal_engine.subbots import orbit
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    rows["09:51"] = (111, 112, 110, 111)
    rows["10:05"] = (111, 142, 111, 140)
    bars = _day(rows)
    sent = []
    bot = OpeningRangeBot(tmp_path, publish=lambda **m: sent.append(m), name="ORBIT")

    def boom():
        raise ConnectionError("yahoo down")
    assert orbit.run_once(bot, fetch=boom) is False
    for i in range(1, len(bars) + 1):                       # one poll per minute
        assert orbit.run_once(bot, fetch=lambda: bars.iloc[:i])
    assert bot.closed and bot.closed[-1]["why"] == "BE+2"          # reached 70%, came back
    assert all(m["title"].split(" ")[1] == "ORBIT" for m in sent)


def test_orbit_test_run_replays_without_touching_the_real_record(tmp_path, monkeypatch, capsys):
    from signal_engine.subbots import orbit
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    bars = pd.concat([_day(rows, day="2025-03-03"), _day(rows, day="2025-03-04")])
    monkeypatch.setattr(orbit, "yahoo_bars", lambda: bars)
    monkeypatch.setattr(orbit, "STORE_DIR", tmp_path / "real")
    assert orbit.test_run(to_discord=False) == 0
    out = capsys.readouterr().out
    assert "caja lista" in out and "COMPRA NQ" in out and "Total: 2 trades" in out
    assert not (tmp_path / "real").exists()


def test_box_still_closes_when_the_feed_skips_0945(tmp_path):
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    rows["09:51"] = (111, 112, 110, 111)
    bars = _day(rows)
    bars = bars[bars.index.strftime("%H:%M") != "09:45"]          # Yahoo gap
    bot, sent = _bot(tmp_path)
    _feed(bot, bars)
    assert sent[0]["title"].startswith("📦") and "caja lista" in sent[0]["title"]
    assert bot.closed and bot.closed[-1]["entry"] == 111


def test_no_box_message_on_a_sunday_evening(tmp_path):
    idx = pd.date_range("2025-03-02 18:01", "2025-03-02 20:00", freq="1min", tz=TZ)
    bars = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}, index=idx)
    bot, sent = _bot(tmp_path)
    _feed(bot, bars)
    assert sent == []


def test_orbit_test_run_over_databento_history(tmp_path, monkeypatch, capsys):
    """--probar --desde: the lab's Databento file (open-labeled, naive ET)
    replayed through ORBIT's own code, with a monthly summary."""
    from signal_engine.subbots import orbit
    rng = np.random.default_rng(5)
    frames = []
    for day in pd.bdate_range("2025-01-06", periods=30):
        idx = pd.date_range(f"{day.date()} 08:00", f"{day.date()} 17:30", freq="1min")
        c = 20000 + np.cumsum(rng.normal(0, 6, len(idx)))
        o = np.r_[c[0], c[:-1]]
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + 2,
                                    "low": np.minimum(o, c) - 2, "close": c, "volume": 1.0},
                                   index=idx.rename("datetime")))
    d = tmp_path / "market_data" / "NQ"
    d.mkdir(parents=True)
    pd.concat(frames).to_csv(d / "DATABENTO_1m.csv")
    monkeypatch.setattr(orbit, "ROOT", tmp_path)
    assert orbit.test_run(False, desde="2025-01-06") == 0
    out = capsys.readouterr().out
    assert "(30 días)" in out and "Por mes" in out and "2025-01:" in out and "2025-02:" in out
    assert "── 📦" not in out                            # long runs: summary only


def test_early_close_holiday_exits_at_the_last_price(tmp_path):
    """Labor Day: the market stops at 13:00. The trade closes at the 13:00
    price (as the lab does), not at the next session's open."""
    rows = _box()
    rows["09:50"] = (100, 112, 100, 111)
    bars = _day(rows)
    bars.loc[bars.index.strftime("%H:%M") == "13:00", "close"] = 104.0
    bars = bars[bars.index.strftime("%H:%M") <= "13:00"]
    evening = pd.date_range("2025-03-03 18:01", periods=30, freq="1min", tz=TZ)
    bars = pd.concat([bars, pd.DataFrame({"open": 120.0, "high": 121.0, "low": 119.0,
                                          "close": 120.0}, index=evening)])
    bot, _ = _bot(tmp_path)
    _feed(bot, bars)
    t = bot.closed[-1]
    assert t["exit"] == 104.0 and t["why"] == "cierre (el mercado cerró antes)"
    assert pd.Timestamp(t["closed_at"]).strftime("%H:%M") == "13:00"




def test_orbit_trades_five_micros_by_default(monkeypatch):
    from signal_engine.subbots import orbit
    from signal_engine.subbots.opening_range import OrbSettings

    monkeypatch.delenv("ORBIT_POSITION", raising=False)
    s = orbit.settings()
    # 5 MNQ: $10/pt; costs per contract stay 1.02 pts ($1.04 commission + 1 tick per side)
    assert (s.position, s.usd_per_point * s.contracts, s.cost_points) == ("5 MNQ", 10.0, 1.02)
    monkeypatch.setenv("ORBIT_POSITION", "1 NQ")
    assert (orbit.settings().usd_per_point, orbit.settings().cost_points) == (20.0, 0.655)
    monkeypatch.setenv("ORBIT_POSITION", "nonsense")
    assert orbit.settings().position == "5 MNQ"
    assert OrbSettings().cost_points == 1.02


def test_a_trade_keeps_the_size_it_opened_with(tmp_path):
    from signal_engine.subbots.opening_range import OpeningRangeBot, OrbSettings
    from signal_engine.versions import orbit_version_of

    bot = OpeningRangeBot(tmp_path, settings=OrbSettings.for_position("1 NQ"))
    bot.st.day = "2026-09-30"
    bot._open({"long": True, "stop": 30683.0, "box_hi": 30810.0, "box_lo": 30683.0,
               "signal_ts": "2026-09-30 10:05:00-04:00", "signal_close": 30841.0},
              30841.25, "2026-09-30 10:06:00-04:00", None, quiet=True)
    # ORBIT restarts with 5 MNQ while the trade is open: it still closes as 1 NQ
    bot2 = OpeningRangeBot(tmp_path, settings=OrbSettings.for_position("5 MNQ"))
    bot2.st.trade = bot.st.trade
    bot2._close(bot2.st.trade, 30941.25, "2026-09-30 11:00:00-04:00", "prueba", None, quiet=True)
    done = bot2.closed[-1]
    assert (done["contracts"], done["symbol"]) == (1, "NQ")
    assert done["usd"] == round((100 - 0.655) * 20, 2)
    assert orbit_version_of(done) == "v2"
    assert orbit_version_of({"symbol": "MNQ", "contracts": 5}) == "v3"
    assert orbit_version_of({"usd": 10}) == "v1"
