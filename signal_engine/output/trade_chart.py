"""Small candlestick PNG of one trade: 1m candles with the zone, entry, stop
and targets drawn, and where the trade was closed. Attached to Discord
messages so a result can be checked by eye. Rendering only — no trading logic.
"""
from __future__ import annotations

import io

import pandas as pd


def render_trade_png(bars: pd.DataFrame, trade: dict, *, title: str = "",
                     marks: list[tuple[pd.Timestamp, float, str, str]] | None = None) -> bytes:
    """`bars`: OHLC frame (DatetimeIndex) covering the trade. `trade`: a
    PaperTrade dict (entry/stop/tp1/tp2, context zone). `marks`: extra
    (time, price, label, color) points, e.g. the old and the corrected exit."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bg, fg, grid = "#0e1117", "#d0d4dc", "#232833"
    up, down = "#26a69a", "#ef5350"
    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    xs = range(len(bars))
    for x, (_, b) in zip(xs, bars.iterrows()):
        c = up if b["close"] >= b["open"] else down
        ax.vlines(x, b["low"], b["high"], color=c, linewidth=0.8)
        lo, hi = sorted((b["open"], b["close"]))
        ax.add_patch(plt.Rectangle((x - 0.35, lo), 0.7, max(hi - lo, 0.25),
                                   color=c, linewidth=0))

    ctx = trade.get("context") or {}
    if ctx.get("zone_bottom") is not None and ctx.get("zone_top") is not None:
        ax.axhspan(ctx["zone_bottom"], ctx["zone_top"], color="#f1c40f", alpha=0.12)
    levels = [("Entrada", trade.get("entry"), "#bbbbbb"), ("SL", trade.get("stop_initial")
              or trade.get("stop"), down), ("TP1", trade.get("tp1"), up),
              ("TP", trade.get("tp2"), up)]
    for name, price, color in levels:
        if price is None:
            continue
        ax.axhline(price, color=color, linewidth=0.9, linestyle="--", alpha=0.8)
        ax.text(len(bars) - 0.5, price, f" {name} {price:,.2f}", color=color,
                fontsize=8, va="center", ha="left")

    def _x(ts):
        return int(bars.index.searchsorted(ts, side="left"))
    for ts, price, label, color in marks or []:
        ax.scatter([_x(ts)], [price], s=60, color=color, zorder=5, edgecolors="white")
        ax.annotate(label, (_x(ts), price), textcoords="offset points", xytext=(6, 8),
                    color=color, fontsize=8)

    step = max(1, len(bars) // 8)
    ax.set_xticks(list(xs)[::step])
    ax.set_xticklabels([t.strftime("%H:%M") for t in bars.index[::step]], color=fg, fontsize=8)
    ax.tick_params(colors=fg, labelsize=8)
    ax.yaxis.tick_right()
    ax.grid(color=grid, linewidth=0.6)
    for s in ax.spines.values():
        s.set_color(grid)
    ax.set_xlim(-1, len(bars) + 10)
    if title:
        ax.set_title(title, color=fg, fontsize=10, loc="left")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()
