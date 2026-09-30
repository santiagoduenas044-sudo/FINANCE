"""Parallel paper variants ("shadows") of each bot, read from config/shadows.yaml.

A GHOST shadow is a second, silent BotRuntime with its own strategy config and
its own paper tracker (data_store/shadows/<id>/). The live runtime hands it
the same bars after each of its own cycles, so both judge exactly the same
market. It sends nothing to Discord and never touches the real record.
ORBIT's shadows are extra APERTURA rule sets run by the ORBIT process
(subbots/orbit.py) the same way.
"""
from __future__ import annotations

import json
import logging
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
    return bool(switches(base).get(bot, {}).get(spec.id, spec.enabled))


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


def run_ghost_shadows(shadows: list, symbol: str, tf_bars: dict, now, decision_time=None) -> None:
    """Feed one closed bar's context to every shadow (the live runtime's
    own bars, so the variants judge the same market). Bars that close after
    `decision_time` (a still-forming 5m/15m candle) are cut, and a variant
    whose own candle has not changed since last time is not run again."""
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
            rt.now_fn = lambda _now=now: _now
            rt.run_cycle(symbol, tf_bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow %s cycle failed (%s)", spec.id, type(exc).__name__)
