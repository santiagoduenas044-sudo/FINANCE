"""Economic calendar: the week's high-impact US releases (CPI, jobs report,
FOMC, ...), so a bot can stay out of the minutes around them and the
dashboard can say what is coming today.

Source: the free weekly calendar feed ForexFactory publishes
(nfs.faireconomy.media). It is read at most every few hours and kept in
data_store/news/week.json, so a failed read keeps the last good copy and
never stops a bot. Times come with their UTC offset and are handled in ET.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data_store" / "news" / "week.json"
FEED = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 15 * 60
NY = "America/New_York"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Event:
    when: pd.Timestamp        # ET
    title: str
    impact: str               # "High" / "Medium" / "Low" / "Holiday"
    country: str

    def to_dict(self) -> dict:
        return {"time": self.when.isoformat(), "hm": self.when.strftime("%H:%M"),
                "title": self.title, "impact": self.impact, "country": self.country}


def parse(rows: list[dict]) -> list[Event]:
    out = []
    for r in rows or []:
        try:
            when = pd.Timestamp(r["date"])
            when = when.tz_localize(NY) if when.tzinfo is None else when.tz_convert(NY)
            out.append(Event(when, str(r.get("title", "")).strip(), str(r.get("impact", "")),
                             str(r.get("country", ""))))
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda e: e.when)


class NewsCalendar:
    def __init__(self, cache: Path = CACHE, fetch=None, countries=("USD",)):
        self.cache = Path(cache)
        self.fetch = fetch or self._fetch
        self.countries = set(countries)
        self._events: list[Event] | None = None
        self._next_try = 0.0

    # ------------------------------------------------------------------ #
    @staticmethod
    def _fetch() -> list[dict]:
        req = urllib.request.Request(FEED, headers={"User-Agent": "Mozilla/5.0 (GHOST bot)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def refresh(self, force: bool = False) -> None:
        """Read the feed if the cached copy is old (never more than every
        RETRY_SECONDS after a failure). Errors only leave a log line."""
        now = time.time()
        age = now - self.cache.stat().st_mtime if self.cache.exists() else None
        if not force and ((age is not None and age < REFRESH_SECONDS) or now < self._next_try):
            return
        try:
            rows = self.fetch()
            if not isinstance(rows, list):
                raise ValueError("unexpected feed format")
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows))
            tmp.replace(self.cache)
            self._events = None
        except Exception as exc:  # noqa: BLE001 - no calendar never stops a bot
            self._next_try = now + RETRY_SECONDS
            log.warning("calendario de noticias no leído (%s)", type(exc).__name__)

    def events(self) -> list[Event]:
        if self._events is None:
            try:
                rows = json.loads(self.cache.read_text())
            except (OSError, ValueError):
                rows = []
            self._events = [e for e in parse(rows) if e.country in self.countries]
        return self._events

    # ------------------------------------------------------------------ #
    def on_day(self, ts, impacts=("High",)) -> list[Event]:
        day = _ny(ts).date()
        return [e for e in self.events() if e.when.date() == day and e.impact in impacts]

    def blackout(self, ts, before_min: float = 10, after_min: float = 15,
                 impacts=("High",)) -> Event | None:
        """The event whose window [before, after] contains `ts`, if any."""
        t = _ny(ts)
        for e in self.events():
            if e.impact in impacts and \
                    e.when - pd.Timedelta(minutes=before_min) <= t <= e.when + pd.Timedelta(minutes=after_min):
                return e
        return None


def _ny(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize(NY) if t.tzinfo is None else t.tz_convert(NY)
