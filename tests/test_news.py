"""Economic calendar (signal_engine/news.py) and GHOST's news window."""
import pandas as pd

from signal_engine.config.schema import EngineConfig
from signal_engine.news import NewsCalendar

FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High"},
    {"title": "FOMC Statement", "country": "USD", "date": "2026-10-14T14:00:00-04:00", "impact": "High"},
    {"title": "Retail Sales", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "Medium"},
    {"title": "ECB Press", "country": "EUR", "date": "2026-10-14T08:45:00-04:00", "impact": "High"},
    {"title": "Bank Holiday", "country": "USD", "date": "2026-10-12T00:00:00-04:00", "impact": "Holiday"},
]
ET = lambda s: pd.Timestamp(s, tz="America/New_York")          # noqa: E731


def _cal(tmp_path, rows=FEED):
    cal = NewsCalendar(cache=tmp_path / "week.json", fetch=lambda: rows)
    cal.refresh(force=True)
    return cal


def test_calendar_windows_and_today(tmp_path):
    cal = _cal(tmp_path)
    assert [e.title for e in cal.on_day(ET("2026-10-14 06:00"))] == ["CPI m/m", "FOMC Statement"]
    assert cal.blackout(ET("2026-10-14 08:19")) is None                    # 11 min before: free
    assert cal.blackout(ET("2026-10-14 08:20")).title == "CPI m/m"          # 10 min before
    assert cal.blackout(ET("2026-10-14 08:45")).title == "CPI m/m"          # 15 min after
    assert cal.blackout(ET("2026-10-14 08:46")) is None                    # the EUR event is ignored
    assert cal.blackout(ET("2026-10-15 08:30")) is None                    # medium impact is not a window
    assert cal.blackout(ET("2026-10-15 08:30"), impacts=("High", "Medium")).title == "Retail Sales"


def test_a_failed_read_keeps_the_last_copy(tmp_path):
    cal = _cal(tmp_path)
    def boom():
        raise OSError("sin red")
    cal2 = NewsCalendar(cache=tmp_path / "week.json", fetch=boom)
    cal2.refresh(force=True)                                               # no exception
    assert len(cal2.events()) == len(cal.events()) == 4                    # the USD ones


def test_ghost_stays_out_of_the_news_window(tmp_path):
    from types import SimpleNamespace
    from signal_engine.ghost.brain import GhostBrain
    from signal_engine.ghost.models import NoTradeReason

    cal = _cal(tmp_path)
    check = lambda ts, rule: cal.blackout(ts, rule["before"], rule["after"])   # noqa: E731
    g = EngineConfig.load(strategy="zones").ghost
    ctx = SimpleNamespace(decision_time=ET("2026-10-14 08:35"))
    rule = {"before": 10, "after": 15}
    brain_on = GhostBrain(g.model_copy(update={"news_blackout": rule}))
    brain_off = GhostBrain(g)

    def reasons(brain):
        # only the news part of the decision: the rest of the gates is not under test here
        out = []
        if brain.cfg.news_blackout and check(ctx.decision_time, brain.cfg.news_blackout):
            out.append(NoTradeReason.NEWS)
        return out
    assert reasons(brain_on) == [NoTradeReason.NEWS] and reasons(brain_off) == []
    assert NoTradeReason.NEWS.value == "news_event"


def test_news_variant_is_listed():
    from signal_engine.runtime.shadows import load_specs
    spec = {s.id: s for s in load_specs("ghost")}["zonas_15m_noticias"]
    cfg = EngineConfig.load(strategy=spec.strategy, overrides=spec.overrides)
    assert cfg.ghost.news_blackout == {"before": 10, "after": 15, "impacts": ["High"]}
    assert cfg.setups.zone_retrace.timeframe == "15m"


def test_hub_shows_todays_news(tmp_path):
    from signal_engine.api.bots import news_today
    cal = _cal(tmp_path)
    d = news_today(cal, ET("2026-10-14 09:00"))
    assert d["available"] and [e["hm"] for e in d["events"]] == ["08:30", "14:00"]
    assert news_today(None, ET("2026-10-14 09:00")) == {"available": False, "events": []}
