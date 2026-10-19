"""GHOST intelligence domain models (Phase 14).

Structured, machine-readable answers to "what is the market doing?" so GHOST can
interpret CONTEXT before deciding — never ambiguous human language, never a
black-box confidence number. Every field is derived from data available up to
the decision bar (no-lookahead).

These are pure data objects. The rules that fill them live in regime.py /
context.py / confluence.py / brain.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Bias(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MarketRegime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    TRANSITION = "TRANSITION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    UNKNOWN = "UNKNOWN"


class Volatility(str, Enum):
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class MTFAlignment(str, Enum):
    ALIGNED = "ALIGNED"        # context + structure + execution agree
    CONFLICT = "CONFLICT"      # context opposes execution
    TRANSITION = "TRANSITION"  # context vs structure disagree with a recent change
    MIXED = "MIXED"            # no clean agreement, no clean conflict
    UNKNOWN = "UNKNOWN"


class NoTradeReason(str, Enum):
    CONFLICTING_TIMEFRAME = "conflicting_timeframe"
    WEAK_STRUCTURE = "weak_structure"
    SETUP_INCOMPLETE = "setup_incomplete"
    INSUFFICIENT_RR = "insufficient_rr"
    EXCESSIVE_VOLATILITY = "excessive_volatility"
    TARGET_TOO_CLOSE = "target_too_close"
    STALE_DATA = "stale_data"
    INVALID_SESSION = "invalid_session"
    CONFLICTING_SETUPS = "conflicting_setups"
    INSUFFICIENT_CONFLUENCE = "insufficient_confluence"
    AGAINST_STRUCTURE = "against_structure"
    DUPLICATE_ACTIVE_TRADE = "duplicate_active_trade"
    # Phase 15: real trade management
    ACTIVE_TRADE = "active_trade"           # symbol already has an open PaperTrade
    COOLDOWN = "cooldown"                    # symbol in post-loss cooldown window
    STOP_TOO_TIGHT = "stop_too_tight"        # |entry-stop| < min_stop_atr_mult * ATR
    STOP_TOO_WIDE = "stop_too_wide"          # |entry-stop| > max_stop_atr_mult * ATR
    TOO_LATE = "too_late_in_session"         # RTH entry after last_entry_time
    INVALID_TRADE_PLAN = "invalid_trade_plan"  # entry/stop/tp inconsistent
    DUPLICATE = "duplicate"                  # same signal_id already processed
    NEWS = "news_event"                      # inside a high-impact news window
    AGAINST_TREND = "against_trend"          # trend_filter: buy under VWAP / sell over it
    LEVEL_IN_THE_WAY = "level_in_the_way"    # clear_path: support/resistance before the target
    LATE_ENTRY = "late_entry"                # max_chase_r: price already ran from the zone
    CHOPPY = "choppy_market"                 # chop_min_er: the market goes back and forth


def _iso(ts) -> str | None:
    return ts.isoformat() if ts is not None else None


@dataclass(frozen=True)
class RegimeState:
    regime: MarketRegime
    volatility: Volatility
    trend_strength: float                 # signed, ~[-1,1]: + up, - down, 0 flat
    atr_percentile: float | None          # 0..1, current ATR vs recent distribution
    ema_fast: float | None
    ema_slow: float | None
    close: float | None
    detail: str = ""
    decision_time: pd.Timestamp | None = None

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "volatility": self.volatility.value,
            "trend_strength": round(self.trend_strength, 4),
            "atr_percentile": (round(self.atr_percentile, 4)
                               if self.atr_percentile is not None else None),
            "detail": self.detail,
            "decision_time": _iso(self.decision_time),
        }


@dataclass(frozen=True)
class TimeframeBias:
    timeframe: str
    bias: Bias
    detail: str = ""

    def to_dict(self) -> dict:
        return {"timeframe": self.timeframe, "bias": self.bias.value, "detail": self.detail}


@dataclass(frozen=True)
class MTFContext:
    context: TimeframeBias        # HTF (e.g. 1H)
    structure: TimeframeBias      # e.g. 15m
    execution: TimeframeBias      # e.g. 5m
    alignment: MTFAlignment

    def favors(self, direction: str) -> bool:
        want = Bias.BULLISH if direction == "LONG" else Bias.BEARISH
        return self.context.bias is want

    def conflicts_with(self, direction: str) -> bool:
        opp = Bias.BEARISH if direction == "LONG" else Bias.BULLISH
        return self.context.bias is opp

    def to_dict(self) -> dict:
        return {
            "context": self.context.to_dict(),
            "structure": self.structure.to_dict(),
            "execution": self.execution.to_dict(),
            "alignment": self.alignment.value,
        }


@dataclass(frozen=True)
class StructureState:
    bias: Bias
    changed_recently: bool          # a CHoCH within the recent window
    last_event_type: str | None     # "BOS" / "CHOCH"
    last_event_direction: Bias | None
    swing_summary: str = ""

    def aligns_with(self, direction: str) -> bool:
        want = Bias.BULLISH if direction == "LONG" else Bias.BEARISH
        return self.bias is want

    def opposes(self, direction: str) -> bool:
        opp = Bias.BEARISH if direction == "LONG" else Bias.BULLISH
        return self.bias is opp

    def to_dict(self) -> dict:
        return {
            "bias": self.bias.value,
            "changed_recently": self.changed_recently,
            "last_event_type": self.last_event_type,
            "last_event_direction": (self.last_event_direction.value
                                     if self.last_event_direction else None),
            "swing_summary": self.swing_summary,
        }


@dataclass(frozen=True)
class KeyLevel:
    price: float
    kind: str                # e.g. "prev_day_high", "session_low", "swing_high", "equal_highs"
    timeframe: str
    side: str                # "buy-side" (above) / "sell-side" (below) / "pivot"
    created_at: pd.Timestamp | None = None
    active: bool = True
    distance: float | None = None    # signed distance from current price (points)
    touched: bool = False
    swept: bool = False

    def to_dict(self) -> dict:
        return {
            "price": round(self.price, 4), "kind": self.kind, "timeframe": self.timeframe,
            "side": self.side, "created_at": _iso(self.created_at), "active": self.active,
            "distance": (round(self.distance, 4) if self.distance is not None else None),
            "touched": self.touched, "swept": self.swept,
        }


@dataclass(frozen=True)
class LiquidityState:
    last_sweep_direction: Bias | None       # BULLISH=buy-side swept, BEARISH=sell-side
    last_sweep_price: float | None
    last_sweep_time: pd.Timestamp | None
    reclaimed: bool                          # did price reclaim the swept level?
    next_target_up: float | None             # nearest un-swept buy-side liquidity above
    next_target_down: float | None           # nearest un-swept sell-side liquidity below
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "last_sweep_direction": (self.last_sweep_direction.value
                                     if self.last_sweep_direction else None),
            "last_sweep_price": self.last_sweep_price,
            "last_sweep_time": _iso(self.last_sweep_time),
            "reclaimed": self.reclaimed,
            "next_target_up": self.next_target_up,
            "next_target_down": self.next_target_down,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class MarketState:
    symbol: str
    decision_time: pd.Timestamp
    session: str
    price: float | None
    vwap: float | None
    vwap_side: str                 # "ABOVE" / "BELOW" / "AT" / "UNKNOWN"
    regime: RegimeState
    mtf: MTFContext
    structure: StructureState
    liquidity: LiquidityState
    key_levels: list[KeyLevel] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "decision_time": _iso(self.decision_time),
            "session": self.session,
            "price": self.price,
            "vwap": self.vwap,
            "vwap_side": self.vwap_side,
            "regime": self.regime.to_dict(),
            "mtf": self.mtf.to_dict(),
            "structure": self.structure.to_dict(),
            "liquidity": self.liquidity.to_dict(),
            "key_levels": [k.to_dict() for k in self.key_levels],
        }


@dataclass(frozen=True)
class ConfluenceContribution:
    name: str
    points: float
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "points": round(self.points, 2), "detail": self.detail}


@dataclass(frozen=True)
class ConfluenceResult:
    direction: str                 # "LONG" / "SHORT"
    score: float
    threshold: float | None        # None = no gate configured
    passed: bool
    contributions: list[ConfluenceContribution] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "score": round(self.score, 2),
            "threshold": self.threshold,
            "passed": self.passed,
            "contributions": [c.to_dict() for c in self.contributions],
        }


@dataclass(frozen=True)
class GhostDecision:
    action: str                    # "LONG" / "SHORT" / "NO_TRADE"
    instrument: str
    setup: str | None
    timestamp: pd.Timestamp
    market_state: MarketState
    confluence: ConfluenceResult | None = None
    signal: object | None = None            # the underlying Signal (if a trade)
    no_trade_reasons: list[NoTradeReason] = field(default_factory=list)
    detail: str = ""
    # Regime-adaptive target decision (ghost/targets.py), when one was made.
    target_plan: dict | None = None

    @property
    def is_trade(self) -> bool:
        return self.action in ("LONG", "SHORT")

    def to_dict(self) -> dict:
        sig = None
        if self.signal is not None:
            s = self.signal
            sig = {
                "signal_id": getattr(s, "signal_id", None),
                "direction": getattr(getattr(s, "direction", None), "value", None),
                "entry": getattr(s, "entry", None),
                "stop": getattr(s, "stop", None),
                "take_profits": list(getattr(s, "take_profits", []) or []),
                "risk_reward": list(getattr(s, "risk_reward", []) or []),
                "invalidation_price": getattr(s, "invalidation_price", None),
                "reasons": list(getattr(s, "reasons", []) or []),
            }
        return {
            "action": self.action,
            "instrument": self.instrument,
            "setup": self.setup,
            "timestamp": _iso(self.timestamp),
            "confluence": self.confluence.to_dict() if self.confluence else None,
            "signal": sig,
            "no_trade_reasons": [r.value for r in self.no_trade_reasons],
            "detail": self.detail,
            "market_state": self.market_state.to_dict(),
        }
