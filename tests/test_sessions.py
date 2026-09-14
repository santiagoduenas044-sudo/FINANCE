from datetime import date

import pandas as pd
import pytest

from signal_engine.config.schema import SessionsConfig
from signal_engine.data.models import CLOSED, ETH, RTH, SESSION, TRADING_DATE
from signal_engine.data.sessions import SessionClassifier

TZ = "America/New_York"


def ts(s: str) -> pd.Timestamp:
    return pd.Timestamp(s, tz=TZ)


@pytest.fixture
def clf():
    return SessionClassifier(SessionsConfig())


@pytest.mark.parametrize("stamp,expected", [
    ("2026-06-15 09:30", RTH),   # Monday RTH open
    ("2026-06-15 15:59", RTH),   # RTH
    ("2026-06-15 16:00", ETH),   # RTH end -> ETH
    ("2026-06-15 16:59", ETH),   # ETH afternoon
    ("2026-06-15 17:00", CLOSED),  # maintenance halt
    ("2026-06-15 17:30", CLOSED),
    ("2026-06-15 18:00", ETH),   # Globex reopen
    ("2026-06-15 23:30", ETH),   # overnight
    ("2026-06-15 03:00", ETH),   # early morning ETH
    ("2026-06-13 12:00", CLOSED),  # Saturday
    ("2026-06-14 12:00", CLOSED),  # Sunday pre-open
    ("2026-06-14 18:00", ETH),   # Sunday Globex open
    ("2026-06-19 17:30", CLOSED),  # Friday post-close
])
def test_classify(clf, stamp, expected):
    assert clf.classify(ts(stamp)) == expected


def test_trading_date_rollover(clf):
    # before 17:00 -> same day
    assert clf.trading_date(ts("2026-06-15 09:30")) == date(2026, 6, 15)
    # at/after 17:00 -> next day
    assert clf.trading_date(ts("2026-06-15 18:00")) == date(2026, 6, 16)
    # Sunday evening -> Monday
    assert clf.trading_date(ts("2026-06-14 18:00")) == date(2026, 6, 15)


def test_eth_toggle_off():
    cfg = SessionsConfig()
    cfg.eth.enabled = False
    clf = SessionClassifier(cfg)
    assert clf.classify(ts("2026-06-15 23:30")) == CLOSED
    assert clf.classify(ts("2026-06-15 09:35")) == RTH  # RTH still works


def test_annotate_columns(clf):
    idx = pd.DatetimeIndex([
        ts("2026-06-15 09:31"), ts("2026-06-15 16:30"),
        ts("2026-06-15 17:30"), ts("2026-06-15 19:00"),
    ], name="timestamp")
    df = pd.DataFrame({"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx)
    out = clf.annotate(df)
    assert list(out[SESSION]) == [RTH, ETH, CLOSED, ETH]
    # overnight bar (19:00) belongs to next trade date
    assert out[TRADING_DATE].iloc[-1] == date(2026, 6, 16)
    assert out[TRADING_DATE].iloc[0] == date(2026, 6, 15)
