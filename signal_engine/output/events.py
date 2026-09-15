"""Generic, adapter-agnostic output events.

An OutputEvent is what the core hands to any OutputAdapter (Discord, Dashboard,
Telegram...). It is NOT specific to Discord. It carries a deterministic
``dedup_key`` derived from the setup's relevant data so the same candidate is
never emitted twice.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..setups.models import SetupCandidate
from ..signals.models import Signal

# Marker text required by Phase 5.5: this is an observation, not a final signal.
DISCLAIMER = "Setup Candidate — not a final trading signal."
TITLE = "🟡 SETUP CANDIDATE"


def dedup_key_for(candidate: SetupCandidate) -> str:
    """Deterministic id from relevant setup data (no randomness):
    instrument + setup_name + timestamp + direction + entry."""
    raw = "|".join([
        candidate.instrument,
        candidate.setup_name,
        candidate.timestamp.isoformat(),
        candidate.direction.value,
        f"{candidate.entry:.10g}",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OutputEvent:
    kind: str
    dedup_key: str
    title: str
    disclaimer: str
    candidate: SetupCandidate | None = None
    # populated only for kind="signal" (Phase 9, definitive Signal Engine output)
    signal: Signal | None = None
    # populated only for kind="runtime_status" (Phase 9) — a plain, JSON-safe
    # dict, never containing secrets (no webhook URLs, no tokens).
    status_payload: dict | None = None

    @classmethod
    def from_setup_candidate(cls, candidate: SetupCandidate) -> "OutputEvent":
        return cls(
            kind="setup_candidate",
            dedup_key=dedup_key_for(candidate),
            title=TITLE,
            disclaimer=DISCLAIMER,
            candidate=candidate,
        )

    @classmethod
    def from_signal(cls, signal: Signal) -> "OutputEvent":
        """A definitive LONG/SHORT Signal (Phase 6) — still not auto-executed;
        the disclaimer says so explicitly."""
        return cls(
            kind="signal",
            dedup_key=signal.signal_id,   # already deterministic (Phase 6)
            title="🟢 SIGNAL",
            disclaimer="Signal for manual execution — not an order; not executed automatically.",
            signal=signal,
        )

    @classmethod
    def from_runtime_status(cls, status: str, detail: str, timestamp) -> "OutputEvent":
        """Bot lifecycle notification (BOT_STARTED, BOT_PAUSED, BOT_STOPPED,
        BOT_ERROR) — a distinct event kind from setup candidates, rendered
        differently by adapters. dedup_key includes the timestamp so each
        lifecycle transition is its own event (no accidental suppression)."""
        raw = f"runtime_status|{status}|{timestamp.isoformat()}|{detail}"
        return cls(
            kind="runtime_status",
            dedup_key=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            title=f"Bot status: {status}",
            disclaimer="Automated runtime notification — not a trading signal.",
            status_payload={"status": status, "detail": detail,
                            "timestamp": timestamp.isoformat()},
        )
