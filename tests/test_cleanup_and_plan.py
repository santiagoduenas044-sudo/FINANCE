"""11-oct: retired set ups, the FVG names, the Europe-only set up and the
morning plan on Discord."""
import numpy as np
import pandas as pd
import pytest

from signal_engine.ghost.brain import outside_entry_hours
from signal_engine.output import briefing
from signal_engine.output.discord import worth_sending
from signal_engine.output.events import OutputEvent
from signal_engine.runtime.shadows import is_on, load_specs, main_retired

NY = "America/New_York"


def test_only_fvg_15_and_1h_run_after_the_audit(tmp_path):
    on = {sp.setup for b in ("ghost", "orbit") for sp in load_specs(b) if is_on(b, sp, tmp_path)}
    assert on == {5, 9, 10, 14, 15, 16, 19, 20, 21}                 # 26-oct audit (+ Set up 1)
    assert main_retired("orbit") and not main_retired("ghost")


def test_a_retired_set_up_cannot_be_switched_back_on(tmp_path):
    from dataclasses import replace

    from signal_engine.runtime.shadows import set_on
    spec = replace(next(s for s in load_specs("orbit") if s.id == "apertura_tp2"), retired=True)
    set_on("orbit", "apertura_tp2", True, tmp_path)
    assert not is_on("orbit", spec, tmp_path)


def test_old_switches_go_back_to_on_once(tmp_path):
    import json

    from signal_engine.runtime.shadows import set_on, switches
    (tmp_path / "enabled.json").write_text(json.dumps({"ghost": {"zonas_15m_noticias": False}}))
    spec = next(s for s in load_specs("ghost") if s.id == "zonas_15m_noticias")
    assert is_on("ghost", spec, tmp_path)                        # the 11-oct "off" is gone
    set_on("ghost", "zonas_15m_noticias", False, tmp_path)       # a new switch stays
    assert not is_on("ghost", spec, tmp_path) and switches(tmp_path)["_reset"]


def test_orbit_runs_nothing_of_its_own_when_retired():
    from signal_engine.subbots.orbit import run_once
    seen = []

    class Shadow:
        def on_bars(self, bars):
            seen.append(bars)
    assert run_once(None, fetch=lambda: "bars", shadows=[(object(), Shadow())])
    assert seen == ["bars"]


@pytest.mark.parametrize("hm,out", [("02:59", True), ("03:00", False), ("09:29", False),
                                    ("09:30", True), ("14:00", True), ("23:00", True)])
def test_europe_window(hm, out):
    assert outside_entry_hours(hm, ["03:00", "09:30"]) is out
    assert outside_entry_hours(hm, None) is False


def test_window_across_midnight():
    assert not outside_entry_hours("23:30", ["18:00", "03:00"])
    assert outside_entry_hours("12:00", ["18:00", "03:00"])


def _m1(seed=7, end="2026-10-09 09:00"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-10-06 18:01", end, freq="1min", tz=NY)
    c = 31000 + np.cumsum(rng.normal(0.05, 3, len(idx)))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 1, "low": np.minimum(o, c) - 1,
                         "close": c}, index=idx)


def test_plan_lists_levels_lean_and_news():
    m1 = _m1()
    now = pd.Timestamp("2026-10-09 09:00", tz=NY)

    class News:
        def on_day(self, ts, impacts=()):
            from signal_engine.news import Event
            return [Event(pd.Timestamp("2026-10-09 08:30", tz=NY), "CPI m/m", "High", "USD")]
    title, text, _c, png = briefing.build(m1, now, news=News())
    assert title == "🧭 Plan del día · vie 09-10"
    assert "Resistencias" in text and "Soportes" in text
    assert "08:30 · CPI m/m · **fuerte**" in text
    assert any(w in text for w in ("COMPRAS (LONG)", "VENTAS (SHORT)", "Sin lado claro"))
    assert png is None or png[:4] == b"\x89PNG"
    parts = briefing.day_parts(m1, now)
    prev = parts["prev"]
    assert prev.index[0].strftime("%H:%M") == "09:31" and prev.index[-1].strftime("%H:%M") == "16:00"
    price = parts["price"]
    lines = text.splitlines()
    res = lines[lines.index("🔴 **Resistencias** (arriba)") + 1:lines.index("🟢 **Soportes** (abajo)")]
    assert res and all(float(line.split(" · ")[0].replace(",", "")) > price for line in res)


def test_plan_reading_of_the_day():
    idx = pd.date_range("2026-10-08 09:31", "2026-10-09 09:00", freq="1min", tz=NY)
    up = pd.Series(np.linspace(30000, 30400, len(idx)), index=idx)      # rose all day and night
    m1 = pd.DataFrame({"open": up - 1, "high": up + 1, "low": up - 2, "close": up})
    side, why = briefing.bias(briefing.day_parts(m1, pd.Timestamp("2026-10-09 09:00", tz=NY)))
    assert side == "LONG" and any("ayer cerró al alza" in w for w in why)
    side, _ = briefing.bias(briefing.day_parts(-m1 + 61000, pd.Timestamp("2026-10-09 09:00", tz=NY)))
    assert side == "SHORT"


def test_plan_goes_out_once_on_weekday_mornings(tmp_path, monkeypatch):
    st = tmp_path / "b.json"
    monkeypatch.delenv("BOT_PLAN", raising=False)
    assert not briefing.due(pd.Timestamp("2026-10-09 08:59", tz=NY), st)
    assert briefing.due(pd.Timestamp("2026-10-09 09:00", tz=NY), st)
    briefing.mark_sent(pd.Timestamp("2026-10-09 09:00", tz=NY), st)
    assert not briefing.due(pd.Timestamp("2026-10-09 09:05", tz=NY), st)
    assert not briefing.due(pd.Timestamp("2026-10-10 09:00", tz=NY), st)        # Saturday
    assert briefing.due(pd.Timestamp("2026-10-12 09:00", tz=NY), st)            # Monday
    monkeypatch.setenv("BOT_PLAN", "off")
    assert not briefing.due(pd.Timestamp("2026-10-12 09:00", tz=NY), st)


def test_plan_goes_to_the_general_channel():
    from signal_engine.output.discord_channels import setup_of
    ev = OutputEvent.from_note("🧭 Plan del día", "x", 1, "briefing:2026-10-09")
    assert worth_sending(ev) and setup_of(ev, 1) is None
