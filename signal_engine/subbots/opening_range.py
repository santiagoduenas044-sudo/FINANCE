"""The APERTURA strategy (opening-range breakout), run bar by bar. ORBIT
(subbots/orbit.py) is the separate paper bot that runs it next to GHOST.

The rules are the ones the strategy lab validated on 5 years of NQ
(scripts/strategy_lab.py --estudio apertura), applied bar by bar:

  1. The box: high and low of 09:30-09:45 ET (1-minute bars).
  2. The entry: the first 5-minute candle that CLOSES outside the box (before
     15:30), in that direction, at the open of the next minute. One trade a
     day; a box wider than 150 pts means no trade that day.
  3. The stop: the other side of the box. The target: 1.5x the stop.
  4. Once price has covered 70% of the way to the target, the stop moves to
     entry + 2 pts (from the next minute on).
  5. Stop first if stop and target fall in the same minute; a stop the
     market gaps through fills at the worse open; flat at 16:55.

It never touches GHOST, its trades or its limits. Paper only: results are
in points and in dollars for `contracts` MNQ after commission + slippage.
State lives in data_store/subbots/ so a restart resumes the day.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import pandas as pd

log = logging.getLogger("signal_engine.subbots.opening_range")

TZ = "America/New_York"
TICK = 0.25


@dataclass
class OrbSettings:
    range_minutes: int = 15
    target_mult: float = 1.5
    be_at: float = 0.7
    be_plus: float = 2.0
    last_entry: str = "15:30"          # the breakout candle must close by then
    flat_at: str = "16:55"
    max_range: float = 150.0
    contracts: int = 1                 # MNQ
    usd_per_point: float = 2.0         # per MNQ
    cost_points: float = 1.02          # commission + 1 tick of slippage per side
    stale_minutes: int = 15            # a breakout older than this is not chased


@dataclass
class DayState:
    day: str = ""
    hi: float | None = None
    lo: float | None = None
    bars: int = 0
    box_sent: bool = False
    done: bool = False                 # the day's breakout (or no-trade) is decided
    pending: dict | None = None        # breakout seen, enters on the next minute
    trade: dict | None = None
    last_ts: str | None = None


def _hm(ts: pd.Timestamp) -> str:
    return ts.strftime("%H:%M")


def _add(hhmm: str, minutes: int) -> str:
    t = pd.Timestamp("2000-01-01 " + hhmm) + pd.Timedelta(minutes=minutes)
    return t.strftime("%H:%M")


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}${abs(x):,.0f}"


def _px(x: float) -> str:
    return f"{x:,.2f}"


class OpeningRangeBot:
    """Feed it the live 1-minute bars (CLOSE-labeled, ET) every cycle."""


    def __init__(self, store_dir: str | Path, *, settings: OrbSettings | None = None,
                 publish: Callable[..., None] | None = None,
                 chart: Callable[..., bytes | None] | None = None, name: str = "ORBIT"):
        self.name = name
        self.s = settings or OrbSettings()
        self.dir = Path(store_dir)
        self.state_file = self.dir / "apertura_state.json"
        self.trades_file = self.dir / "apertura_trades.jsonl"
        self.publish = publish or (lambda **kw: None)
        self.chart = chart
        self.st = self._load()
        self._fresh_start = True       # first call after a restart
        self.closed = self._load_trades()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> DayState:
        try:
            return DayState(**json.loads(self.state_file.read_text()))
        except (OSError, ValueError, TypeError):
            return DayState()

    def _save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(asdict(self.st)))
        except OSError as exc:
            log.warning("APERTURA state not saved (%s)", type(exc).__name__)

    def _load_trades(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.trades_file.read_text().splitlines()
                    if line.strip()]
        except (OSError, ValueError):
            return []

    def _append_trade(self, t: dict) -> None:
        self.closed.append(t)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.trades_file, "a") as fh:
                fh.write(json.dumps(t) + "\n")
        except OSError as exc:
            log.warning("APERTURA trade not saved (%s)", type(exc).__name__)

    # ------------------------------------------------------------------ #
    # the loop
    # ------------------------------------------------------------------ #
    def on_bars(self, m1: pd.DataFrame | None) -> None:
        if m1 is None or m1.empty:
            return
        idx = m1.index
        idx = idx.tz_localize(TZ) if idx.tz is None else idx.tz_convert(TZ)
        bars = m1.set_axis(idx)
        newest = idx[-1]
        last = pd.Timestamp(self.st.last_ts) if self.st.last_ts else None
        if self._fresh_start and (last is None or last.date() != newest.date()):
            # started mid-day with no memory of today: rebuild the box from
            # history, but never enter a breakout that already happened
            self._replay(bars[bars.index.date == newest.date()])
        self._fresh_start = False
        last = pd.Timestamp(self.st.last_ts) if self.st.last_ts else None
        todo = bars if last is None else bars[bars.index > last]
        for ts, bar in todo.iterrows():
            self._step(ts, bar, live=(newest - ts) <= pd.Timedelta(minutes=self.s.stale_minutes),
                       bars=bars)
        self._save()

    def _replay(self, day_bars: pd.DataFrame) -> None:
        for ts, bar in day_bars.iterrows():
            self._step(ts, bar, live=False, bars=day_bars, quiet=True)

    def _new_day(self, day: str) -> None:
        self.st = DayState(day=day)

    def _step(self, ts: pd.Timestamp, bar, *, live: bool, bars: pd.DataFrame,
              quiet: bool = False) -> None:
        s, st = self.s, self.st
        day = ts.date().isoformat()
        if day != st.day:
            if st.trade is not None:                # never carried overnight
                self._close(st.trade, float(bar["open"]), ts, "cierre (sin datos al cierre)",
                            bars, quiet)
            self._new_day(day)
            st = self.st
        st.last_ts = str(ts)
        hm = _hm(ts)
        box_end = _add("09:30", s.range_minutes)

        # 1) the box: 1-minute bars closing in (09:30, 09:45]
        if "09:30" < hm <= box_end:
            st.hi = float(bar["high"]) if st.hi is None else max(st.hi, float(bar["high"]))
            st.lo = float(bar["low"]) if st.lo is None else min(st.lo, float(bar["low"]))
            st.bars += 1
            if hm == box_end:
                self._box_ready(ts, bars, quiet)
            return
        # the feed skipped the 09:45 bar (Yahoo sometimes has gaps): close
        # the box on the first bar after it instead of staying silent all day
        if not st.box_sent and box_end < hm <= "16:00":
            self._box_ready(ts, bars, quiet)

        # 2) a breakout seen on the previous minute enters at this open
        if st.pending is not None:
            p, st.pending = st.pending, None
            if live:
                self._open(p, float(bar["open"]), ts, bars, quiet)
            else:
                st.done = True
                if not quiet:
                    self._note(f"⏭️ {self.name} — ruptura perdida",
                               "La ruptura pasó mientras el bot no tenía datos al día; "
                               "no la persigo. Mañana otra vez.", 0x95A5A6, f"missed:{day}")

        # 3) manage the open trade on this minute
        if st.trade is not None:
            self._manage(st.trade, ts, bar, bars, quiet)
            if st.trade is not None and hm >= s.flat_at:
                self._close(st.trade, float(bar["close"]), ts, "cierre 16:55", bars, quiet)
            return

        # 4) look for the breakout on 5-minute closes
        if st.done or st.hi is None or not st.box_sent:
            return
        if hm > s.last_entry:
            st.done = True
            if not quiet:
                self._note(f"😴 {self.name} — hoy no hubo ruptura",
                           f"Ninguna vela de 5 min cerró fuera de la caja "
                           f"({_px(st.lo)} – {_px(st.hi)}) antes de las {s.last_entry}. "
                           "Sin trade hoy.", 0x95A5A6, f"none:{day}")
            return
        if ts.minute % 5:
            return
        close = float(bar["close"])
        if close > st.hi or close < st.lo:
            long = close > st.hi
            st.done = True
            st.pending = {"long": long, "stop": st.lo if long else st.hi,
                          "signal_ts": str(ts), "signal_close": close,
                          "box_hi": st.hi, "box_lo": st.lo}

    def _box_ready(self, ts, bars, quiet: bool) -> None:
        s, st = self.s, self.st
        st.box_sent = True
        box_end = _add("09:30", s.range_minutes)
        when = f"{ts:%d-%b}"
        width = (st.hi - st.lo) if st.hi is not None else 0.0
        if st.bars < s.range_minutes * 0.8 or width <= 0:
            st.done = True
            msg = (f"📦 {self.name} — hoy no opera ({when})",
                   f"Faltan velas de 9:30 a {box_end} ({st.bars} de {s.range_minutes}); "
                   "sin caja completa no hay trade.")
        elif width > s.max_range:
            st.done = True
            msg = (f"📦 {self.name} — hoy no opera ({when})",
                   f"Caja de {width:.1f} pts ({_px(st.lo)} – {_px(st.hi)}): más de "
                   f"{s.max_range:.0f} pts, demasiado grande.")
        else:
            risk = (width + 5) * s.usd_per_point * s.contracts
            msg = (f"📦 {self.name} — caja lista ({when})",
                   f"**9:30–{box_end}:** máximo **{_px(st.hi)}** · mínimo **{_px(st.lo)}** "
                   f"· {width:.1f} pts\n"
                   f"🟢 **Compra** si una vela de 5 min cierra arriba de {_px(st.hi)}\n"
                   f"🔴 **Venta** si una vela de 5 min cierra abajo de {_px(st.lo)}\n"
                   f"Stop del otro lado de la caja (≈ {width:.0f} pts o algo más, "
                   f"≈ {_usd(-risk)} con {s.contracts} MNQ) · objetivo 1.5× el stop · "
                   f"hasta las {s.last_entry}.")
        if not quiet:
            self._note(*msg, 0x3498DB, f"box:{st.day}")

    def _open(self, p: dict, entry: float, ts, bars, quiet: bool) -> None:
        s = self.s
        long, stop = p["long"], p["stop"]
        risk = (entry - stop) if long else (stop - entry)
        if risk <= TICK:
            return
        target = entry + s.target_mult * risk if long else entry - s.target_mult * risk
        t = {"id": f"APERTURA-{self.st.day}", "day": self.st.day, "long": long,
             "entry": entry, "stop": stop, "stop_initial": stop, "target": target,
             "risk": risk, "be_done": False, "opened_at": str(ts),
             "box_hi": p["box_hi"], "box_lo": p["box_lo"],
             "signal_ts": p["signal_ts"]}
        self.st.trade = t
        if quiet:
            return
        pv = s.usd_per_point * s.contracts
        side = "COMPRA" if long else "VENTA"
        text = (f"La vela de 5 min de las {_hm(pd.Timestamp(p['signal_ts']))} cerró en "
                f"{_px(p['signal_close'])}, {'arriba' if long else 'abajo'} de la caja.\n"
                f"**Entrada** {_px(entry)} · **Stop** {_px(stop)} ({risk:.1f} pts, "
                f"{_usd(-risk * pv)}) · **Objetivo** {_px(target)} "
                f"({s.target_mult * risk:.1f} pts, {_usd(s.target_mult * risk * pv)})\n"
                f"Al {s.be_at:.0%} del camino el stop sube a entrada +{s.be_plus:g}. "
                f"Cierra a las {s.flat_at} si no toca nada. {s.contracts} MNQ, en papel.")
        self._note(f"{'🟢' if long else '🔴'} {self.name} — {side} NQ", text,
                   0x00C851 if long else 0xFF4444, f"open:{t['id']}",
                   image=self._chart(bars, t, f"{self.name} · APERTURA · {side} {_px(entry)}"))

    def _manage(self, t: dict, ts, bar, bars, quiet: bool) -> None:
        long = t["long"]
        h, l, o = float(bar["high"]), float(bar["low"]), float(bar["open"])
        stop, tgt = t["stop"], t["target"]
        if (l <= stop) if long else (h >= stop):
            fill = min(stop, o) if long else max(stop, o)            # gap through the stop
            why = "BE+2" if t["be_done"] else "stop"
            self._close(t, fill, ts, why, bars, quiet)
            return
        if (h >= tgt) if long else (l <= tgt):
            self._close(t, tgt, ts, "objetivo", bars, quiet)
            return
        trig = t["entry"] + (1 if long else -1) * self.s.be_at * abs(tgt - t["entry"])
        if not t["be_done"] and ((h >= trig) if long else (l <= trig)):
            t["be_done"] = True
            t["stop"] = t["entry"] + (self.s.be_plus if long else -self.s.be_plus)
            if not quiet:
                self._note(f"🔒 {self.name} — stop a BE+2",
                           f"El precio recorrió el {self.s.be_at:.0%} del camino al objetivo. "
                           f"Stop movido a {_px(t['stop'])}: este trade ya no puede perder. "
                           f"Objetivo {_px(tgt)}.", 0x2ECC71, f"be:{t['id']}")

    def _close(self, t: dict, price: float, ts, why: str, bars, quiet: bool) -> None:
        s = self.s
        pts = (price - t["entry"]) if t["long"] else (t["entry"] - price)
        net = pts - s.cost_points
        usd = net * s.usd_per_point * s.contracts
        done = {**t, "exit": price, "closed_at": str(ts), "why": why,
                "pts": round(pts, 2), "net_pts": round(net, 2), "usd": round(usd, 2),
                "contracts": s.contracts}
        self.st.trade = None
        self._append_trade(done)
        if quiet:
            return
        mark = "✅" if usd > 0 else ("➖" if abs(net) < 0.5 else "🛑")
        side = "COMPRA" if t["long"] else "VENTA"
        text = (f"{side} {_px(t['entry'])} → {_px(price)} · **{why}**\n"
                f"**{pts:+.2f} pts · {_usd(usd)}** con {s.contracts} MNQ "
                f"(ya descontados {s.cost_points:g} pts de comisión y deslizamiento)\n"
                + self.summary_line())
        self._note(f"{mark} {self.name} — cerrado: {why}", text,
                   0x2ECC71 if usd > 0 else 0xE74C3C, f"close:{t['id']}",
                   image=self._chart(bars, done, f"{self.name} · {side} · {why} {pts:+.2f} pts"))

    # ------------------------------------------------------------------ #
    # messages
    # ------------------------------------------------------------------ #
    def _note(self, title: str, text: str, color: int, key: str, image: bytes | None = None):
        try:
            self.publish(title=title, text=text, color=color, key=key, image=image)
        except Exception as exc:  # noqa: BLE001 - a message must never break the loop
            log.warning("APERTURA message failed (%s)", type(exc).__name__)

    def _chart(self, bars: pd.DataFrame, t: dict, title: str) -> bytes | None:
        if self.chart is None:
            return None
        try:
            day = bars[bars.index.date == pd.Timestamp(t["opened_at"]).date()]
            day = day[day.index.strftime("%H:%M") > "09:30"]
            five = day.resample("5min", label="right", closed="right").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
            trade = {"entry": t["entry"], "stop_initial": t["stop_initial"], "stop": t["stop"],
                     "tp2": t["target"], "opened_at": t["opened_at"],
                     "closed_at": t.get("closed_at"), "exit_price": t.get("exit"),
                     "pnl_points": t.get("pts"),
                     "context": {"zone_bottom": t["box_lo"], "zone_top": t["box_hi"]}}
            return self.chart(five, trade, title=title)
        except Exception as exc:  # noqa: BLE001
            log.warning("APERTURA chart failed (%s)", type(exc).__name__)
            return None

    def summary_line(self) -> str:
        if not self.closed:
            return f"Récord {self.name}: sin trades todavía."
        usd = [t["usd"] for t in self.closed]
        wins = sum(1 for u in usd if u > 0)
        return (f"Récord {self.name}: {len(usd)} trades · ganó {wins} ({wins / len(usd):.0%}) · "
                f"total **{_usd(sum(usd))}**")

    def record_text(self, last: int = 10) -> str:
        """For the !apertura command: totals, worst drop and the last trades."""
        s = self.s
        lines = [f"🛰️ **{self.name}** — estrategia APERTURA (ruptura de la caja 9:30–9:45) "
                 f"· {s.contracts} MNQ · en papel"]
        st, t = self.st, self.st.trade
        if t is not None:
            lines.append(f"🟡 Abierto: {'COMPRA' if t['long'] else 'VENTA'} {_px(t['entry'])} · "
                         f"stop {_px(t['stop'])} · objetivo {_px(t['target'])}")
        elif st.box_sent and st.hi is not None and not st.done:
            lines.append(f"⏳ Hoy esperando ruptura: caja {_px(st.lo)} – {_px(st.hi)}")
        if not self.closed:
            lines.append("_todavía no hay trades cerrados_")
            return "\n".join(lines)
        usd = [x["usd"] for x in self.closed]
        eq = peak = dd = 0.0
        for u in usd:
            eq += u
            peak = max(peak, eq)
            dd = max(dd, peak - eq)
        wins = sum(1 for u in usd if u > 0)
        lines.append(f"**Total:** {len(usd)} trades · gana {wins / len(usd):.0%} · "
                     f"**{_usd(sum(usd))}** · peor caída {_usd(-dd)}")
        lines.append("")
        for x in self.closed[-last:][::-1]:
            mark = "✅" if x["usd"] > 0 else "🛑"
            lines.append(f"{mark} {x['day']} · {'COMPRA' if x['long'] else 'VENTA'} "
                         f"{_px(x['entry'])} → {_px(x['exit'])} · {x['why']} · "
                         f"**{_usd(x['usd'])}**")
        return "\n".join(lines)
