"""Plain-language account of what GHOST sees and why it acts.

The operator asked to understand the trend the bot is actually reading — "I see
this and this, therefore I expect that" — instead of a table of enum values.
Every sentence here is a rendering of a field GHOST already computed; nothing is
inferred beyond the data, and an absent fact is omitted rather than guessed.

Written in Spanish because it is read by the operator, on their phone, in their
language. The structured fields (entry, stop, TP) stay in the embed as numbers.
"""
from __future__ import annotations

from .models import Bias, MarketRegime, MarketState, MTFAlignment

_BIAS = {Bias.BULLISH: "alcista", Bias.BEARISH: "bajista",
         Bias.NEUTRAL: "sin dirección"}

_REGIME = {
    MarketRegime.TREND_UP: "tendencia alcista",
    MarketRegime.TREND_DOWN: "tendencia bajista",
    MarketRegime.RANGE: "mercado lateral (en rango)",
    MarketRegime.TRANSITION: "mercado en transición — la tendencia puede estar girando",
    MarketRegime.HIGH_VOLATILITY: "volatilidad alta",
    MarketRegime.LOW_VOLATILITY: "volatilidad baja, mercado lento",
    MarketRegime.UNKNOWN: "régimen todavía no claro",
}

_ALIGN = {
    MTFAlignment.ALIGNED: "las tres temporalidades coinciden",
    MTFAlignment.CONFLICT: "la 1H va en contra de la 5m",
    MTFAlignment.TRANSITION: "1H y 15m no coinciden — posible cambio de tendencia",
    MTFAlignment.MIXED: "temporalidades mezcladas",
    MTFAlignment.UNKNOWN: "alineación sin datos suficientes",
}

_VWAP = {"ABOVE": "precio por encima del VWAP",
         "BELOW": "precio por debajo del VWAP",
         "AT": "precio pegado al VWAP"}

def _setup_thesis(setup: str, long: bool) -> str:
    if setup == "orb":
        edge = "por arriba" if long else "por abajo"
        return (f"Ruptura del rango de apertura: el precio salió {edge} del rango "
                f"de los primeros 30 minutos, y espero continuación.")
    if setup == "liquidity_sweep":
        taken = "mínimos" if long else "máximos"
        return (f"Barrida de liquidez: tomaron los stops de los {taken} y el "
                f"precio rechazó, así que espero el giro.")
    if setup == "vwap_reclaim":
        side, role = ("arriba", "soporte") if long else ("abajo", "resistencia")
        return (f"Recuperación del VWAP: el precio lo cruzó hacia {side}, y espero "
                f"que ahora lo use de {role}.")
    if setup == "zone_retrace":
        zone = "compra" if long else "venta"
        return (f"Regreso a zona: el precio volvió por primera vez a una zona de "
                f"{zone} (hueco de valor en 5m) y reaccionó ahí, así que espero "
                f"que respete la zona.")
    return f"Setup {setup}."


def _clock(hhmm: str) -> str:
    """'16:55' -> '4:55pm'."""
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h % 12 or 12}:{m:02d}{'pm' if h >= 12 else 'am'}"


def _sentence(s: str) -> str:
    """Uppercase the first letter only — str.capitalize() would lowercase the
    rest and turn 'VWAP' into 'vwap' and '1H' into '1h'."""
    return s[:1].upper() + s[1:] if s else s

# Negative confluence factors, phrased as the risk they represent.
_AGAINST = {
    "htf_conflict": "la 1H apunta en la dirección contraria",
    "against_structure": "la estructura de 5m va en contra",
    "regime_alignment": "el régimen no favorece este tipo de trade",
}


def _p(x: float | None) -> str:
    return f"{x:,.2f}" if x is not None else "—"


