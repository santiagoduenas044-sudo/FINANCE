"""LiveDataAdapter (Phase 10): the ONLY thing that knows how to talk to a
market data source. Swappable by design — the core (LiveFeed, Runtime,
Setup/Signal/Risk Engine) depends only on this interface, never on a
concrete vendor. Do NOT assume TradingView; Databento, Interactive Brokers,
or any other source can implement this later without touching anything else.

A `poll()` call is expected to be cheap and non-blocking-ish (bounded), and
must raise a `ConnectionError` (or subclass) on any transport failure so
`LiveFeed` can apply its reconnect policy uniformly regardless of vendor.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import LiveBar


class LiveDataAdapter(ABC):
    name: str
    # Whether this source is known to be delayed (vs. real-time). LiveFeed
    # reads this to report DataFeedStatus.DELAYED instead of LIVE — purely
    # informational for the dashboard, never affects reconnect/staleness
    # logic. Default False so existing adapters (SimulatedLiveAdapter) are
    # unaffected; a real delayed vendor sets it True.
    is_delayed: bool = False

    @abstractmethod
    def connect(self) -> None:
        """Establish the connection. Raise ConnectionError on failure."""

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down the connection. Must never raise."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @abstractmethod
    def poll(self, symbol: str) -> list[LiveBar]:
        """Return newly CLOSED 1-minute bars for `symbol` since the last call
        (possibly empty). Must raise ConnectionError if the connection is
        lost — never return stale/fabricated data silently."""
