"""Parallel paper variants ("shadows") of each bot, read from config/shadows.yaml.

A GHOST shadow is a second, silent BotRuntime with its own strategy config and
its own paper tracker (data_store/shadows/<id>/). The live runtime hands it
the same bars after each of its own cycles, so both judge exactly the same
market. It sends nothing to Discord and never touches the real record.
ORBIT's shadows are extra APERTURA rule sets run by the ORBIT process
(subbots/orbit.py) the same way.
"""
from __future__ import annotations

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
    strategy: str | None = None          # ghost: config/strategies/<strategy>.yaml
    overrides: dict = field(default_factory=dict)
    settings: dict = field(default_factory=dict)   # orbit: OrbSettings changes


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


def build_ghost_shadows(registry, risk_engine=None, base: Path = SHADOWS_DIR,
                        path: Path = SHADOWS_FILE) -> list:
    """[(spec, BotRuntime)] ready to be fed by the live runtime."""
    from ..data.engine import DataEngine
    from ..ghost.brain import GhostBrain
    from ..output.dispatcher import OutputDispatcher
    from ..paper.tracker import PaperTradeTracker
    from ..risk.models import AccountState
    from ..setups.engine import SetupEngine
    from ..signals.engine import SignalEngine
    from .engine import BotRuntime
    from .models import BotStatus

    out = []
    for spec in load_specs("ghost", path):
        try:
            cfg = EngineConfig.load(strategy=spec.strategy, overrides=spec.overrides or None)
            d = store_dir("ghost", spec.id, base)
            d.mkdir(parents=True, exist_ok=True)
            de = DataEngine(cfg)
            rt = BotRuntime(
                de, SetupEngine(cfg, registry), SignalEngine(cfg.signals), registry,
                risk_engine=risk_engine, account=AccountState(equity=100_000.0),
                output_dispatcher=OutputDispatcher([]), live_feeds={},
                signal_log_path=d / "signal_log.jsonl",
                ghost=GhostBrain(cfg.ghost),
                paper_tracker=PaperTradeTracker.from_config(cfg.ghost, store_dir=d / "paper"),
                ghost_log_path=d / "ghost_log.jsonl")
            rt.status = BotStatus.ONLINE
            out.append((spec, rt))
        except Exception as exc:  # noqa: BLE001 - a bad variant never stops the bot
            log.warning("shadow %s not started (%s: %s)", spec.id, type(exc).__name__, exc)
    return out


def run_ghost_shadows(shadows: list, symbol: str, tf_bars: dict, now) -> None:
    """Feed one closed bar's context to every shadow (the live runtime's
    own bars, so the variants judge the same market)."""
    for spec, rt in shadows:
        try:
            rt.now_fn = lambda _now=now: _now
            rt.run_cycle(symbol, tf_bars)
        except Exception as exc:  # noqa: BLE001
            log.warning("shadow %s cycle failed (%s)", spec.id, type(exc).__name__)
