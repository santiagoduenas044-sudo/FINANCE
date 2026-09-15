"""Control/status API (Phase 9) for the Bot Runtime. Read-only observability
plus three validated lifecycle actions (start/pause/stop) — never order
execution. See signal_engine/runtime/engine.py for the actual control logic;
this package only exposes it over HTTP.
"""
from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
