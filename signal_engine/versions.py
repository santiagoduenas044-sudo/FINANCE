"""Each bot's versions (what changed and since when) and the strategy catalogue
with what the lab said about each one. The dashboard's bot hub reads this.

A new version goes at the END of its bot's list; the last one is the version
running now, and every new trade is tagged with it (GHOST: the trade's
context["bot_version"]; ORBIT: the trade's "version"). Trades from before the
tagging existed are placed by their date.
"""
from __future__ import annotations

import pandas as pd

# bot -> [(version, from ET date, title, rules)]
BOT_VERSIONS: dict[str, list[tuple[str, str, str, str]]] = {
    "GHOST": [
        ("v1", "2026-09-15", "CLÁSICO · setups clásicos",
         "Ruptura de apertura, barrida de liquidez y vuelta al VWAP en velas de 5 min, "
         "con un filtro de confluencia."),
        ("v2", "2026-09-24", "FVG 1 DÍA · zonas 1 min de día",
         "Huecos de precio (FVG) de 5 min, entra si una vela de 1 min reacciona en la zona. "
         "Stop 20–35 pts, objetivo 3× el stop, de 9:30 a 15:30."),
        ("v3", "2026-09-27", "FVG 1 · zonas 1 min día y noche",
         "La misma versión de 1 min, también de noche, con el stop a entrada +2 al 70 % "
         "del camino."),
        ("v4", "2026-09-29", "FVG 15 · zonas 15 min de día",
         "Huecos de 15 min confirmados por una vela de 15 min. Stop 20–35 pts desde la "
         "entrada, objetivo 3×, entradas 9:30–15:30, cierre 16:55, sin límites diarios."),
        ("v5", "2026-10-06", "FVG 15 · zonas 15 min, stop según el mercado",
         "Las mismas reglas, pero el stop, el objetivo y el tamaño de las zonas siguen el "
         "tamaño de las velas de 15 min: 20–35 pts cuando la vela media es de 20; con velas "
         "de 45, 45–79 pts y objetivo 3×. La versión fija sigue en paralelo (Set up 9)."),
        ("v6", "2026-10-11", "FVG 15 · asegura a la mitad",
         "Las reglas de v5, y a la mitad del camino al objetivo cierra 2 de los 5 MNQ y sube "
         "el stop de los otros 3 a entrada +2; esos siguen hasta el objetivo."),
        ("v7", "2026-10-12", "FVG 15 · espacio libre",
         "Las reglas de v6, y no entra si un soporte o resistencia (máximo, mínimo o cierre "
         "de ayer, máximo o mínimo de la noche, o un nivel que frenó al precio 3 veces) está "
         "en la primera mitad del camino al objetivo."),
    ],
    "ORBIT": [
        ("v1", "2026-09-29", "APERTURA · 1 MNQ",
         "Caja de 9:30 a 9:45, entra cuando una vela de 5 min cierra fuera, stop del otro "
         "lado, objetivo 1.5× el stop, entrada +2 al 70 %, cierre 16:55. 1 MNQ."),
        ("v2", "2026-09-29", "APERTURA · 1 NQ",
         "Las mismas reglas con 1 NQ ($20 por punto)."),
        ("v3", "2026-09-30", "APERTURA · 5 MNQ",
         "Las mismas reglas con 5 MNQ ($10 por punto), como GHOST: un stop grande de la "
         "caja cuesta la mitad que con 1 NQ."),
    ],
}

