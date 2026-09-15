"""OutputDispatcher: fans SetupCandidates out to any configured OutputAdapters.

The core (Setup Engine) does not depend on this; it is a thin, optional layer.
A failing adapter never breaks the pipeline — every emit is guarded.
"""
from __future__ import annotations

import logging
from typing import Iterable

from ..config.schema import EngineConfig
from ..setups.models import SetupCandidate
from ..signals.models import Signal
from .base import OutputAdapter
from .discord import DiscordOutputAdapter
from .events import OutputEvent

log = logging.getLogger("signal_engine.output.dispatcher")


class OutputDispatcher:
    def __init__(self, adapters: list[OutputAdapter] | None = None):
        self.adapters = adapters or []

    @classmethod
    def from_config(cls, config: EngineConfig, **adapter_overrides) -> "OutputDispatcher":
        """Build the dispatcher from config. Only enabled adapters are added.
        `adapter_overrides` allows injecting a prebuilt discord adapter (tests)."""
        adapters: list[OutputAdapter] = []
        discord = adapter_overrides.get("discord")
        if discord is not None:
            adapters.append(discord)
        elif config.output.discord.enabled:
            adapters.append(DiscordOutputAdapter(config.output.discord))
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
