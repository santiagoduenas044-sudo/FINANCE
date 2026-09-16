"""OutputDispatcher: fans SetupCandidates out to any configured OutputAdapters.

The core (Setup Engine) does not depend on this; it is a thin, optional layer.
A failing adapter never breaks the pipeline — every emit is guarded.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

from ..config.schema import EngineConfig
from ..setups.models import SetupCandidate
from ..signals.models import Signal
from .base import OutputAdapter
from .discord import DiscordOutputAdapter
from .events import OutputEvent

log = logging.getLogger("signal_engine.output.dispatcher")


def _env_bool(name: str) -> bool | None:
    """Read a tri-state env override: None (unset) / True / False."""
    v = os.environ.get(name)
    if v is None or v == "":
        return None
    return v.strip().lower() in {"1", "true", "yes", "on"}


class OutputDispatcher:
    def __init__(self, adapters: list[OutputAdapter] | None = None):
        self.adapters = adapters or []

    @classmethod
    def from_config(cls, config: EngineConfig, **adapter_overrides) -> "OutputDispatcher":
        """Build the dispatcher from config. Only enabled adapters are added.
        `adapter_overrides` allows injecting a prebuilt discord adapter (tests).

        Env override (Phase 15b): BOT_DISCORD_ENABLED=true|false takes precedence
        over ``config.output.discord.enabled`` so activation is a single line in
        the .env file (no YAML edit). The webhook still comes from the env var
        the config points to — never hard-coded, never logged.
        """
        adapters: list[OutputAdapter] = []
        discord = adapter_overrides.get("discord")
        env_flag = _env_bool("BOT_DISCORD_ENABLED")
        enabled = env_flag if env_flag is not None else config.output.discord.enabled
        if discord is not None:
            adapters.append(discord)
        elif enabled:
            # If the env flag overrode a YAML `enabled: false`, pass a shallow
            # copy with enabled=True so the adapter's own guard doesn't re-drop.
            dc = config.output.discord
            if not dc.enabled:
                dc = dc.model_copy(update={"enabled": True})
            adapters.append(DiscordOutputAdapter(dc))
        return cls(adapters)

    def publish_candidate(self, candidate: SetupCandidate) -> None:
        event = OutputEvent.from_setup_candidate(candidate)
        for adapter in self.adapters:
            try:
                adapter.emit(event)
            except Exception as exc:            # defensive: adapters shouldn't raise
                log.warning("Output adapter %r raised (%s); ignored.",
                            getattr(adapter, "name", "?"), type(exc).__name__)

    def publish_candidates(self, candidates: Iterable[SetupCandidate]) -> None:
        for c in candidates:
            self.publish_candidate(c)

    def publish_signal(self, signal: Signal) -> None:
        event = OutputEvent.from_signal(signal)
        for adapter in self.adapters:
            try:
                adapter.emit(event)
            except Exception as exc:            # defensive: adapters shouldn't raise
                log.warning("Output adapter %r raised (%s); ignored.",
                            getattr(adapter, "name", "?"), type(exc).__name__)
