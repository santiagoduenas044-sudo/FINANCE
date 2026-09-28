"""GHOST's plain-language reasoning — "veo esto, por eso espero aquello".

Each test pins a mistake that was actually made while writing the narrative and
caught only by rendering real decisions from the sample:
  * str.capitalize() lowercased the rest, printing "vwap" and "la 1h";
  * a SHORT vwap_reclaim said VWAP would act as "apoyo" (support);
  * a swept HIGH that was rejected read as "the price recovered them".
"""
import pandas as pd

from signal_engine.ghost.models import (
    Bias, ConfluenceContribution, ConfluenceResult, GhostDecision, LiquidityState,
    MarketRegime, MarketState, MTFAlignment, MTFContext, RegimeState,
    StructureState, TimeframeBias, Volatility,
)
from signal_engine.ghost.narrative import market_read, render_thesis, trade_thesis
from signal_engine.output.discord import build_discord_payload
from signal_engine.output.events import OutputEvent
from signal_engine.signals.models import Signal, SignalDirection

TS = pd.Timestamp("2026-06-15 10:00", tz="America/New_York")


def _ms(*, htf=Bias.BULLISH, s15=Bias.BULLISH, s5=Bias.BULLISH,
        align=MTFAlignment.ALIGNED, regime=MarketRegime.TREND_UP,
        sweep=None, sweep_px=None, reclaimed=False, vwap="ABOVE",
        event="BOS", event_dir=Bias.BULLISH, changed=False):
    reg = RegimeState(regime, Volatility.NORMAL, 0.5, 0.5, 100.0, 100.0, 100.0, "", None)
    mtf = MTFContext(TimeframeBias("1H", htf), TimeframeBias("15m", s15),
                     TimeframeBias("5m", s5), align)
    st = StructureState(s5, changed, event, event_dir, "")
    liq = LiquidityState(sweep, sweep_px, None, reclaimed, None, None, "")
    return MarketState("NQ", TS, "RTH", 100.0, 100.0, vwap, reg, mtf, st, liq, [])


def _decision(action="LONG", setup="orb", entry=20000.0, stop=19980.0,
              tps=(20020.0, 20040.0), ms=None, contribs=(), score=50.0):
    d = SignalDirection.LONG if action == "LONG" else SignalDirection.SHORT
    risk = abs(entry - stop)
    sig = Signal(signal_id="x", instrument="NQ", setup=setup, direction=d,
                 timestamp=TS, timeframe="5m", entry=entry, stop=stop,
                 take_profits=list(tps),
                 risk_reward=[abs(t - entry) / risk for t in tps],
                 reasons=[], invalidation_price=stop)
    conf = ConfluenceResult(action, score, 35.0, True,
                            [ConfluenceContribution(n, p) for n, p in contribs])
    return GhostDecision(action=action, instrument="NQ", setup=setup,
                         timestamp=TS, market_state=ms or _ms(),
                         confluence=conf, signal=sig)


# --------------------------------------------------------------------------- #
# market read
# --------------------------------------------------------------------------- #
def test_market_read_states_every_timeframe_and_their_agreement():
    line = market_read(_ms())[0]
    assert "1H alcista" in line and "15m alcista" in line and "5m alcista" in line
    assert "coinciden" in line


def test_market_read_names_the_regime():
    assert any("tendencia bajista" in l
               for l in market_read(_ms(regime=MarketRegime.TREND_DOWN)))


def test_vwap_keeps_its_capitals():
    """capitalize() turned 'VWAP' into 'vwap'."""
    lines = market_read(_ms(vwap="BELOW"))
    assert "Precio por debajo del VWAP." in lines


def test_rejected_sweep_of_lows_reads_as_bullish_rejection():
    lines = " ".join(market_read(_ms(sweep=Bias.BEARISH, sweep_px=19950.0,
                                     reclaimed=True)))
    assert "mínimos" in lines and "volvió a subir por encima" in lines


def test_rejected_sweep_of_highs_reads_as_bearish_rejection():
    """A swept HIGH that was reclaimed means price fell back BELOW it — not
    that price 'recovered' the highs."""
    lines = " ".join(market_read(_ms(sweep=Bias.BULLISH, sweep_px=20050.0,
                                     reclaimed=True)))
    assert "máximos" in lines and "volvió a bajar por debajo" in lines
    assert "recuper" not in lines


def test_unreclaimed_sweep_says_the_break_is_holding():
    lines = " ".join(market_read(_ms(sweep=Bias.BULLISH, sweep_px=20050.0,
                                     reclaimed=False)))
    assert "sigue por encima" in lines


def test_absent_facts_are_omitted_not_invented():
    lines = " ".join(market_read(_ms(sweep=None, event=None, event_dir=None,
                                     vwap="UNKNOWN")))
    assert "Barrieron" not in lines and "VWAP" not in lines
    assert "quiebre" not in lines and "CHoCH" not in lines


def test_recent_choch_is_flagged_as_a_possible_turn():
    lines = " ".join(market_read(_ms(event="CHOCH", event_dir=Bias.BEARISH,
                                     changed=True)))
    assert "CHoCH" in lines and "posible giro" in lines


# --------------------------------------------------------------------------- #
# trade thesis
# --------------------------------------------------------------------------- #
def test_thesis_has_see_therefore_expect():
    t = trade_thesis(_decision())
    assert set(t) >= {"veo", "por_eso", "espero", "confianza"}


