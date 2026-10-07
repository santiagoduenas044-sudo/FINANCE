"""The record of every set up as a PDF, downloadable from the dashboard.

One page that lists each set up on its own line (never added together), then
each set up's own pages: its total, its curve and every closed trade. Built
with matplotlib (already a dependency), so no new package is needed.
"""
from __future__ import annotations

import io
from datetime import datetime

import pandas as pd

NY = "America/New_York"
INK, INK2, INK3, RULE = "#16181d", "#4b5160", "#8a90a0", "#d9dce3"
GOOD, BAD = "#15803d", "#dc2626"
ROWS_PER_PAGE = 36
ROWS_FIRST_PAGE = 22                 # under the total and the curve
_WHY = {"TP1": "objetivo", "TP2": "objetivo", "STOP": "stop", "EOD": "cierre 16:55",
        "INVALIDATION": "invalidado", "objetivo": "objetivo", "stop": "stop", "BE+2": "BE+2"}


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.0f}"


def _tone(v: float) -> str:
    return GOOD if v > 0 else BAD if v < 0 else INK2


def _ny(ts) -> pd.Timestamp | None:
    if not ts:
        return None
    t = pd.Timestamp(ts)
    return t.tz_localize(NY) if t.tzinfo is None else t.tz_convert(NY)


def _night(t: dict) -> bool:
    """Opened outside 09:30–16:00 ET."""
    o = _ny(t.get("opened_at"))
    return o is not None and not ("09:30" <= o.strftime("%H:%M") < "16:00")


def setups_of(log: dict, only: int | None = None) -> list[tuple[dict, list[dict]]]:
    """[(source, its trades)] in set up order. A bot counts only the version
    it uses now, like the dashboard."""
    out = []
    for so in sorted((s for s in log.get("sources", []) if s.get("setup")),
                     key=lambda s: s["setup"]):
        if only is not None and so["setup"] != only:
            continue
        mine = [t for t in log.get("trades", []) if t["src"] == so["id"]
                and (so.get("kind") != "main" or t.get("version") == so.get("current"))]
        out.append((so, sorted(mine, key=lambda t: t.get("closed_at") or "")))
    return out


def _verdict(fo: dict | None) -> str:
    if not fo or not fo.get("n"):
        return "—"
    if not fo.get("fits"):
        return f"No sirve para {fo['account']}"
    return f"{fo['account']}: faltan trades" if fo.get("few") else f"Sirve para {fo['account']}"


def _page(pdf, draw) -> None:
    from matplotlib.figure import Figure
    fig = Figure(figsize=(8.27, 11.69))          # A4
    draw(fig)
    pdf.savefig(fig)


def _head(fig, title: str, sub: str) -> None:
    fig.text(0.07, 0.95, title, fontsize=17, weight="bold", color=INK)
    fig.text(0.07, 0.928, sub, fontsize=8.5, color=INK3)


def _foot(fig, page: int, made: str) -> None:
    fig.text(0.07, 0.03, f"Trading simulado en papel — no es consejo financiero · {made}",
             fontsize=7, color=INK3)
    fig.text(0.93, 0.03, str(page), fontsize=7, color=INK3, ha="right")


def build_pdf(log: dict, only: int | None = None, now=None) -> bytes:
    import matplotlib
    with matplotlib.rc_context({"text.parse_math": False}):     # "$" is money, not math
        return _build(log, only, now)


