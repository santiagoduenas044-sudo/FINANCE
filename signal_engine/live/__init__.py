"""Live Market Data (Phase 10).

Live Data -> Analysis -> Setup -> Signal -> Risk -> Runtime -> Dashboard/Discord

`LiveDataAdapter` is the swappable interface (no TradingView assumed —
Databento/IBKR/anything else can implement it later). `LiveFeed` turns
incoming bars into the exact same multi-timeframe format the historical Data
Engine produces (reusing Phase 3's session/resample code, not duplicating
it), and reports LIVE/STALE/DISCONNECTED for the dashboard.

No broker, no order execution, no auto-trading.
"""
from __future__ import annotations

from .adapter import LiveDataAdapter
from .feed import LiveFeed
from .models import DataFeedStatus, FeedHealth, LiveBar
from .simulated import SimulatedLiveAdapter

__all__ = [
    "LiveDataAdapter", "LiveFeed", "SimulatedLiveAdapter",
    "DataFeedStatus", "FeedHealth", "LiveBar",
]
