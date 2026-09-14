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
    candidate: SetupCandidate

    @classmethod
    def from_setup_candidate(cls, candidate: SetupCandidate) -> "OutputEvent":
        return cls(
            kind="setup_candidate",
            dedup_key=dedup_key_for(candidate),
            title=TITLE,
            disclaimer=DISCLAIMER,
            candidate=candidate,
        )
