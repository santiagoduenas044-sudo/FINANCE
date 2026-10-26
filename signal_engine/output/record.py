"""The trading record in plain dollars, for Discord (daily message and the
!record command): today, this week, everything since the record started,
and the latest trades one line each. Pure formatting over closed paper
trades — never changes anything.
"""
from __future__ import annotations

import pandas as pd

_NY = "America/New_York"
_EXIT = {"TP1": "objetivo", "TP2": "objetivo", "EOD": "cierre 16:55",
         "INVALIDATION": "invalidado", "CANCELLED": "cancelado", "EXPIRED": "cierre por tiempo"}


def _ts(v) -> pd.Timestamp | None:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize(_NY) if t.tzinfo is None else t.tz_convert(_NY)


def trading_day(ts) -> pd.Timestamp:
    """CME trading day (the 18:00 ET open counts as the next date)."""
    return (_ts(ts) + pd.Timedelta(hours=6)).normalize().tz_localize(None)


def usd(pts: float, pv: float) -> str:
    return f"{'+' if pts >= 0 else '−'}${abs(pts * pv):,.0f}"


def exit_label(t) -> str:
    reason = t.exit_reason.value if t.exit_reason else ""
    if reason == "STOP":
        if t.pnl_points > 0.01:
            return "BE+" if t.pnl_points <= 5 else "stop en ganancia"
        if abs(t.pnl_points) <= 0.01:
            return "breakeven"
        return "stop"
    return _EXIT.get(reason, reason.lower() or "cerrado")


def _n(k: int) -> str:
    return f"{k} {'trade' if k == 1 else 'trades'}"


def _mark(pts: float) -> str:
    return "✅" if pts > 0.01 else ("➖" if pts >= -0.01 else "🛑")


# Tradovate round-trip commission per contract (per side x 2): 1 NQ = $1.55 x 2.
ROUND_TRIP_USD = {"MNQ": 1.04, "MES": 1.04, "NQ": 3.10, "ES": 3.10}


def commission_per_trade(position: str) -> float:
    """Commission of one trade (in and out) for a position label like
    '5 MNQ' -> 5 x $1.04 = $5.20. 0 when the label is not a position."""
    parts = str(position).split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in ROUND_TRIP_USD:
        return int(parts[0]) * ROUND_TRIP_USD[parts[1]]
    return 0.0


def after_fees(pts: list[float], pv: float, fee: float) -> str:
    """' · con comisiones +$874' (what Tradovate would show), or '' without a fee."""
    if not fee or not pts:
        return ""
    net = sum(pts) * pv - fee * len(pts)
    return f" · con comisiones {'+' if net >= 0 else '−'}${abs(net):,.0f}"


def won_lost(pts: list[float], pv: float) -> str:
    """'ganado +$1,200 · perdido −$300 · neto +$900' in the position's dollars."""
    won = sum(p for p in pts if p > 0) * pv
    lost = sum(p for p in pts if p < 0) * pv
    net = won + lost
    return (f"ganado +${won:,.0f} · perdido {'−' if lost else ''}${abs(lost):,.0f} · "
            f"**neto {'+' if net >= 0 else '−'}${abs(net):,.0f}**")


def _line(label: str, trades: list, pv: float, fee: float = 0.0) -> str:
    if not trades:
        return f"**{label}:** sin trades"
    pts = [t.pnl_points for t in trades]
    w = sum(1 for p in pts if p > 0.01)
    flat = sum(1 for p in pts if abs(p) <= 0.01)
    return (f"**{label}:** {_n(len(pts))} · {w} ✅ {flat} ➖ {len(pts) - w - flat} 🛑 · "
            f"{won_lost(pts, pv)}{after_fees(pts, pv, fee)} ({sum(pts):+.1f} pts)")


