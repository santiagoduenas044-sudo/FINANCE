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
        ("v2", "2026-09-24", "Zonas 5 min · de día",
         "Huecos de precio (FVG) de 5 min, entra si una vela de 1 min reacciona en la zona. "
         "Stop 20–35 pts, objetivo 3× el stop, de 9:30 a 15:30."),
        ("v3", "2026-09-27", "Zonas 5 min · día y noche",
         "Las mismas zonas, también de noche, con el stop a entrada +2 al 70 % del camino."),
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
    {"name": "Zonas 1 min", "bot": "GHOST", "status": "retirada",
     "text": "La versión anterior de GHOST: zonas de 5 min confirmadas en 1 min.",
     "lab": "5 años, 5 MNQ: −$91,774."},
    {"name": "Zonas de noche", "bot": "GHOST", "status": "retirada",
     "text": "La versión de zonas operando también fuera del horario de día.",
     "lab": "5 años, 5 MNQ: cerca de −$167,000 (versión de 1 min)."},
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
