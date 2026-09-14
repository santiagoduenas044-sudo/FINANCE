"""Session handling: RTH/ETH classification, trade-date assignment, and
session-open origins for resampling. Timezone/DST are delegated to the tz
database via tz-aware timestamps (canonical tz = America/New_York).

CME index futures schedule (ET), see docs/MARKET_DATA.md §7:
  - RTH:  09:30 - 16:00, Mon-Fri
  - ETH (Globex): Sun 18:00 -> Fri 17:00, with a daily maintenance halt
    17:00 - 18:00 (Mon-Thu). Saturday closed.
  - Trade date rolls at 17:00 ET (configurable): bars at/after the rollover
    belong to the NEXT calendar day's trade date (the upcoming RTH session).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pandas as pd

from ..config.schema import SessionsConfig
from .models import CLOSED, ETH, RTH, SESSION, TRADING_DATE


def _parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


class SessionClassifier:
    def __init__(self, cfg: SessionsConfig, enable_eth: bool | None = None):
        self.cfg = cfg
        self.rth_start = _parse_time(cfg.rth.start)
        self.rth_end = _parse_time(cfg.rth.end)
        self.eth_start = _parse_time(cfg.eth.start)          # 18:00
        self.eth_end = _parse_time(cfg.eth.end)              # 17:00
        self.mb_start = _parse_time(cfg.eth.maintenance_break.start)  # 17:00
        self.mb_end = _parse_time(cfg.eth.maintenance_break.end)      # 18:00
        self.rollover = _parse_time(cfg.trade_date_rollover)         # 17:00
        self.eth_enabled = cfg.eth.enabled if enable_eth is None else enable_eth

    # -- single timestamp API (readable reference implementation) ----------- #
    def classify(self, ts: pd.Timestamp) -> str:
        wd = ts.weekday()          # Mon=0 .. Sun=6
        t = ts.time()

        # Saturday: fully closed
        if wd == 5:
            return CLOSED
        # Sunday: closed until Globex open (18:00)
        if wd == 6:
            return ETH if (self.eth_enabled and t >= self.eth_start) else CLOSED
        # Friday: closed at/after 17:00
        if wd == 4 and t >= self.mb_start:
            return CLOSED
        # Mon-Thu maintenance halt 17:00-18:00
        if wd in (0, 1, 2, 3) and self.mb_start <= t < self.mb_end:
            return CLOSED
        # RTH window (Mon-Fri)
        if self.rth_start <= t < self.rth_end:
            return RTH
        # everything else that is open = ETH
        return ETH if self.eth_enabled else CLOSED

    def trading_date(self, ts: pd.Timestamp) -> date:
        """CME trade date: rolls at ``trade_date_rollover`` (17:00 ET)."""
        d = ts.date()
        if ts.time() >= self.rollover:
            d = d + timedelta(days=1)
        # Sunday evening -> Monday already handled by +1; keep as-is.
        return d

    def session_open_dt(self, trading_date: date, session: str, tz) -> pd.Timestamp:
        """Datetime of the session open for a (trade date, session), used as the
        resample origin so bins align to the session open."""
        if session == RTH:
            naive = datetime.combine(trading_date, self.rth_start)
        else:  # ETH session for trade date D starts at 18:00 on D-1
            naive = datetime.combine(trading_date - timedelta(days=1), self.eth_start)
        return pd.Timestamp(naive, tz=tz)

    # -- vectorized annotation --------------------------------------------- #
    def annotate(self, df: pd.DataFrame, label_interval=None) -> pd.DataFrame:
        """Add SESSION and TRADING_DATE columns to a CLOSE-labeled OHLCV df.

        A bar is classified by the session it TRADED in, i.e. by its open
        instant (close label minus ``label_interval``), not by its close label.
        This keeps the boundary bar (e.g. the 09:29->09:30 pre-open minute)
        out of RTH even though its close stamps exactly on the RTH open.
        """
        if df.empty:
            out = df.copy()
            out[SESSION] = pd.Series(dtype="object")
            out[TRADING_DATE] = pd.Series(dtype="object")
            return out

        import numpy as np

        idx = df.index if label_interval is None else (df.index - label_interval)
        wd = idx.weekday.to_numpy()
        secs = (idx.hour * 3600 + idx.minute * 60 + idx.second).to_numpy()

        def s(t: time) -> int:
            return t.hour * 3600 + t.minute * 60 + t.second

        rth_s, rth_e = s(self.rth_start), s(self.rth_end)
        eth_s = s(self.eth_start)
        mb_s, mb_e = s(self.mb_start), s(self.mb_end)
        roll_s = s(self.rollover)

        session = np.full(len(idx), ETH, dtype=object)
        # RTH window Mon-Fri
        is_weekday = wd <= 4
        rth_mask = is_weekday & (secs >= rth_s) & (secs < rth_e)
        session[rth_mask] = RTH
        # closed rules
        closed = np.zeros(len(idx), dtype=bool)
        closed |= (wd == 5)                                   # Saturday
        closed |= (wd == 6) & (secs < eth_s)                  # Sunday pre-open
        closed |= (wd == 4) & (secs >= mb_s)                  # Friday post-close
        closed |= np.isin(wd, [0, 1, 2, 3]) & (secs >= mb_s) & (secs < mb_e)  # maint
        session[closed] = CLOSED
        # RTH takes precedence over ETH but not over CLOSED (RTH never overlaps closed)
        session[rth_mask] = RTH
        if not self.eth_enabled:
            session[session == ETH] = CLOSED

        # trading date
        base_dates = np.array(list(idx.date), dtype=object)
        add_day = secs >= roll_s
        tdate = np.where(add_day, base_dates + timedelta(days=1), base_dates)

        out = df.copy()
        out[SESSION] = session
        out[TRADING_DATE] = tdate
        return out