def build_record(closed: list, now, pv: float, position: str, last: int = 8,
                 version_of=None, current: str | None = None, title: str | None = None,
                 extras: list[str] | None = None, who: str | None = None,
                 older_label: str = "Versión anterior {names}, aparte") -> str:
    """Markdown for Discord. `closed`: closed PaperTrades; `pv`: $ per point
    of the position the operator trades (5 MNQ = 10).

    With `version_of` (trade -> "v4") and `current` ("v4"), today / this week
    / total count ONLY the version running now (as the dashboard does); the
    older versions get one line of their own, so their trades never inflate
    the current strategy's record. `title` names it ("Set up 1 · FVG 15"),
    `who` says which bot runs it ("GHOST v4"); `extras` are lines added
    before the last trades (every set up's day)."""
    trades = sorted((t for t in closed if t.closed_at is not None), key=lambda t: _ts(t.closed_at))
    older: list = []
    if version_of is not None and current is not None:
        older = [t for t in trades if version_of(t) != current]
        trades = [t for t in trades if version_of(t) == current]
    day = trading_day(now)
    week0 = day - pd.Timedelta(days=day.dayofweek)
    today = [t for t in trades if trading_day(t.closed_at) == day]
    week = [t for t in trades if trading_day(t.closed_at) >= week0]
    fee = commission_per_trade(position)
    unit = (f"(${pv:g} por punto, simulado"
            + (f", comisión ${fee:,.2f} por trade" if fee else "") + ")")
    head = (f"📒 **{title}**" + (f" · {who}" if who else "") + f" · {position} {unit}"
            if title else f"📒 **Récord · {position}** {unit}")
    lines = [head,
             _line(f"Hoy {day:%d-%b}", today, pv, fee), _line("Esta semana", week, pv, fee)]
    if trades:
        pts = [t.pnl_points for t in trades]
        wins = sum(1 for p in pts if p > 0.01)
        first = trading_day(trades[0].closed_at)
        lines.append(f"**Total desde {first:%d-%b}:** {_n(len(pts))} · gana {wins / len(pts):.0%} · "
                     f"{won_lost(pts, pv)}{after_fees(pts, pv, fee)}")
    for name in sorted({version_of(t) for t in older}):
        opts = [t.pnl_points for t in older if version_of(t) == name]     # each version on its own
        lines.append(f"_{older_label.format(names=name)}:_ {_n(len(opts))} · "
                     f"neto {usd(sum(opts), pv)}{after_fees(opts, pv, fee)}")
    if extras:
        lines.append("")
        lines.extend(extras)
    if trades:
        lines.append("")
        lines.append("**Últimos trades" + (f" · {title.split(' · ')[0]}" if title else "") + "**")
        for t in trades[-last:][::-1]:
            num = (t.context or {}).get("idea_number")
            side = "COMPRA" if t.direction == "LONG" else "VENTA"
            when = _ts(t.closed_at)
            lines.append(f"{_mark(t.pnl_points)} {when:%d-%b %H:%M} · "
                         f"{'#' + str(num) + ' ' if num is not None else ''}{side} "
                         f"{t.entry:,.2f} → {(t.exit_price or 0):,.2f} · {exit_label(t)} · "
                         f"**{usd(t.pnl_points, pv)}**")
    else:
        lines.append("_todavía no hay trades cerrados_" if not older else
                     "_esta versión todavía no tiene trades cerrados_")
    return "\n".join(lines)


def variant_lines(shadows, now, pv: float, position: str) -> list[str]:
    """One line per parallel set up switched on: what it did today (paper
    only) and its total, so the day's message
    compares them with the main version."""
    fee = commission_per_trade(position)
    day = trading_day(now)
    out = []
    for spec, rt in shadows or []:
        tr = getattr(rt, "paper_tracker", None)
        if tr is None:
            continue
        closed = [t for t in tr.closed_trades if t.closed_at is not None]
        today = [t.pnl_points for t in closed if trading_day(t.closed_at) == day]
        allp = [t.pnl_points for t in closed]
        name = getattr(spec, "label", None) or str(getattr(spec, "name", spec)).split(" · ")[0]
        now_txt = f"hoy {_n(len(today))} {usd(sum(today), pv)}" if today else "hoy sin trades"
        tot = f" · total {_n(len(allp))} {usd(sum(allp), pv)}" if allp else ""
        net = after_fees(allp, pv, fee) if allp else ""
        out.append(f"∥ **{name}**: {now_txt}{tot}{net}")
    if out:
        out.insert(0, "**Los otros set ups** (en papel, mismo mercado)")
    return out