# The strategy catalogue. Every strategy has a short NAME (its code) and a
# description. Its paper record adds up only what is really its own:
#   "real":    the bot versions that traded it for real ({bot: [versions]}),
#   "shadows": the parallel variants that run it (config/shadows.yaml).
# "lab" is what the 5-year lab said; "lab_prefix" finds its rows in the
# lab's latest run (data_store/lab/*.json) for the ideas still to be tested.
# status: "en vivo" (a bot trades it) · "en paralelo" / "apagada" (decided by
# its variants' switches) · "en estudio" · "por probar" · "descartada".
STRATEGIES: list[dict] = [
    {"code": "FVG 15", "name": "Zonas 15 min", "bot": "GHOST", "status": "en vivo",
     "real": {"GHOST": ["v4", "v5", "v6", "v7"]}, "shadows": ["zonas_15m_fijo"],
     "text": "Huecos de precio (FVG) de 15 min: espera el primer regreso del precio al hueco y "
             "entra cuando una vela reacciona; objetivo 3× el stop. Desde el 6 de octubre el "
             "stop sigue el tamaño de las velas (Set up 1).",
     "lab": "5 años, 5 MNQ: +$25 por trade en 2021–23 y +$12 en 2024–26. Gana en los dos "
            "periodos, pero todavía sin suficiente confianza."},
    {"code": "APERTURA", "name": "Ruptura de la caja de apertura", "bot": "ORBIT",
     "status": "en vivo", "real": {"ORBIT": ["v1", "v2", "v3"]},
     "text": "Ruptura de la caja de 9:30 a 9:45, objetivo 1.5× el stop, entrada +2 al 70 %. "
             "Estuvo apagada del 11 al 19 de oct (no llegaba al objetivo y sus stops eran de "
             "100–190 pts); se volvió a encender para compararla.",
     "lab": "5 años, 5 MNQ: +$109,070, peor caída −$16,969. La única que pasó la prueba "
            "a ciegas."},
    {"code": "FVG 1 DÍA", "name": "Zonas 1 min · de día", "bot": "GHOST", "status": "en paralelo",
     "real": {"GHOST": ["v2"]}, "shadows": ["zonas_1m"],
     "text": "La primera estrategia de zonas de GHOST (huecos de 5 min confirmados en 1 min), "
             "de 9:30 a 15:30. Fue la principal del 24 al 26 de sep; ahora corre en papel al "
             "lado de FVG 15 para compararlas.",
     "lab": "5 años, 5 MNQ: −$91,774."},
    {"code": "FVG 1", "name": "Zonas 1 min · día y noche", "bot": "GHOST",
     "status": "en paralelo", "real": {"GHOST": ["v3"]}, "shadows": ["zonas_1m_noche"],
     "text": "Huecos (FVG) de 5 min con entrada en una vela de 1 min, de día y de noche. Fue "
             "la principal el 27 y 28 de sep.",
     "lab": "5 años, 5 MNQ: cerca de −$167,000."},
    {"code": "FVG 1 EUROPA", "name": "Zonas 1 min solo en horario de Europa", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_1m_europa"],
     "text": "FVG 1 que solo entra de 3:00 a 9:30 (hora de NY), donde el Set up 4 ganaba "
             "(21 trades, +$2,323 al 10 de oct).",
     "lab": "Nueva (11 de oct); se mide en papel."},
    {"code": "FVG 1H", "name": "Zonas 1 hora · día y noche", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_1h"],
     "text": "La estrategia de FVG 15 en velas de 1 hora: hueco entre velas de 1 hora, entra "
             "cuando una vela de 1 hora reacciona en el primer regreso. Stops más grandes "
             "(según las velas de 1 hora, hasta 120 pts), objetivo 3×, asegura a la mitad.",
     "lab": "Nueva (19 de oct); se mide en papel."},
    {"code": "FVG 1H DÍA", "name": "Zonas 1 hora · solo de día", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_1h_dia"],
     "text": "FVG 1H con entradas solo de 9:30 a 15:30.",
     "lab": "Nueva (19 de oct); se mide en papel."},
    {"code": "FVG 15 FILTROS", "name": "Zonas 15 min con filtros", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_15m_filtros"],
     "text": "FVG 15 (Set up 1) sin entrar tarde, con stop máximo de 50 pts y sin entrar "
             "en serrucho.",
     "lab": "Nueva (19 de oct); se mide en papel."},
    {"code": "FVG 1 FILTROS", "name": "Zonas 1 min día y noche con filtros", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_1m_noche_filtros"],
     "text": "FVG 1 (Set up 4) sin entrar tarde ni en serrucho.",
     "lab": "Nueva (19 de oct); se mide en papel."},
    {"code": "FVG 1 EUROPA FILTROS", "name": "Zonas 1 min de Europa con filtros",
     "bot": "GHOST", "status": "en paralelo", "shadows": ["zonas_1m_europa_filtros"],
     "text": "FVG 1 EUROPA (Set up 13) sin entrar tarde ni en serrucho.",
     "lab": "Nueva (19 de oct); se mide en papel."},
    {"code": "FVG 15 NIVEL", "name": "Zonas 15 min con objetivo en el siguiente nivel",
     "bot": "GHOST", "status": "en paralelo", "shadows": ["zonas_15m_nivel"],
     "text": "FVG 15 (Set up 1) con el objetivo justo antes del siguiente soporte o "
             "resistencia en vez de 3× fijo.",
     "lab": "Nueva (21 de oct); se mide en papel."},
    {"code": "FVG 15 TIEMPO", "name": "Zonas 15 min con salida por tiempo", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_15m_tiempo"],
     "text": "FVG 15 (Set up 1) que cierra el trade si en 90 min no avanzó un tercio del camino.",
     "lab": "Nueva (21 de oct); se mide en papel."},
    {"code": "FVG 15 PLAN", "name": "Zonas 15 min a favor del plan del día", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_15m_plan"],
     "text": "FVG 15 (Set up 1) que solo opera hacia donde dice el plan de las 9:00.",
     "lab": "Nueva (21 de oct); se mide en papel."},
    {"code": "FVG 15 CALMA", "name": "Zonas 15 min sin noticias", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_15m_noticias"],
     "text": "FVG 15 sin entrar de 10 min antes a 15 min después de CPI, empleo, FOMC y "
             "otras noticias fuertes de EE.UU.",
     "lab": "Sin prueba de 5 años (no hay calendario histórico); se mide en papel."},
    {"code": "FVG 15 VWAP", "name": "Zonas 15 min a favor del VWAP", "bot": "GHOST",
     "status": "en paralelo", "shadows": ["zonas_15m_tendencia"],
     "text": "FVG 15 que solo compra con el precio arriba del VWAP del día y solo vende "
             "abajo: no opera contra la corriente.",
     "lab": "Nueva (8 de oct); se mide en papel."},
    {"code": "FVG 1 VWAP", "name": "Zonas 1 min día y noche a favor del VWAP",
     "bot": "GHOST", "status": "en paralelo", "shadows": ["zonas_1m_noche_tendencia"],
     "text": "FVG 1 que solo compra arriba del VWAP y solo vende abajo.",
     "lab": "Nueva (8 de oct); se mide en papel."},
    {"code": "FVG 1 CALMA", "name": "Zonas 1 min día y noche sin noticias",
     "bot": "GHOST", "status": "en paralelo", "shadows": ["zonas_1m_noche_noticias"],
     "text": "FVG 1 sin entrar de 10 min antes a 15 min después de una noticia fuerte.",
     "lab": "Sin prueba de 5 años (no hay calendario histórico); se mide en papel."},
    {"code": "APERTURA 30", "name": "Caja de apertura de 30 min", "bot": "ORBIT",
     "status": "en paralelo", "shadows": ["apertura_30m"],
     "text": "APERTURA con la caja de 9:30 a 10:00; todo lo demás igual.",
     "lab": "Parte del estudio de apertura (--estudio apertura)."},
    {"code": "APERTURA 2×", "name": "Caja de apertura con objetivo 2×", "bot": "ORBIT",
     "status": "en paralelo", "shadows": ["apertura_tp2"],
     "text": "APERTURA con el objetivo a 2× el stop en vez de 1.5×.",
     "lab": "Parte del estudio de apertura (--estudio apertura)."},
    {"code": "CLÁSICO", "name": "Setups clásicos", "bot": "GHOST", "status": "en paralelo",
     "real": {"GHOST": ["v1"]}, "shadows": ["setups_clasicos"],
     "text": "La primera versión de GHOST: ruptura de apertura, barrida de liquidez y vuelta "
             "al VWAP en velas de 5 min.",
     "lab": "No pasó la prueba a ciegas."},
    {"code": "ESCUDO", "name": "Asegurar ganancia por escalones", "bot": "GHOST",
     "status": "en estudio",
     "text": "Subir el stop por escalones cuando el trade va bien (BE+2, 30 %, 60 %…).",
     "lab": "Pendiente: 'Cómo asegurar ganancia (GHOST)' en el laboratorio."},
    # new ideas: the lab's "Estrategias nuevas" study (strategy_lab.py --estudio nuevas)
    {"code": "MURO", "name": "Ruptura de soportes y resistencias", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "MURO",
     "text": "Como APERTURA, pero la caja la dibujan los soportes y resistencias del momento: "
             "el precio rebota 2+ veces en un techo y en un piso (2 o 4 horas); cuando una "
             "vela de 5 min cierra fuera, entra hacia ese lado. Prueba el stop al otro lado, "
             "a la mitad, tras el nivel roto o tras la EMA 9/20, y como objetivo 1.5–3× o la "
             "altura del rango.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "RELLENO", "name": "Relleno del hueco de apertura", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "RELLENO",
     "text": "Si el NQ abre lejos del cierre de ayer (un hueco), apuesta a que vuelve a "
             "cerrarlo: entra contra el hueco cuando la primera vela de 5 min se da vuelta, "
             "objetivo el cierre de ayer.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "TRAMPA", "name": "Barrida de máx/mín de ayer", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "TRAMPA",
     "text": "El precio pasa el máximo (o mínimo) de ayer o de la noche, saca los stops y "
             "vuelve a cerrar adentro: entra al revés, stop detrás de la barrida.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "RESORTE", "name": "Ruptura tras un día comprimido", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "RESORTE",
     "text": "Si ayer fue el día de rango más estrecho de la semana (NR7), hoy opera la "
             "ruptura del máximo o mínimo de ayer: tras la calma suele venir el movimiento.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "CIERRE", "name": "Tendencia de la última hora", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "CIERRE",
     "text": "A las 14:30–15:00, si el día va claramente en una dirección (arriba del VWAP y "
             "de la apertura, o abajo), entra a favor hasta el cierre.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "APERTURA VWAP", "name": "Caja de apertura a favor del VWAP", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "APERTURA VWAP",
     "text": "APERTURA, pero solo si la ruptura va del mismo lado del VWAP del día.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "IMPULSO", "name": "Primer impulso de la mañana", "bot": "Laboratorio",
     "status": "por probar", "lab_prefix": "IMPULSO",
     "text": "Si los primeros 30 min se mueven fuerte en una dirección, entra a favor en el "
             "primer retroceso al VWAP o a la EMA 20 de 5 min.",
     "lab": "Por probar: 'Estrategias nuevas' en el laboratorio."},
    {"code": "PÉNDULO", "name": "Vuelta al VWAP", "bot": "Laboratorio", "status": "descartada",
     "text": "Vender/comprar cuando el precio se aleja mucho del VWAP.",
     "lab": "Pierde en los dos periodos."},
    {"code": "NIVELES", "name": "Cajas, niveles de ayer y rebotes en EMA", "bot": "Laboratorio",
     "status": "descartada",
     "text": "Rupturas de cajas de 30/60 min, de máximos y mínimos de ayer o de la noche, "
             "y rebotes en EMA 9/20/50/200.",
     "lab": "Ninguna gana en 2021–23 y en 2024–26 a la vez."},
    {"code": "BRÚJULA", "name": "Filtro de NQ y de las grandes", "bot": "Laboratorio",
     "status": "descartada",
     "text": "Operar solo si NQ o las 8 grandes (NVDA, MSFT, AAPL…) van a favor.",
     "lab": "No mejora a las estrategias que filtra."},
]


