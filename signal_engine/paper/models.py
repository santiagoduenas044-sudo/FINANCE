"""PaperTrade domain model (Phase 14 — GHOST trade memory).

A PaperTrade is GHOST's internal, SIMULATED record of a signal it decided to
"take". It is NOT connected to a broker, NEVER places an order, and NEVER moves
real money — it only lets GHOST remember and monitor what it did and how it
evolved, bar by bar, on delayed/historical data.

Lifecycle (unambiguous, deterministic):

    SIGNAL/PENDING ── entry filled ──▶ ACTIVE ──┬── TP1_HIT (partial; only if a
                                                │            TP2 remains open)
                                                ├── TP2_HIT      (terminal)
                                                ├── STOPPED      (terminal)
                                                ├── INVALIDATED  (terminal)
                                                ├── CANCELLED    (terminal, never filled)
                                                └── CLOSED        (terminal, forced/expired)

`status` is the single source of truth for where a trade is. Whether a status
is still open is derived deterministically by `PaperTrade.is_open` (a TP1_HIT
trade is open only while it still has an un-hit TP2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class TradeStatus(str, Enum):
    PENDING = "PENDING"          # created; entry not yet filled
    ACTIVE = "ACTIVE"            # entry filled; no target/stop hit yet
    TP1_HIT = "TP1_HIT"          # first target reached (partial if a TP2 remains)
    TP2_HIT = "TP2_HIT"          # final target reached (terminal)
    STOPPED = "STOPPED"          # stop-loss hit (terminal)
    INVALIDATED = "INVALIDATED"  # structural/protective invalidation (terminal)
    CANCELLED = "CANCELLED"      # pending entry never filled before invalidation (terminal)
    CLOSED = "CLOSED"            # forced/expired close (terminal)


#: Reasons a trade left the market — mirrors the terminal statuses plus TP1 for
#: a single-target trade and EXPIRED for a forced end-of-data close.
class ExitReason(str, Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    STOP = "STOP"
    INVALIDATION = "INVALIDATION"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


_TERMINAL = {TradeStatus.TP2_HIT, TradeStatus.STOPPED, TradeStatus.INVALIDATED,
             TradeStatus.CANCELLED, TradeStatus.CLOSED}


@dataclass
class PaperTrade:
    # identity / definition (immutable once created)
    signal_id: str
    instrument: str
    direction: str                 # "LONG" / "SHORT"
    setup: str
    entry: float
    stop: float
    take_profits: list[float]
    opened_at: pd.Timestamp        # decision bar timestamp
    initial_risk: float            # |entry - stop| in points (>0)
    invalidation_price: float = 0.0
    timeframe: str | None = None
    # context snapshot at creation (regime/session/confluence) — for later stats
    context: dict = field(default_factory=dict)

    # evolving state
    status: TradeStatus = TradeStatus.PENDING
    current_price: float = 0.0
    pnl_points: float = 0.0
    r_multiple: float = 0.0
    mfe_points: float = 0.0        # max favorable excursion (points, >= 0)
    mae_points: float = 0.0        # max adverse excursion (points, <= 0)
    mfe_r: float = 0.0
    mae_r: float = 0.0
    tp1_hit: bool = False
    updated_at: pd.Timestamp | None = None
    last_bar_time: pd.Timestamp | None = None
    bars_held: int = 0

    # outcome (set on close)
    closed_at: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_reason: ExitReason | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.initial_risk <= 0:
            # never divide by zero downstream; a zero-risk trade is meaningless
            self.initial_risk = abs(self.entry - self.stop) or 1e-9
        if self.current_price == 0.0:
            self.current_price = self.entry

    # ------------------------------------------------------------------ #
    @property
    def is_long(self) -> bool:
        return self.direction == "LONG"

    @property
    def tp1(self) -> float | None:
        return self.take_profits[0] if self.take_profits else None

    @property
    def tp2(self) -> float | None:
        return self.take_profits[1] if len(self.take_profits) > 1 else None

    @property
    def is_open(self) -> bool:
        """A trade is open while PENDING/ACTIVE, or TP1_HIT with a TP2 still to
        reach. Everything terminal is closed."""
        if self.status in _TERMINAL:
            return False
        if self.status is TradeStatus.TP1_HIT:
            return self.tp2 is not None  # still holding toward TP2
        return True

    @property
    def dedup_key(self) -> str:
        """Phase 15: one open trade PER SYMBOL — max_open_per_symbol=1 blocks
        LONG+SHORT, LONG+LONG (different setup), etc. GHOST is the gatekeeper."""
        return self.instrument

    @property
    def tp3(self) -> float | None:
        return self.take_profits[2] if len(self.take_profits) > 2 else None

    def _dist(self, target: float | None) -> float | None:
        """Signed distance in points from current price toward `target`
        (positive = still to reach for the trade direction, negative = already
        passed). None if target is missing."""
        if target is None or self.current_price is None:
            return None
        d = (target - self.current_price) if self.is_long else (self.current_price - target)
        return round(d, 4)

    def _dist_stop(self) -> float:
        """Points from current price to protective stop (always >= 0 while
        healthy — a negative value means the stop was already breached)."""
        d = (self.current_price - self.stop) if self.is_long else (self.stop - self.current_price)
        return round(d, 4)

    def to_dict(self) -> dict:
        risk = self.initial_risk if self.initial_risk > 0 else 1e-9
        def _r(target):
            if target is None:
                return None
            return round(abs(target - self.entry) / risk, 3)
        return {
            "signal_id": self.signal_id,
            "instrument": self.instrument,
            "direction": self.direction,
            "setup": self.setup,
            "entry": self.entry,
            "stop": self.stop,
            "take_profits": list(self.take_profits),
            # Phase 15: explicit, unambiguous target labels + R multiples so the
            # dashboard / Discord never have to guess which number is TP1 vs TP2.
            "tp1": self.tp1,
            "tp2": self.tp2,
            "tp3": self.tp3,
            "tp1_r": _r(self.tp1),
            "tp2_r": _r(self.tp2),
            "tp3_r": _r(self.tp3),
            # Live distances from current price toward each level (positive =
            # target still ahead; negative = already breached).
            "distance_to_stop": self._dist_stop(),
            "distance_to_tp1": self._dist(self.tp1),
            "distance_to_tp2": self._dist(self.tp2),
            "distance_to_tp3": self._dist(self.tp3),
            "invalidation_price": self.invalidation_price,
            "timeframe": self.timeframe,
            "initial_risk": self.initial_risk,
            "status": self.status.value,
            "is_open": self.is_open,
            "current_price": self.current_price,
            "pnl_points": round(self.pnl_points, 4),
            "r_multiple": round(self.r_multiple, 4),
            "mfe_points": round(self.mfe_points, 4),
            "mae_points": round(self.mae_points, 4),
            "mfe_r": round(self.mfe_r, 4),
            "mae_r": round(self.mae_r, 4),
            "tp1_hit": self.tp1_hit,
            "bars_held": self.bars_held,
            "opened_at": _iso(self.opened_at),
            "updated_at": _iso(self.updated_at),
            "closed_at": _iso(self.closed_at),
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "duration_seconds": self.duration_seconds,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperTrade":
        """Rebuild a PaperTrade from its persisted dict (restart recovery)."""
        t = cls(
            signal_id=d["signal_id"], instrument=d["instrument"],
            direction=d["direction"], setup=d["setup"], entry=d["entry"],
            stop=d["stop"], take_profits=list(d.get("take_profits") or []),
            opened_at=pd.Timestamp(d["opened_at"]) if d.get("opened_at") else pd.Timestamp.now(tz="UTC"),
            initial_risk=d.get("initial_risk") or abs(d["entry"] - d["stop"]) or 1e-9,
            invalidation_price=d.get("invalidation_price", 0.0),
            timeframe=d.get("timeframe"), context=d.get("context") or {},
        )
        t.status = TradeStatus(d.get("status", "PENDING"))
        t.current_price = d.get("current_price", t.entry)
        t.pnl_points = d.get("pnl_points", 0.0)
        t.r_multiple = d.get("r_multiple", 0.0)
        t.mfe_points = d.get("mfe_points", 0.0)
        t.mae_points = d.get("mae_points", 0.0)
        t.mfe_r = d.get("mfe_r", 0.0)
        t.mae_r = d.get("mae_r", 0.0)
        t.tp1_hit = d.get("tp1_hit", False)
        t.bars_held = d.get("bars_held", 0)
        t.updated_at = pd.Timestamp(d["updated_at"]) if d.get("updated_at") else None
        t.closed_at = pd.Timestamp(d["closed_at"]) if d.get("closed_at") else None
        t.exit_price = d.get("exit_price")
        t.exit_reason = ExitReason(d["exit_reason"]) if d.get("exit_reason") else None
        t.duration_seconds = d.get("duration_seconds")
        return t


def _iso(ts) -> str | None:
    return ts.isoformat() if ts is not None else None
