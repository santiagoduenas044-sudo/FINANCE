"""Output layer (Phase 5.5): adapter-agnostic observability for the Setup Engine.

The core never depends on this package. Adapters (Discord today; Dashboard,
Telegram, TradingView later) implement OutputAdapter and are wired via the
OutputDispatcher.
"""
from __future__ import annotations

from .base import OutputAdapter
from .discord import DiscordOutputAdapter, build_discord_payload, render_fields
from .dispatcher import OutputDispatcher
from .events import DISCLAIMER, TITLE, OutputEvent, dedup_key_for

__all__ = [
    "OutputAdapter", "OutputEvent", "OutputDispatcher",
    "DiscordOutputAdapter", "build_discord_payload", "render_fields",
    "dedup_key_for", "TITLE", "DISCLAIMER",
]