def market_read(ms: MarketState) -> list[str]:
    """What GHOST sees, one fact per line. Used on its own by !why and the
    status digest, and as the first half of a trade thesis."""
    mtf = ms.mtf
    lines = [
        (f"1H {_BIAS[mtf.context.bias]}, 15m {_BIAS[mtf.structure.bias]}, "
         f"5m {_BIAS[mtf.execution.bias]} — {_ALIGN[mtf.alignment]}."),
        f"Régimen: {_REGIME.get(ms.regime.regime, ms.regime.regime.value)}.",
    ]

    st = ms.structure
    if st.last_event_type and st.last_event_direction is not None:
        kind = ("cambio de carácter (CHoCH)" if st.last_event_type.upper() == "CHOCH"
                else "quiebre de estructura (BOS)")
        tail = " reciente — posible giro" if st.changed_recently else ""
        lines.append(f"Último {kind} {_BIAS[st.last_event_direction]}{tail}.")

    liq = ms.liquidity
    if liq.last_sweep_direction is not None and liq.last_sweep_price is not None:
        # BEARISH = sell-side liquidity (lows) was taken; BULLISH = buy-side
        # (highs). `reclaimed` means price came back to the side it swept FROM:
        # above swept lows, below swept highs — i.e. the sweep was rejected.
        lows = liq.last_sweep_direction is Bias.BEARISH
        side = "mínimos (liquidez de venta)" if lows else "máximos (liquidez de compra)"
        if liq.reclaimed:
            after = ("y el precio volvió a subir por encima — rechazo alcista" if lows
                     else "y el precio volvió a bajar por debajo — rechazo bajista")
        else:
            after = ("y el precio sigue por debajo" if lows
                     else "y el precio sigue por encima")
        lines.append(f"Barrieron {side} en {_p(liq.last_sweep_price)} {after}.")

    if ms.vwap_side in _VWAP:
        lines.append(_sentence(_VWAP[ms.vwap_side]) + ".")
    return lines


def trade_thesis(decision, *, breakeven_after_tp1: bool = False,
                 eod_close_time: str | None = None,
                 eod_close_enabled: bool = False) -> dict[str, str]:
    """{veo, por_eso, espero, en_contra, confianza} for an approved decision.
    Keys with nothing honest to say are left out rather than padded."""
    sig = decision.signal
    ms = decision.market_state
    long = decision.action == "LONG"
    risk = abs(sig.entry - sig.stop)

    out: dict[str, str] = {"veo": " ".join(market_read(ms))}

    out["por_eso"] = f"{decision.action}. {_setup_thesis(sig.setup or '', long)}"

    targets = []
    for i, tp in enumerate(sig.take_profits[:2], start=1):
        pts = abs(tp - sig.entry)
        r = pts / risk if risk else 0.0
        targets.append(f"TP{i} {_p(tp)} (+{r:.1f}R, {pts:.1f} pts)")
    move = "subir" if long else "bajar"
    espero = f"{move.capitalize()} hasta {' y luego '.join(targets)}. " if targets else ""
    espero += f"Stop en {_p(sig.stop)} ({risk:.1f} pts de riesgo)."
    if breakeven_after_tp1 and len(sig.take_profits) > 1:
        espero += (" Si toca TP1 muevo el stop a la entrada: desde ahí el trade "
                   "ya no puede perder.")
    if eod_close_enabled:
        when = _clock(eod_close_time) if eod_close_time else "4:00pm"
        espero += (" No lo dejo abierto de un día para otro: si sigue vivo a las "
                   f"{when} ET lo cierro al precio de ese momento.")
    out["espero"] = espero

    conf = decision.confluence
    risks = []
    if conf is not None:
        for c in conf.contributions:
            if c.points < 0 and c.name in _AGAINST:
                risks.append(_AGAINST[c.name])
    if ms.mtf.context.bias is Bias.NEUTRAL:
        risks.append("la 1H no confirma dirección")
    # A rejected sweep on the trade's own side points the other way: swept
    # lows that were reclaimed are bullish, which argues against a SHORT.
    want = Bias.BULLISH if long else Bias.BEARISH
    if ms.liquidity.last_sweep_direction is want and ms.liquidity.reclaimed:
        risks.append("la última barrida de liquidez apunta en contra")
    if ms.regime.regime is MarketRegime.HIGH_VOLATILITY:
        risks.append("volatilidad alta: el precio puede barrer el stop antes de moverse")
    if risks:
        out["en_contra"] = _sentence("; ".join(dict.fromkeys(risks))) + "."

    if conf is not None:
        gate = (f" (mínimo {conf.threshold:.0f})" if conf.threshold is not None
                else " (informativa; esta estrategia no la exige)")
        out["confianza"] = (f"Confluencia {conf.score:.0f}{gate} "
                            f"— es un puntaje de calidad, no una probabilidad.")
    return out


def render_thesis(thesis: dict[str, str]) -> str:
    """Discord-markdown block for the embed description."""
    labels = [("veo", "👁 Veo"), ("por_eso", "🎯 Por eso"),
              ("espero", "📈 Espero"), ("en_contra", "⚠️ En contra")]
    parts = [f"**{label}:** {thesis[key]}" for key, label in labels if thesis.get(key)]
    if thesis.get("confianza"):
        parts.append(f"_{thesis['confianza']}_")
    return "\n\n".join(parts)
