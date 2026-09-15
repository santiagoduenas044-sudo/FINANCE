"""Read-only chart snapshot for the Phase 12 interactive dashboard.

This module builds the data the dashboard's candlestick chart renders. It is
STRICTLY read-only observability: it reuses the *exact* engines the bot uses
(the live feed buffer, the Data Engine's as-of AnalysisContext, and the Setup
Engine's own MarketAnalyzer) and never runs setups/signals/risk, never places
an order, and never mutates runtime trading state.

Why this is faithful to the real bot and NOT a mockup:
  * Candles come from `LiveFeed.get_tf_bars()` — the same closed, no-lookahead
    bars the trading cycle consumes.
  * VWAP / EMA / market structure (FVG, BOS/CHoCH, liquidity) are computed by
    `runtime.setup_engine.analyzer` and `signal_engine.features.indicators` —
    the identical causal functions Market Analysis uses. Nothing here is
    reimplemented or faked.
  * Signal overlays and their WIN/LOSS/ACTIVE/INVALIDATED status are derived
    from the persisted signal log + in-memory signals produced by the Signal
    Engine, walked forward against real bars only.

No engine in the protected set (Data / Market Analysis / Setup / Signal / Risk)
is modified — this module only *calls* them.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from ..features import indicators as ind
from ..features.market_structure.models import Direction
from ..runtime.engine import read_signal_log

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..runtime.engine import BotRuntime

# How many closed execution-timeframe candles to show by default.
DEFAULT_CANDLES = 200
_MAX_CANDLES = 600

# Structure objects can accumulate; cap what we ship so the payload stays small
# and the chart stays legible. These are display caps only — analysis is full.
_MAX_LIQUIDITY = 16
_MAX_FVG = 12
_MAX_EVENTS = 20
_MAX_SWINGS = 30
_MAX_SIGNALS = 60


def _series_points(series: pd.Series, index: pd.DatetimeIndex) -> list[dict]:
    """Convert an indicator Series to [{t, v}] over `index`, dropping NaNs."""
    out: list[dict] = []
    if series is None:
        return out
    aligned = series.reindex(index)
    for ts, val in aligned.items():
        if pd.isna(val):
            continue
        out.append({"t": ts.isoformat(), "v": float(val)})
    return out


def _direction_str(d: Direction) -> str:
    return d.value if isinstance(d, Direction) else str(d)


def _iso_or_none(ts) -> str | None:
    return ts.isoformat() if ts is not None else None


def _signal_status(direction: str, entry: float, stop: float,
                   tp1: float | None, invalidation_price: float,
                   future_bars: pd.DataFrame) -> str:
    """Forward-walk CLOSED bars strictly after a signal to classify its outcome.

    Statuses (signal-only bot — nothing is actually executed, this is "what
    would have happened" tracking):
      * WIN         — first take-profit reached before any protective level.
      * LOSS        — the stop was hit before the take-profit.
      * INVALIDATED — a distinct, tighter invalidation level was breached
                      before the stop or target (only when the setup defined
                      one separate from the stop).
      * ACTIVE      — neither target nor protective level touched yet.

    Intrabar tie-break is conservative (protective level wins over target),
    matching `setups.models.update_status`, so we never over-report wins.
    """
    if future_bars is None or future_bars.empty:
        return "ACTIVE"

    long = direction == "LONG"
    has_inval = invalidation_price not in (None, 0, 0.0) and invalidation_price != stop

    for _, row in future_bars.iterrows():
        high = float(row["high"])
        low = float(row["low"])
        hit_stop = (low <= stop) if long else (high >= stop)
        hit_inval = has_inval and ((low <= invalidation_price) if long
                                   else (high >= invalidation_price))
        hit_tp = tp1 is not None and ((high >= tp1) if long else (low <= tp1))

        if hit_inval and not hit_stop:
            return "INVALIDATED"
        if hit_stop:
            return "LOSS"
        if hit_tp:
            return "WIN"
    return "ACTIVE"


def _collect_signals(runtime: "BotRuntime", symbol: str) -> list[dict]:
    """Gather all SIGNAL records for `symbol` (persisted + in-memory), deduped.

    The persisted log is the cross-restart superset; in-memory recent_signals
    cover the current session. Both are already enriched (Phase 12), so either
    source carries the full detail the dashboard needs.
    """
    records: list[dict] = []
    if runtime.signal_log_path is not None:
        records.extend(read_signal_log(runtime.signal_log_path, limit=1000))
    records.extend(runtime.recent_signals_dict(limit=1000))

    seen: set = set()
    out: list[dict] = []
    for rec in records:
        if rec.get("kind") != "SIGNAL" or rec.get("instrument") != symbol:
            continue
        key = rec.get("signal_id") or (rec.get("timestamp"), rec.get("entry"), rec.get("stop"))
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    out.sort(key=lambda r: r.get("timestamp") or "")
    return out


def _annotate_signal_status(records: list[dict], exec_bars: pd.DataFrame) -> None:
    """Attach a live `status` to each signal record, in place."""
    for rec in records:
        try:
            ts = pd.Timestamp(rec["timestamp"])
            if ts.tzinfo is not None and exec_bars.index.tz is not None:
                ts = ts.tz_convert(exec_bars.index.tz)
            future = exec_bars[exec_bars.index > ts]
            tps = rec.get("take_profits") or []
            rec["status"] = _signal_status(
                rec.get("direction", ""),
                float(rec.get("entry", 0.0)),
                float(rec.get("stop", 0.0)),
                float(tps[0]) if tps else None,
                float(rec.get("invalidation_price") or 0.0),
                future,
            )
        except Exception:  # noqa: BLE001 - status is best-effort, never fatal
            rec["status"] = "ACTIVE"


def _empty_chart(symbol: str, timeframe: str, reason: str) -> dict:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "available": False,
        "reason": reason,
        "feed_status": "DISCONNECTED",
        "delayed": True,
        "last_update_at": None,
        "last_bar_time": None,
        "last_price": None,
        "session": None,
        "candles": [],
        "indicators": {"vwap": [], "ema9": [], "ema20": []},
        "structure": {"liquidity": [], "fvg": [], "events": [], "swings": []},
        "signals": [],
    }


def build_chart_data(runtime: "BotRuntime", symbol: str,
                     limit: int = DEFAULT_CANDLES) -> dict:
    """Build the JSON-safe chart payload for `symbol`. Never raises for the
    expected "no data yet" cases — returns an empty-but-valid structure."""
    limit = max(10, min(limit, _MAX_CANDLES))
    execution_tf = runtime.data_engine.cfg.timeframe_roles.execution

    feeds = runtime.live_feeds or {}
    feed = feeds.get(symbol)
    if feed is None:
        return _empty_chart(symbol, execution_tf, f"no data feed configured for {symbol!r}")

    health = feed.health()
    tf_bars = feed.get_tf_bars()
    exec_bars = tf_bars.get(execution_tf)
    if exec_bars is None or exec_bars.empty:
        payload = _empty_chart(symbol, execution_tf, "no bars received yet")
        payload["feed_status"] = health.status.value
        payload["delayed"] = feed.adapter.is_delayed
        payload["last_update_at"] = _iso_or_none(health.last_update_at)
        return payload

    decision_time = exec_bars.index[-1]

    # Same as-of context + analysis the trading cycle would build — read-only.
    ctx = runtime.data_engine.analysis_context(symbol, decision_time, tf_bars)
    analysis = runtime.setup_engine.analyzer.analyze_view(ctx.execution, decision_time)
    full_bars = ctx.execution.bars
    inds = analysis.indicators

    # Visible slice (display only; indicators/structure computed on full history).
    visible = full_bars.tail(limit)
    window_start = visible.index[0]

    candles = [
        {
            "t": ts.isoformat(),
            "o": float(row["open"]), "h": float(row["high"]),
            "l": float(row["low"]), "c": float(row["close"]),
            "v": float(row["volume"]),
        }
        for ts, row in visible.iterrows()
    ]

    # VWAP + EMA20 come straight from the analysis; EMA9 uses the same causal
    # ema() function (compute_indicators defaults to 20/50, so 9 is added here).
    vwap_series = inds["vwap"] if "vwap" in inds.columns else ind.vwap(full_bars)
    ema20_series = inds["ema_20"] if "ema_20" in inds.columns else ind.ema(full_bars["close"], 20)
    ema9_series = ind.ema(full_bars["close"], 9)

    indicators = {
        "vwap": _series_points(vwap_series, visible.index),
        "ema9": _series_points(ema9_series, visible.index),
        "ema20": _series_points(ema20_series, visible.index),
    }

    liquidity = [
        {
            "t": _iso_or_none(lv.timestamp),
            "price": float(lv.level),
            "direction": _direction_str(lv.direction),
            "swept_at": _iso_or_none(lv.swept_at),
            "side": "buy-side" if lv.direction is Direction.BULLISH else "sell-side",
        }
        for lv in analysis.liquidity_levels
        if lv.timestamp is None or lv.timestamp >= window_start or lv.swept_at is None
    ][-_MAX_LIQUIDITY:]

    fvg = [
        {
            "t": lv.timestamp.isoformat(),
            "top": float(lv.top), "bottom": float(lv.bottom),
            "direction": _direction_str(lv.direction),
            "mitigated_at": _iso_or_none(lv.mitigated_at),
        }
        for lv in analysis.fair_value_gaps
        if lv.timestamp >= window_start or lv.mitigated_at is None
    ][-_MAX_FVG:]

    events = [
        {
            "t": ev.timestamp.isoformat(),
            "type": ev.type.value,
            "direction": _direction_str(ev.direction),
            "level": float(ev.level),
        }
        for ev in analysis.structure_events
        if ev.timestamp >= window_start
    ][-_MAX_EVENTS:]

    swings = [
        {
            "t": sw.timestamp.isoformat(),
            "price": float(sw.price),
            "type": sw.type.value,
            "label": sw.label.value if sw.label else None,
        }
        for sw in analysis.swings
        if sw.timestamp >= window_start
    ][-_MAX_SWINGS:]

    signals = _collect_signals(runtime, symbol)
    _annotate_signal_status(signals, full_bars)
    signals = signals[-_MAX_SIGNALS:]

    last = ctx.execution.last_closed()
    return {
        "symbol": symbol,
        "timeframe": execution_tf,
        "available": True,
        "reason": None,
        "feed_status": health.status.value,
        "delayed": feed.adapter.is_delayed,
        "last_update_at": _iso_or_none(health.last_update_at),
        "last_bar_time": decision_time.isoformat(),
        "last_price": float(last["close"]) if last is not None else None,
        "session": ctx.session,
        "candles": candles,
        "indicators": indicators,
        "structure": {
            "liquidity": liquidity,
            "fvg": fvg,
            "events": events,
            "swings": swings,
        },
        "signals": signals,
    }
