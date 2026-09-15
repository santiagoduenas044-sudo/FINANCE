"""Domain model for Live Market Data (Phase 10).

DataFeedStatus is what the dashboard shows (LIVE / STALE / DISCONNECTED). It
is computed, never asserted: `LiveFeed` derives it from actual connection
state and how long ago data last arrived — never from a flag someone forgot
to flip.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd


class DataFeedStatus(str, Enum):
    LIVE = "LIVE"                  # connected and data is fresh
    STALE = "STALE"                # connected (or was), but no fresh data recently
    DISCONNECTED = "DISCONNECTED"  # adapter reports no connection


@dataclass(frozen=True)
class LiveBar:
    """One CLOSED 1-minute bar as delivered by a LiveDataAdapter. Timestamp
    marks the bar's CLOSE (same convention as the historical Data Engine —
    see signal_engine/data/models.py) and MUST be tz-aware."""
    symbol: str
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class FeedHealth:
    status: DataFeedStatus
    last_update_at: datetime | None
    last_bar_timestamp: pd.Timestamp | None
    reconnect_attempts: int
    detail: str = ""
