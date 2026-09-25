"""Trade-idea lifecycle for the zone strategy: heads-up -> confirmed / direct /
cancelled, numbered per trading day.

The detector (setups/zone_retrace.py) judges every zone from the bars alone;
this board only remembers what was already ANNOUNCED, so the operator gets one
"get ready" per zone, one outcome per idea, and consistent numbering across a
restart. It never decides a trade — GHOST and the paper tracker still do.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from ..setups.zone_retrace import ARMED, CANCELLED, CONFIRMED, EXPIRED, ZoneState

log = logging.getLogger("signal_engine.runtime.ideas")

WATCHING, TAKEN, DROPPED = "WATCHING", "TAKEN", "CANCELLED"

CANCEL_REASON = {
    CANCELLED: "el precio cruzó la zona sin la reacción",
    EXPIRED: "el precio tocó la zona pero no reaccionó",
    "aged": "la zona quedó vieja sin que el precio regresara",
    "window": "se cerró la ventana de entradas del día",
}


@dataclass
class Idea:
    zone_id: str
    symbol: str
    number: int
    day: str                      # ET trading date, YYYY-MM-DD
    direction: str                # LONG / SHORT
    top: float
    bottom: float
    stop: float
    announced_at: str
    status: str = WATCHING
    direct: bool = False          # confirmed without a prior heads-up
    tp1: float | None = None      # the announced plan, for the "no se cumplió" chart
    final: float | None = None

    @property
    def mid(self) -> float:
        return (self.top + self.bottom) / 2.0

    def to_event(self, **extra) -> dict:
        return {**asdict(self), "mid": round(self.mid, 2),
                "risk_points": round(abs(self.mid - self.stop), 2), **extra}


class IdeaBoard:
    def __init__(self, path: str | Path | None = None, *,
                 timezone: str = "America/New_York"):
        self.path = Path(path) if path else None
        self.tz = timezone
        self.ideas: dict[str, Idea] = {}
        self.counters: dict[str, int] = {}
        self._load()

    # ------------------------------------------------------------------ #
    def _day(self, ts: pd.Timestamp) -> str:
        ts = pd.Timestamp(ts)
        ts = ts.tz_localize(self.tz) if ts.tzinfo is None else ts.tz_convert(self.tz)
        return ts.date().isoformat()

    def _new(self, symbol: str, st: ZoneState, now: pd.Timestamp, *,
             direct: bool) -> Idea:
        z = st.zone
        day = self._day(now)
        self.counters[day] = self.counters.get(day, 0) + 1
        idea = Idea(zone_id=z.zone_id, symbol=symbol, number=self.counters[day], day=day,
                    direction="LONG" if z.bullish else "SHORT", top=z.top,
                    bottom=z.bottom, stop=round(z.stop, 2),
                    announced_at=pd.Timestamp(now).isoformat(), direct=direct)
        self.ideas[z.zone_id] = idea
        return idea

    def watching(self, symbol: str) -> list[Idea]:
        return [i for i in self.ideas.values()
                if i.symbol == symbol and i.status == WATCHING]

    # ------------------------------------------------------------------ #
    def review(self, symbol: str, states: list[ZoneState], now: pd.Timestamp, *,
               window_open: bool, can_announce: bool,
               rejections: dict[str, str] | None = None) -> list[dict]:
        """Advance announced ideas and announce new ones. Returns events:
        {"event": "idea" | "cancelled", ...Idea fields}. Call AFTER the bar's
        trades were opened, so a confirmed idea is already TAKEN here."""
        rejections = rejections or {}
        by_id = {s.zone.zone_id: s for s in states}
        events: list[dict] = []

        for idea in self.watching(symbol):
            st = by_id.get(idea.zone_id)
            reason = None
            if idea.zone_id in rejections:
                reason = rejections[idea.zone_id]
            elif st is None:
                reason = CANCEL_REASON["aged"]
            elif st.state in (CANCELLED, EXPIRED):
                reason = CANCEL_REASON[st.state]
            elif st.state == CONFIRMED:
                # confirmed but no trade opened and no reason reported: the
                # candidate was not buildable (price ran too far from the zone)
                reason = "el precio reaccionó pero ya estaba muy lejos de la zona"
            elif not window_open:
                reason = CANCEL_REASON["window"]
            if reason:
                idea.status = DROPPED
                events.append(idea.to_event(event="cancelled", reason=reason))

        if can_announce and window_open:
            busy = {i.direction for i in self.watching(symbol)}
            armed = sorted((s for s in states if s.state == ARMED
                            and s.zone.zone_id not in self.ideas),
                           key=lambda s: s.distance if s.distance is not None else 1e9)
            for st in armed:
                d = "LONG" if st.zone.bullish else "SHORT"
                if d in busy:
                    continue
                idea = self._new(symbol, st, now, direct=False)
                busy.add(d)
                events.append(idea.to_event(event="idea"))

        if events:
            self._save()
        return events

    def set_plan(self, zone_id: str, tp1: float | None, final: float | None) -> None:
        """Remember the targets announced with the heads-up, so a later
        cancellation can show the plan that did not happen."""
        idea = self.ideas.get(zone_id)
        if idea is None:
            return
        idea.tp1 = round(tp1, 2) if tp1 is not None else None
        idea.final = round(final, 2) if final is not None else None
        self._save()

    def claim_for_trade(self, symbol: str, st: ZoneState | None, zone_id: str,
                        now: pd.Timestamp) -> Idea | None:
        """The zone's trade is opening: mark the announced idea TAKEN, or
        create a numbered DIRECT idea when there was no heads-up."""
        idea = self.ideas.get(zone_id)
        if idea is not None and idea.status == WATCHING:
            idea.status = TAKEN
        elif idea is None and st is not None:
            idea = self._new(symbol, st, now, direct=True)
            idea.status = TAKEN
        else:
            return None
        self._save()
        return idea

    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        if self.path is None:
            return
        # keep a few days: enough for numbering and restart dedup
        days = sorted(self.counters)[-3:]
        self.counters = {d: self.counters[d] for d in days}
        self.ideas = {k: v for k, v in self.ideas.items() if v.day in days}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({
                "counters": self.counters,
                "ideas": [asdict(i) for i in self.ideas.values()]}, indent=1))
        except OSError as exc:
            log.warning("could not save ideas (%s)", type(exc).__name__)

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
            self.counters = {k: int(v) for k, v in raw.get("counters", {}).items()}
            for d in raw.get("ideas", []):
                idea = Idea(**d)
                self.ideas[idea.zone_id] = idea
        except (OSError, ValueError, TypeError) as exc:
            log.warning("ideas file unreadable (%s); starting empty", type(exc).__name__)
