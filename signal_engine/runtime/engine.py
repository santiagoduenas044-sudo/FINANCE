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

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from ..data.contracts import ContractRegistry
from ..data.engine import DataEngine
from ..ghost.narrative import market_read, trade_thesis
from ..output.dispatcher import OutputDispatcher
from ..output.events import OutputEvent
from ..risk.engine import RiskEngine
from ..risk.models import AccountState, RiskOutcome, RiskReasonCode
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

log = logging.getLogger("signal_engine.runtime.engine")

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
    # Phase 11, optional: append-only JSONL audit trail of every Signal/
    # NoTrade produced, so the dashboard's history survives a restart —
    # `recent_signals` (below) stays purely in-memory/transient by design.
    signal_log_path: str | Path | None = None
    # Phase 14, optional: GHOST intelligence brain + paper-trade memory. Both
    # None => runtime behaves exactly as before (no GHOST, no paper trades).
    ghost: object | None = None                    # ghost.brain.GhostBrain
    paper_tracker: object | None = None            # paper.tracker.PaperTradeTracker
    ghost_log_path: str | Path | None = None
    # Optional: heads-up / cancel lifecycle for the zone strategy
    # (runtime/ideas.py). None => no trade-idea messages.
    idea_board: object | None = None

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
    feed_last_update_at: dict[str, datetime] = field(default_factory=dict, init=False)
    # Phase 14: latest GHOST market-state + decision per symbol (JSON-safe), for
    # the dashboard. Populated only when `ghost` is configured.
    ghost_market_state: dict[str, dict] = field(default_factory=dict, init=False)
    ghost_last_decision: dict[str, dict] = field(default_factory=dict, init=False)
    # Plain-language market read per symbol (ghost/narrative.py), for the
    # status digest and the !why command.
    ghost_market_read: dict[str, list[str]] = field(default_factory=dict, init=False)
    # Phase 13: read-only metadata about each symbol's real data source
    # ({symbol: {"source", "delayed", "simulated"}}), so the dashboard can show
    # an honest delayed-vs-simulated distinction. No trading logic reads this.
    feed_info: dict[str, dict] = field(default_factory=dict, init=False)
    # When the last periodic status digest went out (None => none sent yet).
    last_digest_at: datetime | None = field(default=None, init=False)

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

        results: list[Signal | NoTrade] = []
        if candidates:
            try:
                results = self.signal_engine.evaluate_candidates(ctx, candidates)
                self._set_component(ComponentStatus.OK, "signal")
            except Exception as exc:  # noqa: BLE001
                self._fail_cycle(f"signal engine error: {exc}", "signal")
                return CycleResult(ran=False, timestamp=now, reason=str(exc), errors=[str(exc)])

            # Phase 15: when GHOST is enabled it becomes the AUTHORITY —
            # base results do NOT reach Discord / signal log / dashboard until
            # GHOST approves. When GHOST is disabled the legacy path runs
            # exactly as before (Phase 9-13 behaviour, no regression).
            if self.ghost is None:
                for result in results:
                    if isinstance(result, Signal):
                        signals_produced += 1
                        self._handle_signal(result)
                    else:
                        no_trades_produced += 1
                        self.recent_signals.append(result)
                        self._persist_record(result)

        # GHOST runs every cycle (updates active trades even without candidates).
        # When enabled it also drives outputs for APPROVED decisions.
        if self.ghost is not None:
            try:
                approved, rejected = self._run_ghost(symbol, ctx, results)
                signals_produced += approved
                no_trades_produced += rejected
            except Exception as exc:  # noqa: BLE001
                self._set_component(ComponentStatus.DEGRADED, "ghost", str(exc))

        self.last_run_at = now
        self._log(RuntimeEvent(kind="CYCLE", timestamp=now,
                               detail=f"signals={signals_produced} no_trades={no_trades_produced}"))
        return CycleResult(ran=True, timestamp=now, signals_produced=signals_produced,
                           no_trades_produced=no_trades_produced, errors=errors)

    def _handle_signal(self, signal: Signal, *, _skip_risk: bool = False) -> None:
        self.recent_signals.append(signal)
        self._persist_record(signal)
        self.trades_today += 1

        if not _skip_risk:
            if self.risk_engine is not None and self.account is not None:
                try:
                    decision = self.risk_engine.evaluate(signal, self.account)
                    if decision.outcome is RiskOutcome.APPROVED:
                        self._set_component(ComponentStatus.OK, "risk")
                    else:
                        # Fix 4: use a specific, actionable detail for UNVERIFIED
                        if (len(decision.reason_codes) == 1 and
                                decision.reason_codes[0] is RiskReasonCode.UNVERIFIED_CONTRACT_SPEC):
                            self._set_component(
                                ComponentStatus.DEGRADED, "risk",
                                "CME specs unverified — dollar sizing disabled; position sizing inactive",
                            )
                        else:
                            self._set_component(
                                ComponentStatus.DEGRADED, "risk",
                                decision.reasons[0] if decision.reasons else "",
                            )
                except Exception as exc:  # noqa: BLE001
                    self._set_component(ComponentStatus.DOWN, "risk", str(exc))
            else:
                # Fix 4: explain why risk is disabled
                self._set_component(ComponentStatus.DISABLED, "risk",
                                    "no RiskEngine — not wired in bootstrap")

        if self.output_dispatcher is not None:
            try:
                self.output_dispatcher.publish_signal(signal)
                self._update_discord_health()
            except Exception as exc:  # noqa: BLE001
                self._set_component(ComponentStatus.DOWN, "discord", str(exc))

    def _on_trade_opened(self, signal: Signal, paper_trade) -> None:
        """Record-keeping + Discord for a GHOST-approved, paper-trade-opened event.
        Called instead of _handle_signal in the GHOST path so the Discord message
        is a rich 'Trade Opened' embed (with full TradePlan) rather than the
        generic 'Signal' embed used by the non-GHOST path."""
        self.recent_signals.append(signal)
        self._persist_record(signal)
        self.trades_today += 1

        if self.output_dispatcher is not None:
            try:
                log.info("DISCORD SEND signal_id=%.8s… instrument=%s direction=%s",
                         signal.signal_id, signal.instrument,
                         getattr(paper_trade, "direction", "?"))
                self.output_dispatcher.publish_trade_opened(paper_trade.to_dict())
                self._update_discord_health()
            except Exception as exc:            # noqa: BLE001
                log.warning("Discord send failed (%s) signal_id=%.8s…",
                            type(exc).__name__, signal.signal_id)
                self._set_component(ComponentStatus.DOWN, "discord", str(exc))

    def _notify_trade_updates(self, symbol: str, just_closed: list,
                              before_r: dict[str, float],
                              before_tp1: set[str] | None = None) -> None:
        """Tell the operator how a trade is going without them opening the
        dashboard: a close with realised P&L, and an R milestone crossed by a
        still-open trade. Never raises — a notification is not worth a cycle."""
        if self.output_dispatcher is None or self.paper_tracker is None:
            return
        ncfg = self.data_engine.cfg.output.notifications
        try:
            if ncfg.trade_closed:
                for t in just_closed:
                    self.output_dispatcher.publish_trade_closed(t.to_dict())
            if ncfg.trade_progress:
                for t in self.paper_tracker.open_trades:
                    if not t.is_open or t.instrument != symbol:
                        continue
                    prev = before_r.get(t.signal_id)
                    if prev is None:
                        continue   # opened on this bar — the open event covers it
                    for m in _crossed_milestones(prev, t.r_multiple,
                                                 ncfg.progress_milestones_r):
                        self.output_dispatcher.publish_trade_progress(t.to_dict(), m)
            if ncfg.tp1_hit and before_tp1 is not None:
                for t in self.paper_tracker.open_trades:
                    if (t.is_open and t.instrument == symbol and t.tp1_hit
                            and t.signal_id in before_r
                            and t.signal_id not in before_tp1):
                        self.output_dispatcher.publish_trade_tp1(t.to_dict())
            self._update_discord_health()
        except Exception as exc:  # noqa: BLE001
            log.warning("trade notification failed (%s)", type(exc).__name__)

    def maybe_send_status_digest(self, now: datetime | None = None) -> bool:
        """Push a periodic 'where we stand' summary so the operator can confirm
        the bot is alive while away from home. Returns True if one was sent.
        Called from the service tick; the interval lives in config."""
        ncfg = self.data_engine.cfg.output.notifications
        if (self.output_dispatcher is None or not ncfg.status_digest
                or ncfg.status_digest_minutes <= 0):
            return False
        now = now or datetime.now(timezone.utc)
        due = timedelta(minutes=ncfg.status_digest_minutes)
        if self.last_digest_at is not None and now - self.last_digest_at < due:
            return False
        self.last_digest_at = now
        try:
            self.output_dispatcher.publish_status_digest(self.status_digest_payload(), now)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("status digest failed (%s)", type(exc).__name__)
            return False

    def status_digest_payload(self) -> dict:
        """Flat, JSON-safe summary of the day. No secrets — the webhook URL and
        env values never appear here."""
        open_trades = [t for t in (self.paper_tracker.open_trades
                                   if self.paper_tracker else []) if t.is_open]
        closed = list(self.paper_tracker.closed_trades) if self.paper_tracker else []
        wins = [t for t in closed if t.pnl_points > 0]
        open_r = sum(t.r_multiple for t in open_trades) if open_trades else None
        uptime = "—"
        if self.started_at is not None:
            secs = int((datetime.now(timezone.utc) - self.started_at).total_seconds())
            uptime = f"{secs // 3600}h {(secs % 3600) // 60}m"
        return {
            "status": self.status.value,
            "uptime": uptime,
            "feed": self.connection_state(),
            "trades_today": self.trades_today,
            "open_count": len(open_trades),
            "closed_today": len(closed),
            "realised_points": round(self.daily_realized_pnl, 2),
            "win_rate": (f"{len(wins) / len(closed):.0%}" if closed else "—"),
            "open_r": round(open_r, 2) if open_r is not None else None,
            "open_trades": [
                f"{t.instrument} {t.direction} @ {t.entry:.2f} → "
                f"{(t.current_price or t.entry):.2f} "
                f"({t.pnl_points:+.2f} pts, {t.r_multiple:+.2f}R)"
                for t in open_trades
            ],
            "market_read": {sym: list(lines)
                            for sym, lines in self.ghost_market_read.items()},
        }

    def _run_ghost(self, symbol: str, ctx, base_results: list) -> tuple[int, int]:
        """Phase 15 — GHOST is the AUTHORITY when enabled. Flow:

            base results  ->  update active paper trades on this closed bar
                          ->  build MarketState + GHOST decisions
                          ->  APPROVED: run outputs (Discord + persistence via
                              _handle_signal) AND open PaperTrade — same signal_id
                          ->  NO_TRADE: persist decision to ghost_log AND to
                              recent_signals/signal_log so the dashboard shows
                              the filter working; NO Discord, NO PaperTrade.

        Returns (approved_count, rejected_count) so the CycleResult stays accurate.
        """
        # 1) update active paper trades with this decision (closed) bar.
        exec_tf = self.data_engine.cfg.timeframe_roles.execution
        bars = ctx.execution.bars
        # self.ghost.cfg, NOT self.data_engine.cfg.ghost: this function only
        # runs when self.ghost is not None (guarded by the caller), and
        # self.ghost.cfg is what actually drove the decisions below — the two
        # are the same object in the normal bootstrap path, but a caller that
        # builds GhostBrain from its own GhostConfig (as several tests do)
        # would otherwise have every read here — eod_close_enabled included —
        # silently fall back to the data engine's default config instead.
        gcfg = self.ghost.cfg
        if self.paper_tracker is not None and not bars.empty:
            # R reading of every open trade BEFORE this bar is applied, so a
            # milestone is detected as a crossing rather than as "currently
            # above" — a restart therefore never replays an old milestone.
            before_r = {t.signal_id: t.r_multiple
                        for t in self.paper_tracker.open_trades if t.is_open}
            before_tp1 = {t.signal_id for t in self.paper_tracker.open_trades
                          if t.is_open and t.tp1_hit}
            just_closed = []
            try:
                # Fix 3: capture closed trades so account state stays current.
                just_closed = self.paper_tracker.update_symbol(
                    symbol, bars.iloc[-1], ctx.decision_time)
                # Force-flatten anything still open the moment this bar falls
                # outside RTH (data.sessions classifies the 16:00 ET bar itself
                # as ETH/CLOSED, so this fires AT the close using that bar's
                # real price). Runs AFTER update_symbol so a same-bar TP/stop
                # hit is still the real outcome, not overridden by EOD.
                if gcfg.eod_close_enabled and ctx.session != "RTH":
                    eod_closed = self.paper_tracker.close_symbol_eod(
                        symbol, float(bars.iloc[-1]["close"]), ctx.decision_time)
                    just_closed = just_closed + eod_closed
                if just_closed and self.account is not None:
                    _pnl_delta = sum(t.pnl_points for t in just_closed)
                    self.account = AccountState(
                        equity=self.account.equity,
                        trades_today=self.account.trades_today,
                        daily_realized_pnl=self.account.daily_realized_pnl + _pnl_delta,
                        daily_realized_risk=self.account.daily_realized_risk,
                    )
                    self.daily_realized_pnl += _pnl_delta
            except Exception:  # noqa: BLE001 - memory update never breaks the cycle
                pass
            self._notify_trade_updates(symbol, just_closed, before_r, before_tp1)

        # 2) build market state + GHOST decisions (same analysis the setups saw).
        analysis = self.setup_engine.analyzer.analyze(ctx)
        feed_status = self.data_feed_status.get(symbol)
        data_stale = getattr(feed_status, "value", None) == "STALE"

        # Runtime-supplied callbacks (brain stays pure/side-effect-free).
        has_active = (self.paper_tracker.has_open if self.paper_tracker is not None else None)
        cooldown_check = None
        duplicate_check = None
        if self.paper_tracker is not None:
            bar_seconds = _tf_seconds(exec_tf)
            def _cool(inst: str, _pt=self.paper_tracker, _cfg=gcfg, _bs=bar_seconds,
                      _now=ctx.decision_time):
                return _pt.is_in_cooldown(inst, _now, _cfg.cooldown_bars, _bs,
                                          _cfg.cooldown_on_reasons)
            cooldown_check = _cool
            duplicate_check = self.paper_tracker.has_processed

        ms, decisions = self.ghost.evaluate(ctx, analysis, base_results,
                                            data_stale=data_stale,
                                            has_active=has_active,
                                            cooldown_check=cooldown_check,
                                            duplicate_check=duplicate_check)
        self._set_component(ComponentStatus.OK, "ghost")
        self.ghost_market_state[symbol] = ms.to_dict()
        self.ghost_market_read[symbol] = market_read(ms)
        if decisions:
            self.ghost_last_decision[symbol] = decisions[-1].to_dict()

        # Zone strategy: the same per-zone judgement the detector used, for the
        # heads-up / cancel lifecycle. Rejected zone trades cancel their idea.
        zone_states: dict = {}
        zone_rejections: dict[str, str] = {}
        zone_det = (self.setup_engine.zone_detector()
                    if self.idea_board is not None
                    and hasattr(self.setup_engine, "zone_detector") else None)
        if zone_det is not None:
            zone_states = {st.zone.zone_id: st for st in zone_det.zone_states(ctx)}

        def _zone_rejected(d, reasons: list[str]) -> None:
            zid = (d.signal.market_context or {}).get("zone_id") if d.signal else None
            if zid:
                zone_rejections[zid] = _idea_reject_reason(reasons)

        # 3) route each decision. Same signal_id end-to-end.
        log.info("GHOST cycle instrument=%s decisions=%d", symbol, len(decisions))
        approved = rejected = 0
        for d in decisions:
            self._persist_ghost(d)
            log.info("GHOST decision instrument=%s action=%s signal_id=%s",
                     d.instrument, d.action,
                     d.signal.signal_id[:8] + "…" if d.signal else "none")
            if d.is_trade and d.signal is not None:
                # Same-bar race guard: if multiple APPROVED decisions arrive on
                # one bar, only the first actually opens a trade (max 1 per
                # symbol). The rest reclassify as ACTIVE_TRADE so outputs stay
                # consistent (Discord = PaperTrades = final approvals).
                if self.paper_tracker is not None and \
                        self.paper_tracker.has_open(d.signal.instrument):
                    rejected += 1
                    nt = NoTrade(
                        instrument=d.instrument, timestamp=d.timestamp,
                        reasons=["active_trade"], setup=d.setup,
                        timeframe=d.signal.timeframe,
                        candidate_direction=d.signal.direction,
                    )
                    self.recent_signals.append(nt)
                    self._persist_record(nt)
                    self.paper_tracker.mark_processed(d.signal.signal_id)
                    _zone_rejected(d, ["active_trade"])
                    continue
                # Fix 1: Risk gate BEFORE opening the paper trade.
                # UNVERIFIED_CONTRACT_SPEC -> soft WARN, paper trade still opens.
                # Any other rejection reason -> hard block, no paper trade.
                _risk_blocks = False
                _risk_block_codes: list[str] = []
                if self.risk_engine is not None and self.account is not None:
                    _rdec = self.risk_engine.evaluate(d.signal, self.account)
                    if _rdec.outcome is RiskOutcome.REJECTED:
                        _only_unverified = (
                            len(_rdec.reason_codes) == 1
                            and _rdec.reason_codes[0] is RiskReasonCode.UNVERIFIED_CONTRACT_SPEC
                        )
                        if _only_unverified:
                            self._set_component(
                                ComponentStatus.DEGRADED, "risk",
                                "CME specs unverified — dollar sizing disabled; paper trade opened anyway",
                            )
                        else:
                            _risk_blocks = True
                            _risk_block_codes = [r.value for r in _rdec.reason_codes]
                            self._set_component(
                                ComponentStatus.DEGRADED, "risk",
                                _rdec.reasons[0] if _rdec.reasons else str(_risk_block_codes),
                            )
                    else:
                        self._set_component(ComponentStatus.OK, "risk")

                if _risk_blocks:
                    log.info("RISK BLOCKED signal_id=%.8s… codes=%s",
                             d.signal.signal_id, _risk_block_codes)
                    rejected += 1
                    nt = NoTrade(
                        instrument=d.instrument, timestamp=d.timestamp,
                        reasons=["risk_rejected"] + _risk_block_codes,
                        setup=d.setup,
                        timeframe=d.signal.timeframe,
                        candidate_direction=d.signal.direction,
                    )
                    self.recent_signals.append(nt)
                    self._persist_record(nt)
                    if self.paper_tracker is not None:
                        self.paper_tracker.mark_processed(d.signal.signal_id)
                    _zone_rejected(d, ["risk_rejected"] + _risk_block_codes)
                    continue

                # Open the PaperTrade FIRST so it is the source of truth for
                # "this trade actually happened". If the tracker declines
                # (already processed / restart-safe duplicate), Discord and the
                # signal log must NOT fire either — invariant: approved ==
                # Discord == PaperTrades.
                paper_trade = None
                idea_ctx: dict = {}
                zid = (d.signal.market_context or {}).get("zone_id")
                if zone_det is not None and zid:
                    idea = self.idea_board.claim_for_trade(
                        symbol, zone_states.get(zid), zid, d.timestamp)
                    if idea is not None:
                        mc = d.signal.market_context
                        idea_ctx = {
                            "idea_number": idea.number, "idea_direct": idea.direct,
                            "zone_top": mc.get("zone_top"),
                            "zone_bottom": mc.get("zone_bottom"),
                            "target_basis": mc.get("target_basis"),
                            "risk_usd_micro": self._micro_usd(
                                symbol, abs(d.signal.entry - d.signal.stop)),
                        }
                if self.paper_tracker is not None:
                    sig = d.signal
                    paper_trade = self.paper_tracker.open_trade(
                        signal_id=sig.signal_id, instrument=sig.instrument,
                        direction=d.action, setup=sig.setup, entry=sig.entry,
                        stop=sig.stop, take_profits=list(sig.take_profits),
                        opened_at=sig.timestamp,
                        invalidation_price=sig.invalidation_price,
                        timeframe=sig.timeframe,
                        context={
                            "regime": ms.regime.regime.value,
                            "session": ms.session,
                            "mtf": ms.mtf.alignment.value,
                            "confluence": d.confluence.score if d.confluence else None,
                            "thesis": trade_thesis(
                                d, breakeven_after_tp1=gcfg.breakeven_after_tp1,
                                eod_close_enabled=gcfg.eod_close_enabled),
                            **idea_ctx,
                        },
                    )
                if self.paper_tracker is not None and paper_trade is None:
                    # tracker refused (duplicate signal_id after restart, or a
                    # freshly-opened trade on the same symbol earlier this bar)
                    log.info("PAPERTRADE SKIP (dup/active) signal_id=%.8s… instrument=%s",
                             d.signal.signal_id, d.signal.instrument)
                    rejected += 1
                    self.paper_tracker.mark_processed(d.signal.signal_id)
                    continue
                log.info("PAPERTRADE OPEN signal_id=%.8s… instrument=%s direction=%s "
                         "entry=%.2f stop=%.2f",
                         d.signal.signal_id, d.signal.instrument, d.action,
                         d.signal.entry, d.signal.stop)
                approved += 1
                # Fire record-keeping + Discord with rich "Trade Opened" embed.
                self._on_trade_opened(d.signal, paper_trade)
            else:
                rejected += 1
                # A rejected decision is a first-class outcome — record it in
                # recent_signals + signal_log (as an enriched NoTrade) so the
                # dashboard shows GHOST's filter working. No Discord, no paper.
                nt = NoTrade(
                    instrument=d.instrument,
                    timestamp=d.timestamp,
                    reasons=[r.value for r in d.no_trade_reasons] or ["insufficient_confluence"],
                    setup=d.setup,
                    timeframe=(d.signal.timeframe if d.signal else None),
                    candidate_direction=(d.signal.direction if d.signal else None),
                )
                self.recent_signals.append(nt)
                self._persist_record(nt)
                # Mark the base signal_id as processed so a re-poll never
                # reconsiders the same opportunity (restart-safe dedup).
                if d.signal is not None and self.paper_tracker is not None:
                    self.paper_tracker.mark_processed(d.signal.signal_id)
                _zone_rejected(d, list(nt.reasons))

        if zone_det is not None:
            self._review_ideas(symbol, ctx, gcfg, list(zone_states.values()),
                               zone_rejections, data_stale, cooldown_check)
        return approved, rejected

    def _review_ideas(self, symbol, ctx, gcfg, states, rejections,
                      data_stale, cooldown_check) -> None:
        """Announce zones price is approaching and cancel ideas that failed.
        Only announces when a trade could actually follow: inside the entry
        window, no open position, not cooling down, fresh data."""
        hhmm = ctx.decision_time.strftime("%H:%M")
        window_open = (ctx.session in gcfg.valid_sessions
                       and not (gcfg.last_entry_time and hhmm >= gcfg.last_entry_time))
        busy = (self.paper_tracker is not None and self.paper_tracker.has_open(symbol))
        cooling = bool(cooldown_check and cooldown_check(symbol)[0])
        events = self.idea_board.review(
            symbol, states, ctx.decision_time, window_open=window_open,
            can_announce=not (busy or cooling or data_stale), rejections=rejections)
        ncfg = self.data_engine.cfg.output.notifications
        if not events or self.output_dispatcher is None or not ncfg.trade_ideas:
            return
        for ev in events:
            ev["risk_usd_micro"] = self._micro_usd(symbol, ev.get("risk_points") or 0)
            try:
                self.output_dispatcher.publish_trade_idea(ev)
            except Exception as exc:  # noqa: BLE001
                log.warning("trade idea notification failed (%s)", type(exc).__name__)
        self._update_discord_health()

    def _micro_usd(self, symbol: str, points: float) -> float | None:
        """Dollar value of `points` on one micro contract — only from
        VERIFIED contract specs; None otherwise (never a guessed number)."""
        try:
            if not self.registry.verified:
                return None
            for sym in self.registry.symbols():
                spec = self.registry.spec(sym)
                if spec.micro_of == symbol:
                    return round(points * spec.point_value, 2)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _persist_ghost(self, decision) -> None:
        if self.ghost_log_path is None:
            return
        try:
            path = Path(self.ghost_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(_ghost_log_entry(decision)) + "\n")
        except OSError:
            pass

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
        if health.last_update_at is not None:
            self.feed_last_update_at[symbol] = health.last_update_at

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
            "" if self.risk_engine is not None else "no RiskEngine — not wired in bootstrap",
        )
        if self.ghost is not None:
            self.component_health["ghost"] = ComponentHealth("ghost", ComponentStatus.OK)
        self._update_discord_health()

    def _update_discord_health(self) -> None:
        # Phase 15c: when no adapter is attached, report WHY — env flag off,
        # YAML off, or webhook missing — so the dashboard says exactly what to
        # fix. Never returns the webhook itself, only its presence (yes/no).
        if self.output_dispatcher is None or not self.output_dispatcher.adapters:
            import os
            cfg = self.data_engine.cfg.output.discord
            env_raw = os.environ.get("BOT_DISCORD_ENABLED")
            env_flag = None
            if env_raw is not None and env_raw != "":
                env_flag = env_raw.strip().lower() in {"1", "true", "yes", "on"}
            webhook_present = bool(os.environ.get(cfg.webhook_env))
            if env_flag is False:
                detail = f"BOT_DISCORD_ENABLED={env_raw!r} evaluated to false"
            elif env_flag is None and not cfg.enabled:
                detail = ("BOT_DISCORD_ENABLED not set and "
                          "output.discord.enabled=false in config")
            elif not webhook_present:
                detail = f"${cfg.webhook_env} is not set in the process env"
            else:
                # Flag/YAML say enabled, webhook present, but no adapter — an
                # unexpected import/build failure. Point the operator to logs.
                detail = ("adapter not built by dispatcher — check startup logs "
                          "(dashboard.err.log)")
            self.component_health["discord"] = ComponentHealth(
                "discord", ComponentStatus.DISABLED, detail)
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

    def _persist_record(self, result: Signal | NoTrade) -> None:
        """Append one Signal/NoTrade to the on-disk audit log, if configured.
        Best-effort: a disk/permission failure never breaks the trading
        cycle — it only downgrades to an in-memory-only record."""
        if self.signal_log_path is None:
            return
        entry = _record_to_dict(result)
        try:
            path = Path(self.signal_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as fh:
                fh.write(json.dumps(entry) + "\n")
        except OSError as exc:
            self._set_component(ComponentStatus.DEGRADED, "signal_log", type(exc).__name__)

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
    def connection_state(self) -> str:
        """Aggregate feed connectivity, distinct from BotStatus (which stays
        ONLINE while feeds reconnect). One of:
          CONNECTED  — at least one feed is delivering fresh data
          DEGRADED   — feeds present but stale
          DISCONNECTED — feeds configured but none connected
          NONE       — no feeds configured
          CONNECTING — feeds configured but not polled yet
        """
        if not self.live_feeds:
            return "NONE"
        statuses = list(self.data_feed_status.values())
        if not statuses:
            return "CONNECTING"
        values = {s.value for s in statuses}
        if values & {DataFeedStatus.LIVE.value, DataFeedStatus.DELAYED.value}:
            return "CONNECTED"
        if DataFeedStatus.STALE.value in values:
            return "DEGRADED"
        return "DISCONNECTED"

    def to_status_dict(self) -> dict:
        return {
            "status": self.status.value,
            "connection": self.connection_state(),
            "started_at": _iso(self.started_at),
            "last_heartbeat_at": _iso(self.last_heartbeat_at),
            "last_run_at": _iso(self.last_run_at),
            "last_error": self.last_error,
            "trades_today": self.trades_today,
            "daily_realized_pnl": self.daily_realized_pnl,
            "feed_info": self.feed_info,
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
            "feed_last_update_at": {
                sym: _iso(ts) for sym, ts in self.feed_last_update_at.items()
            },
            "ghost": {
                "enabled": self.ghost is not None,
                "active_trades": (len(self.paper_tracker.active())
                                  if self.paper_tracker is not None else 0),
                "regime": {sym: (st.get("regime", {}) or {}).get("regime")
                           for sym, st in self.ghost_market_state.items()},
            },
        }

    def recent_signals_dict(self, limit: int = 20) -> list[dict]:
        return [_record_to_dict(r) for r in list(self.recent_signals)[-limit:][::-1]]


def _json_safe(value):
    """Best-effort recursive conversion to JSON-serialisable primitives.

    Used only for the read-only audit log / dashboard payload — it never
    feeds back into the trading pipeline, so an odd value is coerced to its
    string form rather than being allowed to raise and break persistence."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    try:
        import numpy as _np  # local import: numpy is already a hard dependency
        if isinstance(value, _np.generic):
            return value.item()
    except Exception:  # noqa: BLE001
        pass
    return str(value)


def _level_ref_to_dict(ref) -> dict:
    return {
        "kind": ref.kind,
        "timestamp": _iso(ref.timestamp),
        "price": _json_safe(ref.price),
        "note": ref.note,
    }


def _record_to_dict(r: Signal | NoTrade) -> dict:
    if isinstance(r, Signal):
        return {
            "kind": "SIGNAL", "instrument": r.instrument, "setup": r.setup,
            "direction": r.direction.value, "timestamp": r.timestamp.isoformat(),
            "entry": r.entry, "stop": r.stop, "take_profits": r.take_profits,
            # Phase 12: richer, JSON-safe detail so the dashboard can show the
            # full setup story (reasons, R:R, invalidation, structure used) and
            # so it survives a restart alongside the rest of the log entry.
            "signal_id": r.signal_id,
            "timeframe": r.timeframe,
            "risk_reward": _json_safe(r.risk_reward),
            "reasons": list(r.reasons),
            "invalidation_conditions": list(r.invalidation_conditions),
            "invalidation_price": r.invalidation_price,
            "market_context": _json_safe(r.market_context),
            "referenced": [_level_ref_to_dict(ref) for ref in r.referenced],
        }
    return {
        "kind": "NO_TRADE", "instrument": r.instrument,
        "setup": r.setup, "timestamp": r.timestamp.isoformat(),
        "reasons": r.reasons,
        # Phase 12: keep NoTrade detail symmetric with SIGNAL entries.
        "timeframe": r.timeframe,
        "candidate_direction": r.candidate_direction.value if r.candidate_direction else None,
    }


_TF_SECS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "1h": 3600, "4H": 14400}
_IDEA_REJECT_ES = {
    "active_trade": "ya había otro trade abierto",
    "cooldown": "el bot está en pausa después de un stop",
    "too_late_in_session": "ya es tarde para entrar (después de la hora límite)",
    "invalid_session": "fuera del horario de la sesión",
    "excessive_volatility": "volatilidad excesiva en ese momento",
    "stale_data": "los datos llegaron atrasados",
}


def _idea_reject_reason(reasons: list[str]) -> str:
    for r in reasons:
        if r in _IDEA_REJECT_ES:
            return "reaccionó, pero " + _IDEA_REJECT_ES[r]
    return "reaccionó, pero el filtro de riesgo la descartó (" + ", ".join(reasons) + ")"


def _tf_seconds(tf: str) -> float:
    return float(_TF_SECS.get(tf, 300))


def _crossed_milestones(prev: float, now: float,
                        milestones: list[float]) -> list[float]:
    """R milestones crossed between two readings of the same open trade.

    A positive milestone fires on the way UP through it and a negative one on
    the way DOWN, so a trade hovering around a level announces it once rather
    than on every bar. Returned in the order price passed them."""
    out = [m for m in milestones
           if (m > 0 and prev < m <= now) or (m < 0 and prev > m >= now)]
    return sorted(out, reverse=now < prev)


def _ghost_log_entry(decision) -> dict:
    """Compact, JSON-safe record of a GHOST decision for the audit log."""
    ms = decision.market_state
    return {
        "kind": "GHOST",
        "action": decision.action,
        "instrument": decision.instrument,
        "setup": decision.setup,
        "timestamp": _iso(decision.timestamp),
        "regime": ms.regime.regime.value,
        "session": ms.session,
        "mtf": ms.mtf.alignment.value,
        "score": decision.confluence.score if decision.confluence else None,
        "no_trade_reasons": [r.value for r in decision.no_trade_reasons],
    }


def read_signal_log(path: str | Path, limit: int = 50) -> list[dict]:
    """Read the last `limit` entries from a signal_log_path audit file.
    Returns [] if the file doesn't exist yet (e.g. no signal fired since
    the log was configured) — never raises for that expected case."""
    p = Path(path)
    if not p.exists():
        return []
    lines = p.read_text().splitlines()
    out = []
    for line in lines[-limit:]:
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out[::-1]


def _iso(dt: datetime | pd.Timestamp | None) -> str | None:
    return dt.isoformat() if dt is not None else None
