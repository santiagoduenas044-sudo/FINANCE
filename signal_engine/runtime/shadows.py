"""Parallel paper variants ("shadows") of each bot, read from config/shadows.yaml.

A GHOST shadow is a second, silent BotRuntime with its own strategy config and
its own paper tracker (data_store/shadows/<id>/). The live runtime hands it
the same bars after each of its own cycles, so both judge exactly the same
market. It never touches the real record; its entries and exits go to
Discord as "Set up N" notes (see `trade_events`). ORBIT's shadows are extra
APERTURA rule sets run by the ORBIT process (subbots/orbit.py) the same way.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..config.schema import CONFIG_DIR, EngineConfig

ROOT = Path(__file__).resolve().parents[2]
SHADOWS_FILE = CONFIG_DIR / "shadows.yaml"
SHADOWS_DIR = ROOT / "data_store" / "shadows"

log = logging.getLogger(__name__)


@dataclass
class ShadowSpec:
    id: str
    name: str
    about: str = ""
    strategy: str | None = None          # ghost: config/strategies/<strategy>.yaml ("legacy" = none)
    overrides: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)   # orbit: OrbSettings changes
    version: str | None = None           # the bot version it reproduces (versions.py)
    enabled: bool = True                 # default; the dashboard switch overrides it
    setup: int | None = None             # its number: "Set up 3"
    retired: bool = False                # retired: never runs again; its record stays

    @property
    def code(self) -> str:
        return str(self.name).split(" · ")[0]

    @property
    def label(self) -> str:
        """"Set up 3 · FVG 1 DÍA": the short name used everywhere."""
        return f"Set up {self.setup} · {self.code}" if self.setup else self.code


def alerts_on() -> bool:
    """Discord alerts for the set ups' entries and exits (BOT_AVISOS_SETUPS=off)."""
    return os.environ.get("BOT_AVISOS_SETUPS", "on").strip().lower() not in ("off", "0", "false")


# --------------------------------------------------------------------------- #
# on / off (the dashboard's switches), kept in data_store/shadows/enabled.json
# --------------------------------------------------------------------------- #
def _state_file(base: Path) -> Path:
    return Path(base) / "enabled.json"


def switches(base: Path = SHADOWS_DIR) -> dict:
    try:
        return json.loads(_state_file(base).read_text())
    except (OSError, ValueError):
        return {}


def is_on(bot: str, spec: ShadowSpec, base: Path = SHADOWS_DIR) -> bool:
    if spec.retired:
        return False
    return bool(switches(base).get(bot, {}).get(spec.id, spec.enabled))