def _build(log: dict, only: int | None, now) -> bytes:
    from matplotlib.backends.backend_pdf import PdfPages
    now = _ny(now or datetime.now().astimezone())
    made = f"generado el {now:%d-%m-%Y %H:%M} ET"
    sus = setups_of(log, only)
    buf = io.BytesIO()
    page = [0]

    def nxt():
        page[0] += 1
        return page[0]

    with PdfPages(buf, metadata={"Title": "Récord de los set ups", "Author": "GHOST"}) as pdf:
        # 1 · every set up on its own line
        def cover(fig):
            _head(fig, "Récord de los set ups",
                  "Cada set up por separado · en papel · dólares con comisiones")
            y = 0.87
            for col, x, ha in (("Set up", 0.07, "left"), ("Total", 0.55, "right"),
                               ("Trades", 0.64, "right"), ("Ganó", 0.71, "right"),
                               ("Cuenta de fondeo", 0.75, "left")):
                fig.text(x, y, col, fontsize=8, color=INK3, weight="bold", ha=ha)
            for so, T in sus:
                y -= 0.034
                v = sum(t.get("net_usd") or 0.0 for t in T)
                name = so.get("setup_label") or so.get("name") or so["id"]
                off = so.get("kind") != "main" and so.get("enabled") is False
                fig.text(0.07, y, name + ("  (apagado)" if off else ""), fontsize=10, color=INK)
                fig.text(0.55, y, _usd(v) if T else "—", fontsize=10, ha="right",
                         color=_tone(v) if T else INK3, weight="bold")
                fig.text(0.64, y, str(len(T)), fontsize=10, ha="right", color=INK2)
                wins = sum(1 for t in T if (t.get("net_usd") or 0) > 0)
                fig.text(0.71, y, str(wins) if T else "—", fontsize=10, ha="right", color=INK2)
                fig.text(0.75, y, _verdict(so.get("fondeo")), fontsize=8.5, color=INK2)
                fig.add_artist(_hline(fig, y - 0.011))
            _foot(fig, nxt(), made)
        if only is None:
            _page(pdf, cover)

        # 2 · each set up: total, curve and every trade
        for so, T in sus:
            name = so.get("setup_label") or so.get("name") or so["id"]
            v = sum(t.get("net_usd") or 0.0 for t in T)
            wins = sum(1 for t in T if (t.get("net_usd") or 0) > 0)
            first_n = ROWS_FIRST_PAGE if len(T) > 1 else ROWS_PER_PAGE
            chunks = [T[:first_n]] + [T[i:i + ROWS_PER_PAGE]
                                      for i in range(first_n, len(T), ROWS_PER_PAGE)]
            for ci, rows in enumerate(chunks):
                def draw(fig, rows=rows, first=ci == 0):
                    pos = so.get("position") or ""
                    _head(fig, name, f"{so.get('bot', '')} · en papel"
                          + (f" · {pos}" if pos else "") + " · dólares con comisiones"
                          + ("" if first else " · sigue"))
                    top = 0.89
                    if first:
                        fig.text(0.07, 0.865, _usd(v) if T else "$0", fontsize=30, weight="bold",
                                 color=_tone(v) if T else INK3)
                        fig.text(0.07, 0.84, (f"{len(T)} trades · ganó {wins}" if T
                                              else "Todavía sin trades cerrados")
                                 + f" · {_verdict(so.get('fondeo'))}", fontsize=10, color=INK2)
                        night = [t for t in T if _night(t)]
                        if night and len(night) < len(T):
                            day = [t for t in T if not _night(t)]
                            sd = sum(t.get("net_usd") or 0.0 for t in day)
                            sn = sum(t.get("net_usd") or 0.0 for t in night)
                            fig.text(0.07, 0.82, f"De día (9:30–16:00): {len(day)} trades {_usd(sd)}"
                                     f"   ·   De noche: {len(night)} trades {_usd(sn)}",
                                     fontsize=9, color=INK2)
                        if len(T) > 1:
                            _curve(fig, T)
                            top = 0.56
                        else:
                            top = 0.79
                    _table(fig, rows, top)
                    _foot(fig, nxt(), made)
                _page(pdf, draw)
    return buf.getvalue()


def _hline(fig, y: float):
    from matplotlib.lines import Line2D
    return Line2D([0.07, 0.93], [y, y], transform=fig.transFigure, color=RULE, linewidth=0.6)


def _curve(fig, T: list[dict]) -> None:
    """The set up's money trade after trade (one line, one axis)."""
    ax = fig.add_axes([0.11, 0.61, 0.72, 0.19])
    eq, run = [0.0], 0.0
    for t in T:
        run += t.get("net_usd") or 0.0
        eq.append(run)
    x = list(range(len(eq)))
    color = GOOD if run >= 0 else BAD
    ax.axhline(0, color=INK3, linewidth=0.8, linestyle=(0, (3, 3)))
    ax.fill_between(x, eq, 0, color=color, alpha=0.08, linewidth=0)
    ax.plot(x, eq, color=color, linewidth=2, solid_capstyle="round")
    ax.set_xlim(0, len(eq) - 1)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(RULE)
    ax.tick_params(colors=INK3, labelsize=7, length=0)
    ax.grid(axis="y", color=RULE, linewidth=0.5)
    ax.yaxis.set_major_formatter(lambda v, _p: _usd(v))
    ax.set_xlabel("trades", fontsize=7, color=INK3)
    ax.text(len(eq) - 1, run, f" {_usd(run)}", fontsize=8, color=color, weight="bold", va="center")


def _table(fig, rows: list[dict], top: float) -> None:
    cols = (("Fecha", 0.07, "left"), ("Hora ET", 0.20, "left"), ("Lado", 0.31, "left"),
            ("Entrada → salida", 0.40, "left"), ("Salida", 0.66, "left"),
            ("Pts", 0.80, "right"), ("$", 0.93, "right"))
    for col, x, ha in cols:
        fig.text(x, top, col, fontsize=7.5, color=INK3, weight="bold", ha=ha)
    fig.add_artist(_hline(fig, top - 0.008))
    y = top
    for t in rows:
        y -= 0.0215
        o = _ny(t.get("opened_at"))
        usd = t.get("net_usd") or 0.0
        pts = t.get("pts")
        cells = (
            (t.get("day") or "", INK2),
            (f"{o:%H:%M}" if o is not None else "—", INK2),
            ("COMPRA" if t.get("side") == "LONG" else "VENTA", INK),
            (f"{_n(t.get('entry'))} → {_n(t.get('exit'))}", INK),
            (_WHY.get(t.get("why") or "", (t.get("why") or "—").lower()), INK2),
            (f"{pts:+.2f}" if pts is not None else "—", INK2),
            (_usd(usd), _tone(usd)),
        )
        for (text, color), (_c, x, ha) in zip(cells, cols):
            fig.text(x, y, text, fontsize=8, color=color, ha=ha,
                     weight="bold" if color in (GOOD, BAD) else "normal")
    if not rows:
        fig.text(0.07, top - 0.03, "Todavía sin trades cerrados.", fontsize=9, color=INK3)


def _n(v) -> str:
    return f"{float(v):,.2f}" if v is not None else "—"