def test_vwap_reclaim_long_calls_vwap_support():
    t = trade_thesis(_decision("LONG", "vwap_reclaim"))
    assert "soporte" in t["por_eso"]


def test_vwap_reclaim_short_calls_vwap_resistance():
    """It said 'apoyo' (support) for a SHORT."""
    t = trade_thesis(_decision("SHORT", "vwap_reclaim", entry=20000, stop=20020,
                               tps=(19980, 19960)))
    assert "resistencia" in t["por_eso"] and "apoyo" not in t["por_eso"]


def test_expectation_quotes_targets_in_points_and_r():
    t = trade_thesis(_decision())
    assert "Subir hasta TP1 20,020.00 (+20.0 pts)" in t["espero"]
    assert "TP2 20,040.00 (+40.0 pts)" in t["espero"]
    assert "20.0 pts de riesgo" in t["espero"]


def test_short_expectation_says_down():
    t = trade_thesis(_decision("SHORT", entry=20000, stop=20020,
                               tps=(19980, 19960)))
    assert t["espero"].startswith("Bajar hasta")


def test_breakeven_promise_only_when_it_will_actually_happen():
    two = _decision()
    one = _decision(tps=(20020.0,))
    assert "stop a la entrada" in trade_thesis(two, breakeven_after_tp1=True)["espero"]
    assert "stop a la entrada" not in trade_thesis(two)["espero"]
    # a single-target trade closes AT TP1, so there is nothing to move
    assert "stop a la entrada" not in trade_thesis(one, breakeven_after_tp1=True)["espero"]


def test_eod_close_promise_only_when_the_guard_is_on():
    """The operator asked not to hold overnight — the thesis should say so
    when the guard that actually enforces it is on, and stay silent otherwise
    so the message never promises behaviour the bot won't deliver."""
    d = _decision()
    assert "4:00pm" in trade_thesis(d, eod_close_enabled=True)["espero"]
    assert "4:00pm" not in trade_thesis(d)["espero"]
    assert "4:00pm" not in trade_thesis(d, eod_close_enabled=False)["espero"]


def test_negative_factors_become_risks():
    t = trade_thesis(_decision(contribs=[("htf_conflict", -12.5),
                                         ("against_structure", -7.5)]))
    assert "1H apunta en la dirección contraria" in t["en_contra"]
    assert "estructura de 5m va en contra" in t["en_contra"]


def test_risk_line_keeps_its_capitals():
    """capitalize() turned '1H' into '1h'."""
    t = trade_thesis(_decision(ms=_ms(htf=Bias.NEUTRAL)))
    assert "1H" in t["en_contra"] and "1h" not in t["en_contra"]


def test_opposing_sweep_is_called_out_against_the_trade():
    """Swept lows that were reclaimed are bullish — that argues against a SHORT."""
    ms = _ms(sweep=Bias.BEARISH, sweep_px=20010.0, reclaimed=True)
    short = trade_thesis(_decision("SHORT", entry=20000, stop=20020,
                                   tps=(19980, 19960), ms=ms))
    assert "barrida de liquidez apunta en contra" in short["en_contra"]


def test_supportive_sweep_is_not_called_a_risk():
    ms = _ms(sweep=Bias.BEARISH, sweep_px=19990.0, reclaimed=True)
    t = trade_thesis(_decision("LONG", ms=ms))
    assert "barrida" not in t.get("en_contra", "")


def test_clean_trade_has_no_invented_risks():
    t = trade_thesis(_decision())
    assert "en_contra" not in t


def test_confidence_is_never_presented_as_a_probability():
    t = trade_thesis(_decision(score=58.0))
    assert "58" in t["confianza"] and "no una probabilidad" in t["confianza"]


def test_render_skips_sections_with_nothing_to_say():
    out = render_thesis({"veo": "a", "por_eso": "b", "espero": "c"})
    assert "Veo" in out and "Espero" in out and "En contra" not in out


# --------------------------------------------------------------------------- #
# Discord wiring
# --------------------------------------------------------------------------- #
def test_trade_opened_embed_leads_with_the_thesis():
    thesis = trade_thesis(_decision(), breakeven_after_tp1=True)
    ev = OutputEvent.from_trade_opened({
        "signal_id": "x", "instrument": "NQ", "direction": "LONG", "setup": "orb",
        "entry": 20000.0, "stop": 19980.0, "tp1": 20020.0, "tp2": 20040.0,
        "context": {"thesis": thesis}})
    desc = build_discord_payload(ev)["embeds"][0]["description"]
    assert "👁 Veo" in desc and "📈 Espero" in desc


def test_trade_opened_without_a_thesis_still_renders():
    ev = OutputEvent.from_trade_opened({
        "signal_id": "x", "instrument": "NQ", "direction": "LONG",
        "entry": 20000.0, "stop": 19980.0, "context": {}})
    assert build_discord_payload(ev)["embeds"][0]["description"]


def test_digest_shows_the_market_read_per_symbol():
    ev = OutputEvent.from_status_digest(
        {"status": "ONLINE", "market_read": {"NQ": ["1H alcista.", "Régimen: rango."]}},
        TS)
    fields = {f["name"]: f["value"]
              for f in build_discord_payload(ev)["embeds"][0]["fields"]}
    assert "Régimen: rango." in fields["👁 Lo que veo en NQ"]