def main_retired(bot: str, path: Path | None = None) -> bool:
    """A bot's own strategy retired in shadows.yaml (`retired: [orbit]`)."""
    try:
        raw = yaml.safe_load(Path(path or SHADOWS_FILE).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return False
    return bot.lower() in [str(b).lower() for b in raw.get("retired") or []]


def set_on(bot: str, spec_id: str, on: bool, base: Path = SHADOWS_DIR) -> None:
    state = switches(base)
    state.setdefault(bot, {})[spec_id] = bool(on)
    f = _state_file(base)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(f)


def load_specs(bot: str, path: Path = SHADOWS_FILE) -> list[ShadowSpec]:
    """The variants listed for `bot` ("ghost" / "orbit"). A broken entry is
    skipped with a warning; it never stops the bot."""
    try:
        raw = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("shadows.yaml not read (%s)", type(exc).__name__)
        return []
    out = []
    for item in raw.get(bot) or []:
        try:
            out.append(ShadowSpec(**item))
        except TypeError as exc:
            log.warning("shadow entry skipped (%s): %r", exc, item)
    return out


def store_dir(bot: str, spec_id: str, base: Path = SHADOWS_DIR) -> Path:
    return Path(base) / bot / spec_id


def build_ghost_shadow(spec: ShadowSpec, registry, risk_engine=None, base: Path = SHADOWS_DIR):
    """One silent BotRuntime for `spec`, or None when its config is broken."""
    from ..data.engine import DataEngine
    from ..ghost.brain import GhostBrain
    from ..output.dispatcher import OutputDispatcher
    from ..paper.tracker import PaperTradeTracker
    from ..risk.models import AccountState
    from ..setups.engine import SetupEngine
    from ..signals.engine import SignalEngine
    from .engine import BotRuntime
    from .models import BotStatus
    try:
        strategy = None if spec.strategy in (None, "", "legacy") else spec.strategy
        cfg = EngineConfig.load(strategy=strategy, overrides=spec.overrides or None)
        d = store_dir("ghost", spec.id, base)
        d.mkdir(parents=True, exist_ok=True)
        rt = BotRuntime(
            DataEngine(cfg), SetupEngine(cfg, registry), SignalEngine(cfg.signals), registry,
            risk_engine=risk_engine, account=AccountState(equity=100_000.0),
            output_dispatcher=OutputDispatcher([]), live_feeds={},
            signal_log_path=d / "signal_log.jsonl",
            ghost=GhostBrain(cfg.ghost),
            paper_tracker=PaperTradeTracker.from_config(cfg.ghost, store_dir=d / "paper"),
            ghost_log_path=d / "ghost_log.jsonl")
        rt.status = BotStatus.ONLINE
        return rt
    except Exception as exc:  # noqa: BLE001 - a bad variant never stops the bot
        log.warning("shadow %s not started (%s: %s)", spec.id, type(exc).__name__, exc)
        return None


def build_ghost_shadows(registry, risk_engine=None, base: Path = SHADOWS_DIR,
                        path: Path = SHADOWS_FILE) -> list:
    """[(spec, BotRuntime)] for the variants switched on, ready to be fed."""
    out = []
    for spec in load_specs("ghost", path):
        if is_on("ghost", spec, base):
            rt = build_ghost_shadow(spec, registry, risk_engine, base)
            if rt is not None:
                out.append((spec, rt))
    return out


def switch_ghost_shadow(runtime, spec_id: str, on: bool, base: Path = SHADOWS_DIR) -> bool:
    """Turn a GHOST variant on/off while the bot runs (the dashboard switch).
    The list is replaced, never edited in place, so a cycle in progress keeps
    iterating the old one. False when there is no such variant."""
    spec = next((sp for sp in load_specs("ghost") if sp.id == spec_id), None)
    if spec is None:
        return False
    set_on("ghost", spec_id, on, base)
    current = [(sp, rt) for sp, rt in (runtime.shadows or []) if sp.id != spec_id]
    if on:
        rt = build_ghost_shadow(spec, runtime.registry, runtime.risk_engine, base)
        if rt is not None:
            current.append((spec, rt))
    runtime.shadows = current
    return True


def trade_events(tracker, before_open: set, before_closed: int,
                 before_partial: set | None = None) -> list[tuple[str, object]]:
    """[("open"|"partial"|"close", PaperTrade)] that happened in `tracker`
    since the snapshot (open signal ids, number of closed trades, ids that had
    already locked in part). A trade that opened and closed on the same bar
    gives both; "partial" = it closed part of the position on the way."""
    if tracker is None:
        return []
    closed = tracker.closed_trades[before_closed:]
    opened = {t.signal_id: t for t in list(tracker.open_trades) + list(closed)
              if t.signal_id not in before_open}
    partial = [t for t in tracker.open_trades if t.is_open and t.tp1_hit and t.partial_fraction
               and t.signal_id not in (before_partial or set())] if before_partial is not None else []
    return [("open", t) for t in opened.values()] + [("partial", t) for t in partial] + \
        [("close", t) for t in closed]


def _snapshot(tracker) -> tuple[set, int, set]:
    if tracker is None:
        return set(), 0, set()
    return ({t.signal_id for t in tracker.open_trades}, len(tracker.closed_trades),
            {t.signal_id for t in tracker.open_trades if t.tp1_hit})


def run_ghost_shadows(shadows: list, symbol: str, tf_bars: dict, now, decision_time=None,
                      news=None, notify=None) -> None:
    """Feed one closed bar's context to every shadow (the live runtime's
    own bars, so the variants judge the same market). Bars that close after
    `decision_time` (a still-forming 5m/15m candle) are cut, and a variant
    whose own candle has not changed since last time is not run again.
    `notify(spec, rt, kind, trade)` hears every entry and exit; the 1-minute
    bars it judged (or its own candles) are left in `rt._shadow_bars` so the
    message can carry a chart."""
    if decision_time is not None:
        tf_bars = {tf: df[df.index <= decision_time] for tf, df in tf_bars.items()}
    for spec, rt in shadows:
        try:
            exec_tf = rt.data_engine.cfg.timeframe_roles.execution
            bars = tf_bars.get(exec_tf)
            if bars is None or bars.empty:
                continue
            last = bars.index[-1]
            if getattr(rt, "_shadow_last_bar", None) == last:
                continue
            rt._shadow_last_bar = last
            rt.news = news
            rt.now_fn = lambda _now=now: _now
            tracker = getattr(rt, "paper_tracker", None)
            before = _snapshot(tracker)
            rt.run_cycle(symbol, tf_bars)
            one = tf_bars.get("1m")
            rt._shadow_bars = one if one is not None and not one.empty else bars
            if notify is not None:
                for kind, trade in trade_events(tracker, *before):
                    try:
                        notify(spec, rt, kind, trade)
                    except Exception as exc:  # noqa: BLE001 - a message never stops a cycle
                        log.warning("set up %s alert failed (%s)", spec.id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow %s cycle failed (%s)", spec.id, type(exc).__name__)


def _px(x) -> str:
    return f"{x:,.2f}" if x is not None else "—"


def alert_note(label: str, kind: str, trade, pv: dict | None,
               closed: list | None = None) -> tuple[str, str, int]:
    """(title, text, color) of a set up's Discord note when it enters
    ("open") or exits ("close"). `pv`: {"5 MNQ": 10.0} for the dollars;
    `closed`: all its closed trades, for the running record line."""
    from ..output.record import _mark, _n, commission_per_trade, exit_label, usd
    position, per_pt = next(iter((pv or {}).items()), ("", 0.0))
    long = trade.direction == "LONG"
    side = "COMPRA" if long else "VENTA"
    sym = trade.instrument

    def money(pts: float) -> str:
        return f" · {usd(pts, per_pt)}" if per_pt else ""

    if kind == "partial":
        got = abs(trade.tp1 - trade.entry)
        n = int(position.split()[0]) if position and position.split()[0].isdigit() else 0
        k = round(trade.partial_fraction * n) if n else 0
        part = f"Cerró {k} de {n} MNQ" if k else f"Cerró el {trade.partial_fraction:.0%}"
        money_k = f" · **+${got * per_pt * trade.partial_fraction:,.0f}**" if per_pt else ""
        return (f"💰 {label} — aseguró +{got:.2f} pts",
                f"{part} en {_px(trade.tp1)}{money_k}\n"
                f"🛑 Stop de los demás a {_px(trade.stop)}: ya no puede perder\n"
                f"🎯 Siguen a {_px(trade.tp2)}", 0x2ECC71)
    if kind == "open":
        risk = abs(trade.entry - trade.stop)
        target = trade.tp2 if trade.tp2 is not None else trade.tp1
        lines = [f"✅ **Entrada {_px(trade.entry)}**",
                 f"🛑 Stop {_px(trade.stop)} · {risk:.1f} pts{money(-risk)}"]
        if target is not None:
            reward = abs(target - trade.entry)
            lines.append(f"🎯 Objetivo {_px(target)} · {reward:.1f} pts{money(reward)}")
        return (f"{'🟢' if long else '🔴'} {label} — {side} {sym}", "\n".join(lines),
                0x00C851 if long else 0xFF4444)
    pts = trade.pnl_points
    why = exit_label(trade)
    lines = [f"{_px(trade.entry)} → {_px(trade.exit_price)}"
             + (f" · **{usd(pts, per_pt)}**" if per_pt else "")]
    allp = [t.pnl_points for t in closed or []]
    if allp and per_pt:
        wins = sum(1 for p in allp if p > 0.01)
        fee = commission_per_trade(position)
        net = sum(allp) * per_pt - fee * len(allp)
        lines.append(f"Récord: {_n(len(allp))} · ganó {wins} · "
                     f"**{'+' if net >= 0 else '−'}${abs(net):,.0f}**"
                     + (" con comisiones" if fee else ""))
    return (f"{_mark(pts)} {label} — {why} · {pts:+.2f} pts", "\n".join(lines),
            0x2ECC71 if pts > 0.01 else (0x95A5A6 if pts >= -0.01 else 0xE74C3C))