def _setup1_record(runtime, now, symbol: str = "NQ") -> str:
    """Set up 1's own record (GHOST's strategy in use) with its parallel set
    ups: the fallback when the full day message cannot be built."""
    from ..runtime.engine import position_usd_per_point
    from ..versions import current_version, ghost_version_of, main_setup_label
    try:
        pv = runtime._point_values(symbol)
    except Exception:  # noqa: BLE001
        pv = position_usd_per_point(runtime.registry, runtime.data_engine.cfg.ghost, symbol)
    label, per_pt = next(iter(pv.items())) if pv else (f"1 punto {symbol}", 1.0)
    cur = current_version("GHOST")
    tracker = getattr(runtime, "paper_tracker", None)
    extras = variant_lines(getattr(runtime, "shadows", None), now, per_pt, label)
    return build_record(list(tracker.closed_trades) if tracker is not None else [], now,
                        per_pt, label, version_of=ghost_version_of, current=cur,
                        title=main_setup_label("GHOST"), who=f"GHOST {cur}",
                        older_label="GHOST {names} (versión anterior)",
                        extras=extras)


# --------------------------------------------------------------------------- #
# The day's message: every set up, grouped by bot, saying plainly whether it
# won or lost (from api/bots.trade_log: every closed trade of every set up).
# --------------------------------------------------------------------------- #
def _money(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.0f}"


def _verdict(v: float, he: bool = True) -> str:
    """GANÓ +$290 / PERDIÓ −$350 / QUEDÓ EN $0 (GANARON… for `he=False`)."""
    if abs(v) < 0.5:
        return "QUEDÓ EN $0" if he else "QUEDARON EN $0"
    word = ("GANÓ" if v > 0 else "PERDIÓ") if he else ("GANARON" if v > 0 else "PERDIERON")
    return f"{word} {_money(v)}"


def day_message(log: dict, now) -> str:
    """Discord text of the day's close. `log`: api/bots.trade_log(...).
    Every set up on its own (each one is a different strategy: they are never
    added together), with today's trades under it. Dollars after commissions;
    Set up 1 and 2 count only the version each bot uses now."""
    now = pd.Timestamp(now)
    now = now.tz_localize("America/New_York") if now.tzinfo is None else \
        now.tz_convert("America/New_York")
    day = now.date().isoformat()
    trades = log.get("trades") or []
    setups = []
    for so in log.get("sources") or []:
        if not so.get("setup"):
            continue
        mine = [t for t in trades if t["src"] == so["id"]
                and (so.get("kind") != "main" or t.get("version") == so.get("current"))]
        setups.append({**so, "label": so.get("setup_label") or so["name"], "trades": mine,
                       "on": so.get("kind") == "main" or so.get("enabled", True)})
    setups.sort(key=lambda s: s["setup"])

    lines = ["Cada set up por separado · en papel · dólares con comisiones", ""]
    for s in setups:                                  # one line each, in number order
        allv = sum(t["net_usd"] for t in s["trades"])
        total = f" · total {_money(allv)}" if s["trades"] else ""
        if not s["on"]:
            lines.append(f"⚪ {s['label']} — apagado")
            continue
        today = [t for t in s["trades"] if t["day"] == day]
        if not today:
            lines.append(f"➖ **{s['label']}** — hoy sin trades{total}")
            continue
        v = sum(t["net_usd"] for t in today)
        mark = "✅" if v >= 0.5 else ("🛑" if v <= -0.5 else "➖")
        lines.append(f"{mark} **{s['label']}** — hoy **{_verdict(v)}** ({_n(len(today))}){total}")
    return "\n".join(lines)


def day_record(runtime, now, symbol: str = "NQ") -> str:
    """The day's message for GHOST's process (16:55 and !record). Falls back
    to Set up 1's own record if the other set ups cannot be read."""
    try:
        from ..api.bots import trade_log
        return day_message(trade_log(runtime), now)
    except Exception:  # noqa: BLE001
        return _setup1_record(runtime, now, symbol)


# --------------------------------------------------------------------------- #
# The day's message, option C (26-oct): a bar per set up that traded today,
# the count of who won and lost, and every copy against its original.
# --------------------------------------------------------------------------- #
def _running(log: dict) -> list[dict]:
    out = []
    for so in log.get("sources") or []:
        if not so.get("setup") or so.get("retired"):
            continue
        if so.get("kind") != "main" and not so.get("enabled", True):
            continue
        out.append({**so, "label": so.get("setup_label") or so["name"]})
    return sorted(out, key=lambda s: s["setup"])


def _short(label: str) -> str:
    """'Set up 16 · FVG 15 FILTROS' -> '16 FVG 15 FILTROS'."""
    return label.replace("Set up ", "").replace(" · ", " ", 1)


