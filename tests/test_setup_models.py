from datetime import date

import pandas as pd
import pytest

from signal_engine.features.market_structure.models import Direction
from signal_engine.setups.models import (
    LevelRef, SetupCandidate, SetupStatus, risk_reward, update_status,
)
from tests.conftest import tz_bars

TZ = "America/New_York"


def test_risk_reward_price_only():
    assert risk_reward(100, 96, [104, 108]) == [1.0, 2.0]
    assert risk_reward(100, 100, [104]) == [0.0]   # zero-risk guard


def _candidate(direction, entry, stop, tp, ts, valid_until):
    return SetupCandidate(
        setup_name="x", instrument="ES", direction=direction, timestamp=ts,
        timeframe="5m", entry=entry, stop_loss=stop, take_profits=[tp],
        risk_reward=risk_reward(entry, stop, [tp]), reasons=[], invalidation_conditions=[],
        invalidation_price=stop, valid_until=valid_until,
    )


def test_update_status_invalid_when_stop_hit():
    ts = pd.Timestamp("2026-06-15 10:00", tz=TZ)
    cand = _candidate(Direction.BULLISH, 100, 96, 108, ts, ts + pd.Timedelta(minutes=30))
    future = tz_bars(["2026-06-15 10:05"], [(100, 101, 95, 97)])  # low 95 <= stop 96
    assert update_status(cand, future) is SetupStatus.INVALID


def test_update_status_expired():
    ts = pd.Timestamp("2026-06-15 10:00", tz=TZ)
    cand = _candidate(Direction.BULLISH, 100, 96, 120, ts, ts + pd.Timedelta(minutes=5))
    # bar at 10:10 > valid_until (10:05); never hit stop or tp
    future = tz_bars(["2026-06-15 10:05", "2026-06-15 10:10"],
                     [(100, 101, 99, 100), (100, 102, 99, 101)])
    assert update_status(cand, future) is SetupStatus.EXPIRED


def test_update_status_valid_when_untouched():
    ts = pd.Timestamp("2026-06-15 10:00", tz=TZ)
    cand = _candidate(Direction.BULLISH, 100, 96, 108, ts, ts + pd.Timedelta(hours=2))
    future = tz_bars(["2026-06-15 10:05"], [(100, 101, 99, 100.5)])
    assert update_status(cand, future) is SetupStatus.VALID