# What each version is called on the page and in messages: plain words, no codes.
VERSION_NAMES: dict[tuple[str, str], str] = {
    ("GHOST", "v1"): "Setups clásicos",
    ("GHOST", "v2"): "Zonas de 1 minuto",
    ("GHOST", "v3"): "Zonas de 1 minuto, día y noche",
    ("GHOST", "v4"): "Zonas de 15 minutos",
    ("GHOST", "v5"): "Zonas de 15 minutos, stop según el mercado",
    ("GHOST", "v6"): "FVG de 15 minutos que asegura a la mitad",
    ("GHOST", "v7"): "FVG de 15 minutos con espacio libre",
    ("ORBIT", "v1"): "Caja de apertura con 1 MNQ",
    ("ORBIT", "v2"): "Caja de apertura con 1 NQ",
    ("ORBIT", "v3"): "Caja de apertura con 5 MNQ",
}
_MES = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def version_name(bot: str, vid: str) -> str:
    """"Versión 4 · Zonas de 15 minutos"."""
    return f"Versión {vid.lstrip('v')} · {VERSION_NAMES.get((bot, vid), version_title(bot, vid))}"


def version_span(bot: str, vid: str) -> str:
    """When it ran: "desde 29 sep", "27–28 sep" or "15–23 sep"."""
    rows = BOT_VERSIONS[bot]
    i = next(k for k, r in enumerate(rows) if r[0] == vid)
    d0 = pd.Timestamp(rows[i][1])
    if i == len(rows) - 1:
        return f"desde {d0.day} {_MES[d0.month - 1]}"
    d1 = max(d0, pd.Timestamp(rows[i + 1][1]) - pd.Timedelta(days=1))
    if d1 == d0:
        return f"{d0.day} {_MES[d0.month - 1]}"
    if d1.month == d0.month:
        return f"{d0.day}–{d1.day} {_MES[d0.month - 1]}"
    return f"{d0.day} {_MES[d0.month - 1]} – {d1.day} {_MES[d1.month - 1]}"