def day_bars_png(rows: list[tuple[str, float]], title: str) -> bytes | None:
    """Horizontal bars, green won / red lost, one per set up. None without
    matplotlib or rows (the message still goes)."""
    if not rows:
        return None
    try:
        import io

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001
        return None
    bg, fg, grid = "#0e1117", "#d0d4dc", "#232833"
    rows = sorted(rows, key=lambda r: r[1])
    fig, ax = plt.subplots(figsize=(8, 0.42 * len(rows) + 1.2), dpi=110)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    vals = [v for _, v in rows]
    ax.barh(range(len(rows)), vals, color=["#26a69a" if v >= 0 else "#ef5350" for v in vals], height=0.62)
    ax.set_yticks(range(len(rows)), [n for n, _ in rows], color=fg, fontsize=9)
    ax.axvline(0, color="#6b7280", linewidth=1)
    span = max(abs(v) for v in vals) or 1.0
    for i, v in enumerate(vals):
        ax.text(v + (span * 0.02 if v >= 0 else -span * 0.02), i,
                f"{'+' if v >= 0 else '−'}${abs(v):,.0f}", va="center",
                ha="left" if v >= 0 else "right", color="#26a69a" if v >= 0 else "#ef5350",
                fontsize=9, fontweight="bold")
    ax.set_xlim(-span * 1.35 if min(vals) < 0 else -span * 0.05, span * 1.35 if max(vals) > 0 else span * 0.05)
    ax.tick_params(axis="x", colors=fg, labelsize=8)
    ax.xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(
        lambda x, _p: f"{'−' if x < 0 else ''}${abs(x):,.0f}"))
    ax.set_axisbelow(True)
    ax.grid(axis="x", color=grid, linewidth=0.6)
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_title(title, color=fg, fontsize=11, loc="left")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


def day_summary(log: dict, now) -> tuple[str, bytes | None]:
    """(text, chart) of the day's close. Every set up on its own; dollars
    after commissions; Set up 1 counts only the version in use."""
    now = pd.Timestamp(now)
    now = now.tz_localize("America/New_York") if now.tzinfo is None else now.tz_convert("America/New_York")
    day = now.date().isoformat()
    trades = log.get("trades") or []
    setups = _running(log)

    def mine(so):
        return [t for t in trades if t["src"] == so["id"]
                and (so.get("kind") != "main" or t.get("version") == so.get("current"))]

    rows, won, lost, none = [], 0, 0, []
    by_n = {}
    for so in setups:
        T = mine(so)
        by_n[so["setup"]] = (so, T)
        today = [t for t in T if t["day"] == day]
        if not today:
            none.append(str(so["setup"]))
            continue
        v = sum(t["net_usd"] for t in today)
        rows.append((_short(so["label"]), v))
        won += v >= 0.5
        lost += v <= -0.5
    lines = []
    if rows:
        best = max(rows, key=lambda r: r[1])
        worst = min(rows, key=lambda r: r[1])
        lines.append(f"**{won} {'ganó' if won == 1 else 'ganaron'} · {lost} {'perdió' if lost == 1 else 'perdieron'}"
                     f" · {len(none)} sin trades**")
        lines.append(f"🏆 Mejor: {best[0]} {_money(best[1])}" + (f" · 💀 Peor: {worst[0]} {_money(worst[1])}"
                                                               if worst[1] < -0.5 else ""))
    else:
        lines.append("**Hoy ningún set up operó.**")
    pairs = []
    for so in setups:
        vs = so.get("vs")
        if not vs or vs not in by_n:
            continue
        T = by_n[so["setup"]][1]
        if not T:
            continue
        since = min(t["day"] for t in T)                       # the same days for both
        orig = [t for t in by_n[vs][1] if t["day"] >= since]
        d = sum(t["net_usd"] for t in T) - sum(t["net_usd"] for t in orig)
        word = "mejor" if d > 0.5 else ("peor" if d < -0.5 else "igual")
        pairs.append(f"{_short(so['label'])} vs {vs} → {_money(d)} {word}")
    if pairs:
        lines += ["", "**Parejas** (copia contra original, desde que existe la copia)"] + pairs
    totals = [f"{so['setup']} {_money(sum(t['net_usd'] for t in T))}"
              for so, T in (by_n[n] for n in sorted(by_n)) if T]
    if totals:
        lines += ["", "**Total de cada uno:** " + " · ".join(totals)]
    lines += ["", "_Cada set up por separado · en papel · dólares con comisiones_"]
    png = day_bars_png(rows, f"Resultado de hoy por set up · {now:%d-%m}")
    return "\n".join(lines), png
