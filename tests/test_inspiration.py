"""The morning Bible verse and the midday curious fact on Discord."""
import pandas as pd

from signal_engine.output.discord_bot import build_reply
from signal_engine.output.inspiration import FACTS, VERSES, DailyNotes, fact_for, verse_for

NY = "America/New_York"


def test_a_day_always_gets_the_same_verse_and_none_repeats_in_a_cycle():
    days = pd.date_range("2026-10-01", periods=len(VERSES))
    assert verse_for("2026-10-05") == verse_for(pd.Timestamp("2026-10-05 15:00"))
    # a whole cycle starting at a cycle boundary shows every verse once
    start = pd.Timestamp.fromordinal((pd.Timestamp("2026-10-01").toordinal() // len(VERSES)) * len(VERSES))
    cycle = [verse_for(start + pd.Timedelta(days=i)) for i in range(len(VERSES))]
    assert len(set(cycle)) == len(VERSES)
    fstart = pd.Timestamp.fromordinal((pd.Timestamp("2026-10-01").toordinal() // len(FACTS)) * len(FACTS))
    facts = [fact_for(fstart + pd.Timedelta(days=i)) for i in range(len(FACTS))]
    assert len(set(facts)) == len(FACTS)
    assert all(ref and text for text, ref in VERSES) and len(days) == len(VERSES)


def test_each_note_goes_once_at_its_time_and_survives_a_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_VERSICULOS", raising=False)
    monkeypatch.delenv("BOT_DATOS", raising=False)
    f = tmp_path / "inspiration.json"
    notes = DailyNotes(f)
    t = lambda hm: pd.Timestamp(f"2026-10-05 {hm}", tz=NY)  # noqa: E731
    assert notes.due(t("08:00")) == []                       # too early
    first = notes.due(t("08:46"))
    assert [k for k, *_ in first] == ["verse"] and "RVR1960" in first[0][2]
    assert notes.due(t("09:30")) == []                       # already sent today
    assert DailyNotes(f).due(t("10:00")) == []               # nor after a restart
    assert [k for k, *_ in DailyNotes(f).due(t("12:31"))] == ["fact"]
    assert [k for k, *_ in DailyNotes(f).due(pd.Timestamp("2026-10-06 08:50", tz=NY))] == ["verse"]
    assert DailyNotes(tmp_path / "x.json").due(t("20:00")) == []   # bot started at night: skip


def test_switches_turn_them_off(tmp_path, monkeypatch):
    monkeypatch.setenv("BOT_VERSICULOS", "off")
    monkeypatch.setenv("BOT_DATO_HORA", "07:00")
    notes = DailyNotes(tmp_path / "s.json")
    assert [k for k, *_ in notes.due(pd.Timestamp("2026-10-05 08:50", tz=NY))] == ["fact"]


def test_discord_commands():
    assert build_reply("!versiculo", None).startswith("📖 **Palabra del día**")
    assert build_reply("!versículo", None).endswith("(RVR1960)")
    assert build_reply("!dato", None).startswith("💡 **Dato curioso**")
    assert "!versiculo" in build_reply("!ayuda", None)


def test_runtime_publishes_the_notes(tmp_path):
    from types import MethodType, SimpleNamespace
    from signal_engine.runtime.engine import BotRuntime
    sent = []
    rt = SimpleNamespace(daily_notes=DailyNotes(tmp_path / "n.json"),
                         output_dispatcher=SimpleNamespace(publish_note=lambda **kw: sent.append(kw)))
    rt.maybe_send_daily_notes = MethodType(BotRuntime.maybe_send_daily_notes, rt)
    assert rt.maybe_send_daily_notes(pd.Timestamp("2026-10-05 12:40", tz=NY)) == 1
    assert sent[0]["title"] == "💡 Dato curioso" and sent[0]["key"] == "fact:2026-10-05"