def version_title(bot: str, vid: str) -> str:
    return next((t for v, _s, t, _r in BOT_VERSIONS[bot] if v == vid), vid)


def current_version(bot: str) -> str:
    return BOT_VERSIONS[bot][-1][0]


# Every strategy that trades (for real or in parallel) is a numbered "set up".
# The bots themselves are 1 and 2; the parallel ones carry their number in
# config/shadows.yaml (`setup:`).
MAIN_SETUP = {"GHOST": 1, "ORBIT": 2}


def main_setup_label(bot: str) -> str:
    """"Set up 1 · FVG 15": the bot's number and the code it trades now."""
    code = version_title(bot, current_version(bot)).split(" · ")[0]
    return f"Set up {MAIN_SETUP[bot]} · {code}"


def version_by_date(bot: str, when) -> str:
    """The version that was current on `when`'s ET date (for untagged trades)."""
    ts = pd.Timestamp(when)
    ts = ts.tz_localize("America/New_York") if ts.tzinfo is None else ts.tz_convert(
        "America/New_York")
    day = ts.date().isoformat()
    found = BOT_VERSIONS[bot][0][0]
    for vid, start, _title, _rules in BOT_VERSIONS[bot]:
        if start <= day:
            found = vid
    return found


def ghost_version_of(trade) -> str:
    """Which GHOST version made a paper trade. New trades carry the tag; for
    older ones it is read from the trade itself rather than the calendar,
    because the Mac did not always update the day the code changed:
      * setups other than the zones (ORB, sweep, VWAP)      -> v1
      * zones confirmed on a 15-minute candle              -> v4
      * zones confirmed on a 1-minute candle: at night, or on 27-28 Sep
        (when that version traded day and night)          -> v3, else v2."""
    ctx = getattr(trade, "context", None) or {}
    if ctx.get("bot_version"):
        return ctx["bot_version"]
    if getattr(trade, "setup", None) != "zone_retrace":
        return "v1"
    tf = getattr(trade, "timeframe", None)
    if tf == "15m":
        return "v4"
    if tf not in (None, "1m"):
        return version_by_date("GHOST", trade.opened_at)
    ts = pd.Timestamp(trade.opened_at)
    ts = ts.tz_localize("America/New_York") if ts.tzinfo is None else ts.tz_convert(
        "America/New_York")
    night = ctx.get("session") not in (None, "RTH") or not ("09:30" <= ts.strftime("%H:%M") < "16:00")
    if night or "2026-09-27" <= ts.date().isoformat() <= "2026-09-28":
        return "v3"
    return "v2"


def orbit_version_of(trade: dict) -> str:
    """Which ORBIT version made a closed trade, from the size it traded."""
    if trade.get("version"):
        return trade["version"]
    if trade.get("symbol") == "NQ":
        return "v2"
    if trade.get("symbol") == "MNQ" and int(trade.get("contracts") or 1) >= 5:
        return "v3"
    return "v1"
