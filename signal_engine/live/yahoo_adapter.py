"""YahooDelayedAdapter (Phase 11): real (delayed) ES/NQ market data via
Yahoo Finance's public chart endpoint.

Why Yahoo Finance: no API key/registration required (free MVP path), widely
used and documented (the `yfinance` package wraps this same endpoint),
provides 1-minute OHLCV bars for CME index futures (`ES=F`, `NQ=F`) which is
enough to derive 5m/15m/1H via the existing resample pipeline. Quotes for
unauthenticated/free access are DELAYED (Yahoo's own UI marks futures
quotes "Delayed Quote") — this adapter always reports `is_delayed = True`,
never claims real-time.

This sandbox's outbound network egress is restricted to a small allowlist
that does NOT include finance.yahoo.com (confirmed via curl — same failure
mode already documented for cmegroup.com/discord.com in earlier phases), so
the real HTTP call cannot be exercised from *this* development environment.
The adapter is written against the documented, stable shape of the public
endpoint and unit-tested with an injected fake transport (same pattern as
DiscordOutputAdapter) — running it for real requires a machine with normal
internet access (e.g. the user's own machine), where `run_dashboard.py`
already falls back to SimulatedLiveAdapter if `connect()` fails.

Do NOT assume this is the only possible source: swap this module out for
Databento, Interactive Brokers, or any other real vendor later — nothing in
LiveFeed/BotRuntime/Setup/Signal/Risk needs to change, by the same
LiveDataAdapter contract this implements.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable

import pandas as pd

from .adapter import LiveDataAdapter
from .models import LiveBar

# Yahoo's ticker convention for CME-listed continuous front-month futures.
_SYMBOL_MAP = {"ES": "ES=F", "NQ": "NQ=F"}

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_USER_AGENT = "Mozilla/5.0 (compatible; signal-engine/1.0)"
_BAR_INTERVAL = pd.Timedelta(minutes=1)   # matches interval=1m in the request


def _urllib_transport(ticker: str, timeout: float) -> bytes:
    # 5d gives ~115 hourly bars after resample — enough to warm EMA-50 on 1H.
    url = f"{_CHART_URL.format(ticker=ticker)}?interval=1m&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class YahooDelayedAdapter(LiveDataAdapter):
    """LiveDataAdapter backed by Yahoo Finance's public (unauthenticated,
    delayed) chart API. No API key needed."""

    name = "yahoo_delayed"
    is_delayed = True

    def __init__(
        self,
        timezone: str = "America/New_York",
        timeout_seconds: float = 10.0,
        transport: Callable[[str, float], bytes] | None = None,
        symbol_map: dict[str, str] | None = None,
    ):
        self._timezone = timezone
        self._timeout = timeout_seconds
        self._transport = transport or _urllib_transport
        self._symbol_map = symbol_map or dict(_SYMBOL_MAP)
        self._connected = False
        # last CLOSED bar timestamp emitted per symbol, to avoid re-sending
        # the same bars every poll (Yahoo returns the whole day's series).
        self._last_emitted: dict[str, pd.Timestamp] = {}

    def connect(self) -> None:
        # Validate reachability with a real (cheap) request rather than just
        # flipping a flag — a source that "connects" but can't actually be
        # reached would otherwise silently look healthy until the first poll.
        probe_symbol = next(iter(self._symbol_map))
        try:
            self._fetch(probe_symbol)
        except ConnectionError:
            self._connected = False
            raise
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def poll(self, symbol: str) -> list[LiveBar]:
        if not self._connected:
            raise ConnectionError("yahoo adapter: not connected")
        if symbol not in self._symbol_map:
            return []

        bars = self._fetch(symbol)
        last_seen = self._last_emitted.get(symbol)
        if last_seen is not None:
            bars = [b for b in bars if b.timestamp > last_seen]
        if bars:
            self._last_emitted[symbol] = bars[-1].timestamp
        return bars

    # ------------------------------------------------------------------ #
    def _fetch(self, symbol: str) -> list[LiveBar]:
        ticker = self._symbol_map[symbol]
        try:
            raw = self._transport(ticker, self._timeout)
            payload = json.loads(raw)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ConnectionError(f"yahoo finance request failed: {type(exc).__name__}") from exc

        try:
            result = payload["chart"]["result"][0]
            error = payload["chart"].get("error")
            if error:
                raise ConnectionError(f"yahoo finance API error: {error}")
            timestamps = result["timestamp"]
            quote = result["indicators"]["quote"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise ConnectionError(f"yahoo finance response malformed: {type(exc).__name__}") from exc

        bars: list[LiveBar] = []
        n = len(timestamps)
        # Drop the last entry: it is the currently-forming (not yet closed)
        # bar — LiveDataAdapter.poll() must only ever return CLOSED bars.
        for i in range(n - 1):
            o, h, l, c, v = (quote["open"][i], quote["high"][i], quote["low"][i],
                             quote["close"][i], quote["volume"][i])
            if None in (o, h, l, c, v):
                continue   # Yahoo reports gaps (e.g. maintenance break) as nulls
            # Yahoo stamps each bar with its interval START; LiveBar (and the
            # whole engine) labels bars by their CLOSE. Without this shift every
            # live bar sat one minute early: RTH began at 09:31, the ORB window
            # and 5m bins took the wrong minutes.
            ts = (pd.Timestamp(timestamps[i], unit="s", tz="UTC").tz_convert(self._timezone)
                  + _BAR_INTERVAL)
            bars.append(LiveBar(symbol=symbol, timestamp=ts, open=float(o), high=float(h),
                                low=float(l), close=float(c), volume=float(v)))
        return bars
