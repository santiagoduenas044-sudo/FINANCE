"""BotRuntime (Phase 9): the ONLY thing that decides whether the trading
pipeline actually runs on a given cycle. This is a REAL control, not a UI
toggle — `run_cycle()` checks `self.status` itself and refuses to call
Setup/Signal/Risk when not ONLINE.

Market Data -> Analysis -> Setup -> Signal -> Risk -> Runtime -> Dashboard/Discord

This module does not execute orders, does not connect to a broker, and does
not add AI. It orchestrates existing engines (Phases 3-8) exactly as they
are — no changes to their logic.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import pandas as pd

from ..data.contracts import ContractRegistry
from ..data.engine import DataEngine
from ..output.dispatcher import OutputDispatcher
from ..output.events import OutputEvent
from ..risk.engine import RiskEngine
from ..risk.models import AccountState, RiskOutcome
from ..setups.engine import SetupEngine
from ..signals.engine import SignalEngine
from ..signals.models import NoTrade, Signal
from .models import (
    BotStatus, ComponentHealth, ComponentStatus, CycleResult, InstrumentStatus,
    RuntimeEvent,
)

# Phase 10: optional. BotRuntime works exactly as in Phase 9 (historical
# tf_bars passed explicitly to run_cycle()) when no live_feeds are configured.
from ..live.feed import LiveFeed
from ..live.models import DataFeedStatus

_DEFAULT_MAX_EVENTS = 200
_DEFAULT_MAX_SIGNALS = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class BotRuntime:
    data_engine: DataEngine
    setup_engine: SetupEngine
    signal_engine: SignalEngine
    registry: ContractRegistry
    risk_engine: RiskEngine | None = None
    account: AccountState | None = None
    output_dispatcher: OutputDispatcher | None = None
    live_feeds: dict[str, LiveFeed] | None = None   # Phase 10, optional
    now_fn: Callable[[], datetime] = field(default=_utcnow)

    status: BotStatus = field(default=BotStatus.OFFLINE, init=False)
    started_at: datetime | None = field(default=None, init=False)
    last_heartbeat_at: datetime | None = field(default=None, init=False)
    last_run_at: datetime | None = field(default=None, init=False)
    last_error: str | None = field(default=None, init=False)
    component_health: dict[str, ComponentHealth] = field(default_factory=dict, init=False)
    instrument_status: dict[str, InstrumentStatus] = field(default_factory=dict, init=False)
    recent_signals: deque = field(default_factory=lambda: deque(maxlen=_DEFAULT_MAX_SIGNALS),
                                  init=False)
    events: deque = field(default_factory=lambda: deque(maxlen=_DEFAULT_MAX_EVENTS), init=False)
    trades_today: int = field(default=0, init=False)
    daily_realized_pnl: float = field(default=0.0, init=False)
    data_feed_status: dict[str, DataFeedStatus] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._init_component_health()

    # ------------------------------------------------------------------ #
    # Lifecycle control — the REAL switch
    # ------------------------------------------------------------------ #
    def start(self) -> RuntimeEvent:
        now = self.now_fn()
        self.status = BotStatus.ONLINE
        self.started_at = now
        self.last_error = None
        event = RuntimeEvent(kind="BOT_STARTED", timestamp=now,
                             detail="Bot started; pipeline will run on next cycle().")
        self._log(event)
        self._notify_discord(event)
        return event

    def pause(self) -> RuntimeEvent:
        now = self.now_fn()
        self.status = BotStatus.PAUSED
        event = RuntimeEvent(kind="BOT_PAUSED", timestamp=now,
                             detail="No new signals will be generated while paused.")
        self._log(event)
        self._notify_discord(event)
        return event

    def stop(self) -> RuntimeEvent:
        now = self.now_fn()
        self.status = BotStatus.OFFLINE
        event = RuntimeEvent(kind="BOT_STOPPED", timestamp=now, detail="Bot stopped.")
        self._log(event)
        self._notify_discord(event)
        return event

    def mark_error(self, reason: str) -> RuntimeEvent:
        now = self.now_fn()
        self.status = BotStatus.ERROR
        self.last_error = reason        # never hidden — always visible in status
        event = RuntimeEvent(kind="BOT_ERROR", timestamp=now, detail=reason)
        self._log(event)
        self._notify_discord(event)
        return event

    def heartbeat(self) -> None:
        self.last_heartbeat_at = self.now_fn()

    # ------------------------------------------------------------------ #
    # The actual trading cycle — refuses to run unless ONLINE
    # ------------------------------------------------------------------ #
    def run_cycle(self, symbol: str, tf_bars: dict[str, pd.DataFrame] | None = None) -> CycleResult:
        """`tf_bars`: pass explicitly for historical/backtest-style use
        (Phase 9 behavior, unchanged). Omit it to pull from a configured
        `live_feeds[symbol]` instead (Phase 10) — the feed is polled first,
        so this always reflects the freshest data available right now."""
        now = self.now_fn()
        self.heartbeat()

        if self.status is not BotStatus.ONLINE:
            return CycleResult(ran=False, timestamp=now,
                               reason=f"runtime status is {self.status.value}, not ONLINE")

        used_live_feed = tf_bars is None
        if used_live_feed:
            tf_bars = self._pull_live_bars(symbol)
            if tf_bars is None:
                return CycleResult(ran=False, timestamp=now,
                                   reason=f"no data source configured for {symbol!r}")

        execution_tf = self.data_engine.cfg.timeframe_roles.execution
        bars = tf_bars.get(execution_tf)
        if bars is None or bars.empty:
            self._set_component(ComponentStatus.DOWN, "data", "no bars available")
            return CycleResult(ran=False, timestamp=now, reason="no data available")

        decision_time = bars.index[-1]
        errors: list[str] = []
        signals_produced = no_trades_produced = 0

        try:
            ctx = self.data_engine.analysis_context(symbol, decision_time, tf_bars)
            if not used_live_feed:
                # _pull_live_bars() already set an accurate "data" status
                # (OK/DEGRADED/DOWN) from the feed's real connection health —
                # don't clobber a DISCONNECTED/STALE reading with a blanket OK
                # just because slicing the (possibly stale/last-known) buffer
                # into an AnalysisContext happened to succeed.
                self._set_component(ComponentStatus.OK, "data")
        except Exception as exc:      # noqa: BLE001 — deliberately broad: never crash the runtime
            self._fail_cycle(f"data/context error: {exc}", "data")
            return CycleResult(ran=False, timestamp=now, reason=str(exc), errors=[str(exc)])

        try:
            candidates = self.setup_engine.detect(ctx)
            self._set_component(ComponentStatus.OK, "analysis")
            self._set_component(ComponentStatus.OK, "setup")
        except Exception as exc:      # noqa: BLE001
            self._fail_cycle(f"setup detection error: {exc}", "analysis")
            return CycleResult(ran=False, timestamp=now, reason=str(exc), errors=[str(exc)])

        self._update_instrument_status(symbol, ctx)

        if candidates:
            try:
                results = self.signal_engine.evaluate_candidates(ctx, candidates)
                self._set_component(ComponentStatus.OK, "signal")
            except Exception as exc:  # noqa: BLE001
                self._fail_cycle(f"signal engine error: {exc}", "signal")
                return CycleResult(ran=False, timestamp=now, reason=str(exc), errors=[str(exc)])

            for result in results:
                if isinstance(result, Signal):
                    signals_produced += 1
                    self._handle_signal(result)
                else:
                    no_trades_produced += 1
                    self.recent_signals.append(result)

        self.last_run_at = now
        self._log(RuntimeEvent(kind="CYCLE", timestamp=now,
                               detail=f"signals={signals_produced} no_trades={no_trades_produced}"))
        return CycleResult(ran=True, timestamp=now, signals_produced=signals_produced,
                           no_trades_produced=no_trades_produced, errors=errors)

    def _handle_signal(self, signal: Signal) -> None:
        self.recent_signals.append(signal)
        self.trades_today += 1

        if self.risk_engine is not None and self.account is not None:
            try:
                decision = self.risk_engine.evaluate(signal, self.account)
                status = (ComponentStatus.OK if decision.outcome is RiskOutcome.APPROVED
                         else ComponentStatus.DEGRADED)
                self._set_component(status, "risk",
                                    "" if status is ComponentStatus.OK else decision.reasons[0]
                                    if decision.reasons else "")
            except Exception as exc:  # noqa: BLE001
                self._set_component(ComponentStatus.DOWN, "risk", str(exc))
        else:
            self._set_component(ComponentStatus.DISABLED, "risk", "no RiskEngine configured")

        if self.output_dispatcher is not None:
            try:
                self.output_dispatcher.publish_signal(signal)
                self._update_discord_health()
            except Exception as exc:  # noqa: BLE001
                self._set_component(ComponentStatus.DOWN, "discord", str(exc))

    def _pull_live_bars(self, symbol: str) -> dict[str, pd.DataFrame] | None:
        """Poll the configured LiveFeed for `symbol` and reflect its health
        into component_health/data_feed_status. Returns None if no feed is
        configured for this symbol at all (a genuine setup problem, distinct
        from a feed that IS configured but currently disconnected)."""
        feed = (self.live_feeds or {}).get(symbol)
        if feed is None:
            return None

        feed.poll()
        health = feed.health()
        self.data_feed_status[symbol] = health.status

        if health.status is DataFeedStatus.DISCONNECTED:
            self._set_component(ComponentStatus.DOWN, "data",
                                f"{symbol} feed disconnected: {health.detail}")
        elif health.status is DataFeedStatus.STALE:
            self._set_component(ComponentStatus.DEGRADED, "data",
                                f"{symbol} feed stale (no update since {health.last_update_at})")
        else:
            self._set_component(ComponentStatus.OK, "data")

        return feed.get_tf_bars()

    def _fail_cycle(self, reason: str, component: str) -> None:
        self._set_component(ComponentStatus.DOWN, component, reason)
        self.mark_error(reason)

    # ------------------------------------------------------------------ #
    # Status / health bookkeeping
    # ------------------------------------------------------------------ #
    def _init_component_health(self) -> None:
        for name in ("data", "analysis", "setup", "signal"):
            self.component_health[name] = ComponentHealth(name, ComponentStatus.OK)
        self.component_health["risk"] = ComponentHealth(
            "risk",
            ComponentStatus.OK if self.risk_engine is not None else ComponentStatus.DISABLED,
        )
        self._update_discord_health()

    def _update_discord_health(self) -> None:
        if self.output_dispatcher is None or not self.output_dispatcher.adapters:
            self.component_health["discord"] = ComponentHealth(
                "discord", ComponentStatus.DISABLED,
                "no Discord adapter attached (disabled or not configured)")
            return
        discord_adapter = next(
            (a for a in self.output_dispatcher.adapters if a.name == "discord"), None)
        if discord_adapter is None:
            self.component_health["discord"] = ComponentHealth(
                "discord", ComponentStatus.DISABLED, "Discord adapter not attached")
        elif not discord_adapter.config.enabled:
            self.component_health["discord"] = ComponentHealth(
                "discord", ComponentStatus.DISABLED, "disabled in config")
        elif not discord_adapter.configured:
            self.component_health["discord"] = ComponentHealth(
                "discord", ComponentStatus.DEGRADED, "webhook not configured")
        else:
            self.component_health["discord"] = ComponentHealth("discord", ComponentStatus.OK)

    def _set_component(self, status: ComponentStatus, name: str, detail: str = "") -> None:
        self.component_health[name] = ComponentHealth(name, status, detail)

    def _update_instrument_status(self, symbol: str, ctx) -> None:
        last = ctx.execution.last_closed()
        self.instrument_status[symbol] = InstrumentStatus(
            symbol=symbol,
            last_price=float(last["close"]) if last is not None else None,
            last_bar_time=ctx.execution.bars.index[-1] if not ctx.execution.bars.empty else None,
            session=ctx.session,
        )

    def _log(self, event: RuntimeEvent) -> None:
        self.events.append(event)

    def _notify_discord(self, event: RuntimeEvent) -> None:
        if self.output_dispatcher is None:
            return
        try:
            out_event = OutputEvent.from_runtime_status(event.kind, event.detail, event.timestamp)
            for adapter in self.output_dispatcher.adapters:
                adapter.emit(out_event)
            self._update_discord_health()
        except Exception as exc:      # noqa: BLE001 — a Discord failure never breaks control flow
            self._set_component(ComponentStatus.DOWN, "discord", str(exc))

    # ------------------------------------------------------------------ #
    # JSON-safe snapshot for the API/dashboard — NEVER includes secrets
    # ------------------------------------------------------------------ #
    def to_status_dict(self) -> dict:
        return {
            "status": self.status.value,
            "started_at": _iso(self.started_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "last_run_at": _iso(self.last_run_at),
            "last_error": self.last_error,
            "trades_today": self.trades_today,
            "daily_realized_pnl": self.daily_realized_pnl,
            "components": {
                name: {"status": h.status.value, "detail": h.detail}
                for name, h in self.component_health.items()
            },
            "instruments": {
                sym: {
                    "last_price": s.last_price,
                    "last_bar_time": _iso(s.last_bar_time),
                    "session": s.session,
                }
                for sym, s in self.instrument_status.items()
            },
            "data_feed_status": {
                sym: status.value for sym, status in self.data_feed_status.items()
            },
        }

    def recent_signals_dict(self, limit: int = 20) -> list[dict]:
        out = []
        for r in list(self.recent_signals)[-limit:][::-1]:
            if isinstance(r, Signal):
                out.append({
                    "kind": "SIGNAL", "instrument": r.instrument, "setup": r.setup,
                    "direction": r.direction.value, "timestamp": r.timestamp.isoformat(),
                    "entry": r.entry, "stop": r.stop, "take_profits": r.take_profits,
                })
            elif isinstance(r, NoTrade):
                out.append({
                    "kind": "NO_TRADE", "instrument": r.instrument,
                    "setup": r.setup, "timestamp": r.timestamp.isoformat(),
                    "reasons": r.reasons,
                })
        return out


def _iso(dt: datetime | pd.Timestamp | None) -> str | None:
    return dt.isoformat() if dt is not None else None
