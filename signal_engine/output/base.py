"""OutputAdapter abstraction. Future adapters (Dashboard, Telegram, TradingView)
implement this same interface, so the core never depends on any one channel.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .events import OutputEvent


class OutputAdapter(ABC):
    name: str

    @abstractmethod
    def emit(self, event: OutputEvent) -> bool:
        """Attempt to publish an event. MUST NOT raise: return True if
        published, False if skipped/failed (disabled, deduped, not configured,
        transport error). Failures never propagate to the core."""
