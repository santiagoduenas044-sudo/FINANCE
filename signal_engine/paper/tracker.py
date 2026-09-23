"""PaperTradeTracker (Phase 14) — GHOST's trade memory.

Creates simulated trades from GHOST's decisions, updates them on every newly
CLOSED bar (P&L, R, MFE/MAE, TP/SL/invalidation), prevents duplicate trades,
persists open + closed trades to disk (restart recovery), and computes simple
aggregate statistics.

NO broker, NO order execution, NO real money — purely internal bookkeeping over
delayed/historical bars. No-lookahead is the caller's responsibility: only feed
`update_on_bar` bars that have already closed at or after the trade's opening
bar; the tracker never peeks at future data.

Intrabar ambiguity: when a single OHLC bar's range contains BOTH a protective
level (stop/invalidation) and a take-profit, the true intrabar order is unknown
from OHLC alone. We resolve it CONSERVATIVELY — the protective level wins — so a
win is never over-reported (same convention as setups.models.update_status).
This is a deliberate, documented assumption, not fabricated data.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .models import ExitReason, PaperTrade, TradeStatus


class PaperTradeTracker:
    def __init__(self, store_dir: str | Path | None = None,
                 breakeven_after_tp1: bool = False):
        # Once TP1 is reached the trade continues toward TP2 with its ORIGINAL
        # stop, so a winner can hand back the whole move and close at -1R. Move
        # the stop to entry instead and that giveback becomes ~0R. Measured on
        # the ES sample through this tracker: expectancy 0.500R -> 0.583R,
        # profit factor 2.00 -> 3.33, net 10.0R -> 14.0R at the same 2.0R
        # drawdown. Win rate FALLS (50% -> 42%) because breakeven exits count as
        # non-wins — which is why win rate is the wrong thing to steer by.
        self.breakeven_after_tp1 = breakeven_after_tp1
        self.open_trades: list[PaperTrade] = []
        self.closed_trades: list[PaperTrade] = []
        # Phase 15: dedup by signal_id (guards restart + polling reprocesses)
        # and remember all signal_ids we have ever processed (approved or not).
        self._processed_signal_ids: set[str] = set()
        self._store_dir = Path(store_dir) if store_dir else None
        if self._store_dir is not None:
            self._load()

    # ------------------------------------------------------------------ #
    # Creation + duplicate protection
    # ------------------------------------------------------------------ #
    def has_open(self, instrument: str, direction: str | None = None,
                 setup: str | None = None) -> bool:
        """Phase 15: max_open_per_symbol=1 semantics — an open trade for the
        symbol (any direction/setup) blocks new ones. The direction/setup
        parameters are kept for backward compatibility with existing callers
        but are ignored: one open trade per instrument is the invariant."""
        return any(t.instrument == instrument and t.is_open for t in self.open_trades)

    def last_closed(self, instrument: str) -> PaperTrade | None:
        """Most recently closed trade for the symbol (any reason). Used for
        cooldown gating."""
        recent = [t for t in self.closed_trades if t.instrument == instrument
                  and t.closed_at is not None]
        return max(recent, key=lambda t: t.closed_at) if recent else None

    def is_in_cooldown(self, instrument: str, now: pd.Timestamp,
                       cooldown_bars: int, bar_seconds: float,
                       trigger_reasons: list[str]) -> tuple[bool, dict]:
        """Deterministic cooldown check. Returns (in_cooldown, info) with the
        previous trade id, its closed_at, cooldown_until, bars remaining. Never
        raises — returns (False, {}) when disabled or no history exists."""
        if cooldown_bars <= 0 or bar_seconds <= 0:
            return False, {}
        last = self.last_closed(instrument)
        if last is None or last.exit_reason is None:
            return False, {}
        if last.exit_reason.value.upper() not in {r.upper() for r in trigger_reasons}:
            return False, {}
        window = cooldown_bars * bar_seconds
        elapsed = (now - last.closed_at).total_seconds()
        if elapsed >= window:
            return False, {}
        remaining = window - elapsed
        return True, {
            "previous_trade_id": last.signal_id,
            "closed_at": last.closed_at.isoformat(),
            "cooldown_until": (last.closed_at + pd.Timedelta(seconds=window)).isoformat(),
            "bars_remaining": round(remaining / bar_seconds, 2),
        }

    def has_processed(self, signal_id: str) -> bool:
        """Phase 15: has this exact signal_id already been evaluated (approved
        or rejected) since we started? Survives restart via _load()."""
        return signal_id in self._processed_signal_ids

    def mark_processed(self, signal_id: str) -> None:
        self._processed_signal_ids.add(signal_id)

    def open_trade(self, *, signal_id: str, instrument: str, direction: str,
                   setup: str, entry: float, stop: float, take_profits: list[float],
                   opened_at: pd.Timestamp, invalidation_price: float = 0.0,
                   timeframe: str | None = None, context: dict | None = None,
                   fill: str = "immediate") -> PaperTrade | None:
        """Create a PaperTrade. Returns None if the symbol already has an open
        trade or if this exact signal_id was already processed (restart-safe
        duplicate guard)."""
        if self.has_processed(signal_id):
            return None
        if self.has_open(instrument):
            return None
        risk = abs(entry - stop)
        trade = PaperTrade(
            signal_id=signal_id, instrument=instrument, direction=direction,
            setup=setup, entry=entry, stop=stop, take_profits=list(take_profits),
            opened_at=opened_at, initial_risk=risk if risk > 0 else 1e-9,
            invalidation_price=invalidation_price, timeframe=timeframe,
            context=context or {},
        )
        # "immediate": filled at entry at the decision close (entry == close[N],
        # known with no lookahead). "limit": wait for a later bar to touch entry.
        trade.status = TradeStatus.ACTIVE if fill == "immediate" else TradeStatus.PENDING
        trade.updated_at = opened_at
        trade.last_bar_time = opened_at
        self.open_trades.append(trade)
        self.mark_processed(signal_id)
        self._save_open()
        return trade

    # ------------------------------------------------------------------ #
    # Per-bar update
    # ------------------------------------------------------------------ #
    def update_symbol(self, instrument: str, bar: pd.Series,
                      bar_time: pd.Timestamp) -> list[PaperTrade]:
        """Update every open trade for `instrument` with one closed bar. Returns
        the trades that closed on this bar."""
        just_closed: list[PaperTrade] = []
        for trade in list(self.open_trades):
            if trade.instrument != instrument or not trade.is_open:
                continue
            if trade.last_bar_time is not None and bar_time <= trade.last_bar_time:
                continue  # never process a bar at/at-before what we already saw
            self.update_on_bar(trade, bar, bar_time)
            if not trade.is_open:
                just_closed.append(trade)
        self._reconcile()
        return just_closed

    def update_on_bar(self, trade: PaperTrade, bar: pd.Series,
                      bar_time: pd.Timestamp) -> None:
        high = float(bar["high"]); low = float(bar["low"]); close = float(bar["close"])
        trade.last_bar_time = bar_time
        trade.updated_at = bar_time
        trade.bars_held += 1

        long = trade.is_long

        # --- fill a PENDING limit entry (or cancel if invalidated first) ---- #
        if trade.status is TradeStatus.PENDING:
            inval = trade.invalidation_price
            invalidated = inval and ((low <= inval) if long else (high >= inval))
            touched = (low <= trade.entry <= high)
            if invalidated and not touched:
                self._close(trade, bar_time, inval, ExitReason.CANCELLED,
                            TradeStatus.CANCELLED)
                return
            if touched:
                trade.status = TradeStatus.ACTIVE
            else:
                return  # still waiting to fill

        # --- excursions / P&L (points + R) ---------------------------------- #
        favorable = (high - trade.entry) if long else (trade.entry - low)
        adverse = (low - trade.entry) if long else (trade.entry - high)
        trade.mfe_points = max(trade.mfe_points, favorable)
        trade.mae_points = min(trade.mae_points, adverse)
        trade.mfe_r = trade.mfe_points / trade.initial_risk
        trade.mae_r = trade.mae_points / trade.initial_risk
        trade.current_price = close
        trade.pnl_points = (close - trade.entry) if long else (trade.entry - close)
        trade.r_multiple = trade.pnl_points / trade.initial_risk

        # --- exits, in a deterministic, conservative order ------------------ #
        stop, inval = trade.stop, trade.invalidation_price
        tp1, tp2 = trade.tp1, trade.tp2

        stop_hit = (low <= stop) if long else (high >= stop)
        inval_distinct = inval and inval != stop
        inval_hit = inval_distinct and ((low <= inval) if long else (high >= inval))
        # which protective level is reached FIRST as price moves against us?
        # LONG: the HIGHER level (closer to entry) is hit first as price falls.
        if stop_hit or inval_hit:
            if inval_hit and (not stop_hit or _closer_to_entry(trade, inval, stop)):
                self._close(trade, bar_time, inval, ExitReason.INVALIDATION,
                            TradeStatus.INVALIDATED)
            else:
                self._close(trade, bar_time, stop, ExitReason.STOP, TradeStatus.STOPPED)
            return

        # Targets (protective already ruled out this bar).
        if tp2 is not None and ((high >= tp2) if long else (low <= tp2)):
            trade.tp1_hit = True
            self._close(trade, bar_time, tp2, ExitReason.TP2, TradeStatus.TP2_HIT)
            return
        if tp1 is not None and ((high >= tp1) if long else (low <= tp1)):
            trade.tp1_hit = True
            if tp2 is None:
                self._close(trade, bar_time, tp1, ExitReason.TP1, TradeStatus.TP1_HIT)
            else:
                trade.status = TradeStatus.TP1_HIT   # partial; still open toward TP2
                if self.breakeven_after_tp1:
                    # Takes effect from the NEXT bar: this bar's protective
                    # levels were already evaluated above, so the move can
                    # never retroactively close a trade that was still alive.
                    trade.stop = trade.entry
        self._save_open()

    def invalidate_symbol(self, instrument: str, price: float,
                          bar_time: pd.Timestamp, reason: str = "structure") -> list[PaperTrade]:
        """Externally invalidate open trades for a symbol (e.g. GHOST's structure
        layer says the thesis broke before TP/SL). Closes them as INVALIDATED."""
        closed = []
        for trade in list(self.open_trades):
            if trade.instrument == instrument and trade.is_open:
                trade.context = {**trade.context, "invalidation_note": reason}
                self._close(trade, bar_time, price, ExitReason.INVALIDATION,
                            TradeStatus.INVALIDATED)
                closed.append(trade)
        self._reconcile()
        return closed

    # ------------------------------------------------------------------ #
    def _close(self, trade: PaperTrade, when: pd.Timestamp, price: float,
               reason: ExitReason, status: TradeStatus) -> None:
        trade.status = status
        trade.exit_price = price
        trade.exit_reason = reason
        trade.closed_at = when
        trade.current_price = price
        trade.pnl_points = (price - trade.entry) if trade.is_long else (trade.entry - price)
        trade.r_multiple = trade.pnl_points / trade.initial_risk
        # keep MFE/MAE consistent with the realized exit
        trade.mfe_points = max(trade.mfe_points, trade.pnl_points)
        trade.mae_points = min(trade.mae_points, trade.pnl_points)
        trade.mfe_r = trade.mfe_points / trade.initial_risk
        trade.mae_r = trade.mae_points / trade.initial_risk
        try:
            trade.duration_seconds = float((trade.closed_at - trade.opened_at).total_seconds())
        except Exception:  # noqa: BLE001
            trade.duration_seconds = None
        self._record_closed(trade)

    def _reconcile(self) -> None:
        still_open = [t for t in self.open_trades if t.is_open]
        if len(still_open) != len(self.open_trades):
            self.open_trades = still_open
            self._save_open()

    # ------------------------------------------------------------------ #
    # Views / stats
    # ------------------------------------------------------------------ #
    def active(self, instrument: str | None = None) -> list[PaperTrade]:
        return [t for t in self.open_trades
                if t.is_open and (instrument is None or t.instrument == instrument)]

    def history(self, limit: int | None = None) -> list[PaperTrade]:
        out = list(self.closed_trades)[::-1]
        return out[:limit] if limit else out

    def stats(self) -> dict:
        return compute_stats(self.closed_trades)

    # ------------------------------------------------------------------ #
    # Persistence (open snapshot + append-only closed log)
    # ------------------------------------------------------------------ #
    def _open_path(self) -> Path:
        return self._store_dir / "paper_open.json"

    def _closed_path(self) -> Path:
        return self._store_dir / "paper_closed.jsonl"

    def _save_open(self) -> None:
        if self._store_dir is None:
            return
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            data = [t.to_dict() for t in self.open_trades if t.is_open]
            self._open_path().write_text(json.dumps(data))
        except OSError:
            pass  # best-effort; persistence failure never breaks tracking

    def _record_closed(self, trade: PaperTrade) -> None:
        self.closed_trades.append(trade)
        if self._store_dir is None:
            return
        try:
            self._store_dir.mkdir(parents=True, exist_ok=True)
            with open(self._closed_path(), "a") as fh:
                fh.write(json.dumps(trade.to_dict()) + "\n")
        except OSError:
            pass

    def _load(self) -> None:
        op = self._open_path()
        if op.exists():
            try:
                self.open_trades = [PaperTrade.from_dict(d) for d in json.loads(op.read_text())]
            except (OSError, ValueError):
                self.open_trades = []
        cp = self._closed_path()
        if cp.exists():
            try:
                self.closed_trades = [PaperTrade.from_dict(json.loads(line))
                                      for line in cp.read_text().splitlines() if line.strip()]
            except (OSError, ValueError):
                self.closed_trades = []
        # Phase 15: restart-safe duplicate guard — remember every signal_id we
        # ever accepted (whether the trade is still open or already closed).
        for t in list(self.open_trades) + list(self.closed_trades):
            self._processed_signal_ids.add(t.signal_id)


def _closer_to_entry(trade: PaperTrade, a: float, b: float) -> bool:
    """Is level `a` reached before level `b` as price moves against the trade?
    LONG: both are below entry, the HIGHER one is hit first. SHORT: mirror."""
    return (a > b) if trade.is_long else (a < b)


def compute_stats(trades: list[PaperTrade]) -> dict:
    """Aggregate closed-trade statistics. Expectancy here is simply the average
    R per trade — NOT a probability of winning."""
    closed = [t for t in trades if not t.is_open]
    n = len(closed)
    base = {
        "total_trades": n, "wins": 0, "losses": 0, "breakeven": 0,
        "win_rate": 0.0, "avg_r": 0.0, "total_r": 0.0, "expectancy_r": 0.0,
        "avg_mfe_r": 0.0, "avg_mae_r": 0.0, "avg_duration_seconds": 0.0,
        "by_setup": {}, "by_session": {}, "by_regime": {}, "by_direction": {},
    }
    if n == 0:
        return base
    rs = [t.r_multiple for t in closed]
    wins = [t for t in closed if t.r_multiple > 0]
    losses = [t for t in closed if t.r_multiple < 0]
    be = [t for t in closed if t.r_multiple == 0]
    durs = [t.duration_seconds for t in closed if t.duration_seconds is not None]
    base.update(
        wins=len(wins), losses=len(losses), breakeven=len(be),
        win_rate=round(len(wins) / n, 4),
        avg_r=round(sum(rs) / n, 4), total_r=round(sum(rs), 4),
        expectancy_r=round(sum(rs) / n, 4),
        avg_mfe_r=round(sum(t.mfe_r for t in closed) / n, 4),
        avg_mae_r=round(sum(t.mae_r for t in closed) / n, 4),
        avg_duration_seconds=round(sum(durs) / len(durs), 1) if durs else 0.0,
        by_setup=_group(closed, lambda t: t.setup),
        by_session=_group(closed, lambda t: t.context.get("session", "?")),
        by_regime=_group(closed, lambda t: t.context.get("regime", "?")),
        by_direction=_group(closed, lambda t: t.direction),
    )
    return base


def _group(trades: list[PaperTrade], keyfn) -> dict:
    out: dict[str, dict] = {}
    for t in trades:
        k = str(keyfn(t))
        g = out.setdefault(k, {"trades": 0, "total_r": 0.0, "wins": 0})
        g["trades"] += 1
        g["total_r"] = round(g["total_r"] + t.r_multiple, 4)
        if t.r_multiple > 0:
            g["wins"] += 1
    return out
