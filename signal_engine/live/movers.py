"""What is moving the Nasdaq-100 today: the mega caps' % change and an
estimate of how many NQ points each one is adding or taking away.

Context for the operator, never an input to the strategy. Quotes come from
Yahoo Finance's public chart API (same source as the NQ feed). The index
weights are APPROXIMATE and change over time — they live in
config/nq_movers.yaml so they can be refreshed without touching code.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import yaml

_CONFIG = Path(__file__).resolve().parent.parent / "config" / "nq_movers.yaml"
_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=5d"
_UA = "Mozilla/5.0 (compatible; signal-engine/1.0)"


def load_components(path: Path = _CONFIG) -> list[dict]:
    raw = yaml.safe_load(path.read_text()) or {}
    return [{"symbol": c["symbol"], "name": c.get("name", c["symbol"]),
             "weight": float(c.get("weight", 0))} for c in raw.get("components", [])]


def _fetch(sym: str, timeout: float) -> bytes:
    req = urllib.request.Request(_URL.format(sym=sym), headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_quote(payload: bytes) -> tuple[float, float] | None:
    """(last price, previous close) from a Yahoo chart response, or None."""
    try:
        meta = json.loads(payload)["chart"]["result"][0]["meta"]
        last = float(meta["regularMarketPrice"])
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        return (last, float(prev)) if prev else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


class MoversService:
    """Cached snapshot; refreshed at most every `ttl` seconds, all quotes in
    parallel, and a failed quote just drops out (the rest still show)."""

    def __init__(self, fetch: Callable[[str, float], bytes] | None = None,
                 ttl: float = 60.0, timeout: float = 4.0,
                 components: list[dict] | None = None):
        self._fetch = fetch or _fetch
        self.ttl, self.timeout = ttl, timeout
        self.components = components if components is not None else load_components()
        self._lock = threading.Lock()
        self._cache: tuple[float, list[dict]] | None = None

    def _quotes(self) -> list[dict]:
        def one(c):
            try:
                q = parse_quote(self._fetch(c["symbol"], self.timeout))
            except Exception:  # noqa: BLE001 - one bad quote never breaks the card
                q = None
            if q is None:
                return None
            last, prev = q
            return {**c, "price": last, "chg_pct": (last / prev - 1) * 100}
        with ThreadPoolExecutor(max_workers=8) as pool:
            return [r for r in pool.map(one, self.components) if r is not None]

    def snapshot(self, nq_price: float | None) -> dict:
        with self._lock:
            now = time.time()
            if self._cache is None or now - self._cache[0] > self.ttl:
                self._cache = (now, self._quotes())
            at, items = self._cache
        out = []
        for q in items:
            # index move from this stock ≈ weight × its % change; in NQ points
            pts = (nq_price * q["weight"] / 100 * q["chg_pct"] / 100) if nq_price else None
            out.append({**q, "nq_points": pts})
        out.sort(key=lambda q: abs(q["nq_points"] or q["chg_pct"] * q["weight"]), reverse=True)
        pts = [q["nq_points"] for q in out if q["nq_points"] is not None]
        return {"asof": at, "items": out,
                "up": sum(1 for q in out if q["chg_pct"] > 0),
                "down": sum(1 for q in out if q["chg_pct"] < 0),
                "nq_points_total": sum(pts) if pts else None,
                "weight_covered": sum(q["weight"] for q in out)}
