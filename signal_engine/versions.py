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
        ("v1", "2026-09-15", "Setups clásicos",
         "Ruptura de apertura, barrida de liquidez y vuelta al VWAP en velas de 5 min, "
         "con un filtro de confluencia."),
        ("v2", "2026-09-24", "Zonas 1 min · de día",
         "Huecos de precio (FVG) de 5 min, entra si una vela de 1 min reacciona en la zona. "
         "Stop 20–35 pts, objetivo 3× el stop, de 9:30 a 15:30."),
        ("v3", "2026-09-27", "Zonas 1 min · día y noche",
         "La misma versión de 1 min, también de noche, con el stop a entrada +2 al 70 % "
         "del camino."),
        ("v4", "2026-09-29", "Zonas 15 min · solo de día",
         "Huecos de 15 min confirmados por una vela de 15 min. Stop 20–35 pts desde la "
         "entrada, objetivo 3×, entradas 9:30–15:30, cierre 16:55, sin límites diarios."),
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

# name, where it runs, status, what it does, what the lab measured
STRATEGIES: list[dict] = [
    {"name": "Zonas 15 min", "bot": "GHOST", "status": "en vivo",
     "text": "Huecos de precio de 15 min, primer regreso, stop 20–35 pts, objetivo 3×.",
     "lab": "5 años, 5 MNQ: +$25 por trade en 2021–23 y +$12 en 2024–26. Gana en los dos "
            "periodos, pero todavía sin suficiente confianza."},
    {"name": "APERTURA", "bot": "ORBIT", "status": "en vivo",
     "text": "Ruptura de la caja de 9:30 a 9:45, objetivo 1.5× el stop, entrada +2 al 70 %.",
     "lab": "5 años, 5 MNQ: +$109,070, peor caída −$16,969. La única que pasó la prueba "
            "a ciegas."},
    {"name": "Asegurar ganancia (escalones)", "bot": "GHOST", "status": "en estudio",
     "text": "Subir el stop por escalones cuando el trade va bien (BE+2, 30 %, 60 %…).",
     "lab": "Pendiente: --estudio proteccion en la Mac."},
    {"name": "Evitar noticias fuertes", "bot": "GHOST", "status": "en paralelo",
     "shadows": ["zonas_15m_noticias"],
     "text": "La de 15 min sin entrar de 10 min antes a 15 min después de CPI, empleo, "
             "FOMC y otras noticias fuertes de EE.UU.",
     "lab": "Sin prueba de 5 años (no hay calendario histórico); se mide en papel."},
    {"name": "Zonas 1 min", "bot": "GHOST", "status": "en paralelo", "shadows": ["zonas_1m"],
     "text": "La versión anterior de GHOST (zonas de 5 min confirmadas en 1 min), corriendo "
             "en papel al lado de la de 15 min para compararlas.",
     "lab": "5 años, 5 MNQ: −$91,774."},
    {"name": "Zonas 1 min de noche", "bot": "GHOST", "status": "en paralelo",
     "shadows": ["zonas_1m_noche"],
     "text": "La misma versión de 1 min también fuera del horario de día, en papel.",
     "lab": "5 años, 5 MNQ: cerca de −$167,000."},
    {"name": "APERTURA con otras reglas", "bot": "ORBIT", "status": "en paralelo",
     "shadows": ["apertura_30m", "apertura_tp2"],
     "text": "La ruptura de apertura con caja de 30 min y con objetivo 2×, en papel al lado "
             "de la de ORBIT.",
     "lab": "Parte del estudio de apertura (--estudio apertura)."},
    {"name": "Vuelta al VWAP", "bot": "Laboratorio", "status": "descartada",
     "text": "Vender/comprar cuando el precio se aleja mucho del VWAP.",
     "lab": "Pierde en los dos periodos."},
    {"name": "Cajas, niveles de ayer y rebotes en EMA", "bot": "Laboratorio",
     "status": "descartada",
     "text": "Rupturas de cajas de 30/60 min, de máximos y mínimos de ayer o de la noche, "
             "y rebotes en EMA 9/20/50/200.",
     "lab": "Ninguna gana en 2021–23 y en 2024–26 a la vez."},
    {"name": "Filtro de NQ y de las grandes", "bot": "Laboratorio", "status": "descartada",
     "text": "Operar solo si NQ o las 8 grandes (NVDA, MSFT, AAPL…) van a favor.",
     "lab": "No mejora a las estrategias que filtra."},
]


def current_version(bot: str) -> str:
    return BOT_VERSIONS[bot][-1][0]


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
