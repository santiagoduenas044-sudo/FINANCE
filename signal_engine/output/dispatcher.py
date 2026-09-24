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
        raw = os.environ.get("BOT_DISCORD_ENABLED", "")
        enabled = env_flag if env_flag is not None else config.output.discord.enabled
        webhook_env = config.output.discord.webhook_env
        webhook_present = bool(os.environ.get(webhook_env))
        # Phase 15c: log the decision path (never the webhook itself) so
        # startup log shows exactly why Discord was or wasn't attached.
        log.info("Discord dispatcher decision: env_flag_raw=%r env_flag=%s "
                 "yaml_enabled=%s enabled=%s ${%s}_present=%s",
                 raw, env_flag, config.output.discord.enabled, enabled,
                 webhook_env, webhook_present)
        if discord is not None:
            adapters.append(discord)
        elif enabled:
            if not webhook_present:
                log.warning("Discord enabled but $%s is not set — the adapter "
                            "will be attached but emit() will skip until the "
                            "webhook is provided.", webhook_env)
            # If the env flag overrode a YAML `enabled: false`, pass a shallow
            # copy with enabled=True so the adapter's own guard doesn't re-drop.
            dc = config.output.discord
            if not dc.enabled:
                dc = dc.model_copy(update={"enabled": True})
            adapters.append(DiscordOutputAdapter(dc))
            log.info("Discord adapter attached.")
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

    def publish_trade_opened(self, paper_trade_dict: dict) -> None:
        """Send a rich 'Trade Opened' notification (GHOST-approved paper trade).
        Uses the trade's signal_id as dedup_key — one notification per trade."""
        self._emit(OutputEvent.from_trade_opened(paper_trade_dict))

    def publish_trade_closed(self, paper_trade_dict: dict) -> None:
        """Realised outcome of a paper trade (TP/stop/invalidation)."""
        self._emit(OutputEvent.from_trade_closed(paper_trade_dict))

    def publish_trade_progress(self, paper_trade_dict: dict,
                               milestone_r: float) -> None:
        """An open trade crossed an R milestone."""
        self._emit(OutputEvent.from_trade_progress(paper_trade_dict, milestone_r))

    def publish_trade_idea(self, idea_event: dict) -> None:
        """Zone-strategy heads-up ("get ready") or its cancellation."""
        self._emit(OutputEvent.from_trade_idea(idea_event))

    def publish_trade_tp1(self, paper_trade_dict: dict) -> None:
        """TP1 reached; stop moved to breakeven."""
        self._emit(OutputEvent.from_trade_tp1(paper_trade_dict))

    def publish_status_digest(self, payload: dict, timestamp) -> None:
        """Periodic 'where we stand' summary for remote monitoring."""
        self._emit(OutputEvent.from_status_digest(payload, timestamp))

    def _emit(self, event: OutputEvent) -> None:
        for adapter in self.adapters:
            try:
                adapter.emit(event)
            except Exception as exc:            # defensive: adapters shouldn't raise
                log.warning("Output adapter %r raised on %s (%s); ignored.",
                            getattr(adapter, "name", "?"), event.kind,
                            type(exc).__name__)
