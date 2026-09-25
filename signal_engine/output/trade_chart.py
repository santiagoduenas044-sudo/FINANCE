"""Small candlestick PNG of one trade, drawn like TradingView's long/short
position tool: a green reward box (entry -> final target) and a red risk box
(entry -> stop) spanning the time the trade was open, TP1 inside the reward
box, and a dashed line from the entry to the exit. Attached to Discord
messages so a result can be checked by eye. Rendering only — no trading logic.
"""
from __future__ import annotations

import io

import pandas as pd

_BG, _FG, _GRID = "#0e1117", "#d0d4dc", "#232833"
_UP, _DOWN = "#26a69a", "#ef5350"
_REWARD, _RISK = "#089981", "#f23645"


def render_trade_png(bars: pd.DataFrame, trade: dict, *, title: str = "",
                     marks: list[tuple[pd.Timestamp, str, str]] | None = None) -> bytes:
    """`bars`: OHLC frame (DatetimeIndex) covering the trade. `trade`: a
    PaperTrade dict (entry, stop_initial/stop, tp1, tp2, opened_at, closed_at,
    exit_price, pnl_points, r_multiple, context zone). `marks`: extra
    (time, label, color) moments drawn as a dotted vertical line, e.g. where
    the trade used to be closed before a correction."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=110)
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    n = len(bars)

    def x_of(ts) -> int:
        return min(int(bars.index.searchsorted(pd.Timestamp(ts), side="left")), n - 1)

    # --- zone band (behind everything) --------------------------------- #
    ctx = trade.get("context") or {}
    if ctx.get("zone_bottom") is not None and ctx.get("zone_top") is not None:
        ax.axhspan(ctx["zone_bottom"], ctx["zone_top"], color="#f1c40f", alpha=0.08, zorder=0)

    # --- position boxes ------------------------------------------------- #
    entry = trade.get("entry")
    stop = trade.get("stop_initial") or trade.get("stop")
    tp1, final = trade.get("tp1"), trade.get("tp2") or trade.get("tp1")
    x0 = x_of(trade["opened_at"]) if trade.get("opened_at") else 0
    x1 = x_of(trade["closed_at"]) if trade.get("closed_at") else n - 1
    x1 = max(x1, x0 + 1)
    width = x1 - x0
    lo, hi = bars["low"].min(), bars["high"].max()
    if entry is not None and stop is not None and final is not None:
        risk = abs(entry - stop) or 1e-9
        for a, b, color in ((entry, final, _REWARD), (entry, stop, _RISK)):
            ax.add_patch(Rectangle((x0, min(a, b)), width, abs(b - a), facecolor=color,
                                   alpha=0.22, edgecolor=color, linewidth=0.8, zorder=1))
        ax.hlines(entry, x0, x1, color="#b2b5be", linewidth=1.0, zorder=3)
        if tp1 is not None and tp1 != final:
            ax.hlines(tp1, x0, x1, color=_REWARD, linewidth=0.9, linestyles="--", zorder=3)
            ax.text(x0 + 0.5, tp1, f" TP1 {tp1:,.2f}", color=_REWARD, fontsize=7,
                    va="bottom", zorder=4)
        up = final > entry
        ax.text(x0 + 0.5, final, f" TP {final:,.2f} · {abs(final - entry):.1f} pts "
                f"({abs(final - entry) / risk:.1f}R)", color=_REWARD, fontsize=7.5,
                va="top" if up else "bottom", zorder=4)
        ax.text(x0 + 0.5, stop, f" SL {stop:,.2f} · {risk:.1f} pts", color=_RISK,
                fontsize=7.5, va="bottom" if up else "top", zorder=4)
        ax.text(x0 + 0.5, entry, f" Entrada {entry:,.2f}", color="#b2b5be", fontsize=7.5,
                va="bottom" if not up else "top", zorder=4,
                bbox={"boxstyle": "round,pad=0.2", "facecolor": _BG, "edgecolor": "none",
                      "alpha": 0.7})
        lo, hi = min(lo, stop, final), max(hi, stop, final)

    # --- candles --------------------------------------------------------- #
    for x, (_, b) in enumerate(bars.iterrows()):
        c = _UP if b["close"] >= b["open"] else _DOWN
        ax.vlines(x, b["low"], b["high"], color=c, linewidth=0.8, zorder=2)
        o, cl = sorted((b["open"], b["close"]))
        ax.add_patch(Rectangle((x - 0.35, o), 0.7, max(cl - o, 0.25), color=c,
                               linewidth=0, zorder=2))

    # --- entry -> exit and the result ----------------------------------- #
    exit_px, pts = trade.get("exit_price"), trade.get("pnl_points")
    if entry is not None and exit_px is not None:
        color = _REWARD if (pts or 0) >= 0 else _RISK
        ax.plot([x0, x1], [entry, exit_px], color=color, linestyle="--", linewidth=1.2,
                zorder=4)
        if pts is not None:
            r = trade.get("r_multiple")
            ax.text(x1, exit_px, f" {pts:+.2f} pts" + (f" ({r:+.2f}R)" if r is not None else ""),
                    color="white", fontsize=8, va="center", zorder=5,
                    bbox={"boxstyle": "round,pad=0.3", "facecolor": color,
                          "edgecolor": "none"})

    for ts, label, color in marks or []:
        x = x_of(ts)
        ax.axvline(x, color=color, linestyle=":", linewidth=1.0, zorder=3)
        ax.text(x, hi, f" {label}", color=color, fontsize=7.5, va="top", zorder=5)

    step = max(1, n // 8)
    ax.set_xticks(list(range(n))[::step])
    ax.set_xticklabels([t.strftime("%H:%M") for t in bars.index[::step]], color=_FG, fontsize=8)
    ax.tick_params(colors=_FG, labelsize=8)
    ax.yaxis.tick_right()
    ax.grid(color=_GRID, linewidth=0.6, zorder=0)
    for s in ax.spines.values():
        s.set_color(_GRID)
    pad = (hi - lo) * 0.06 or 1.0
    ax.set_ylim(lo - pad, hi + pad)
    ax.set_xlim(-1, n + 6)
    if title:
        ax.set_title(title, color=_FG, fontsize=10, loc="left")
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", facecolor=_BG)
    plt.close(fig)
    return buf.getvalue()
