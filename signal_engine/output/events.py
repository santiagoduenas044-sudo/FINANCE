"""Generic, adapter-agnostic output events.

An OutputEvent is what the core hands to any OutputAdapter (Discord, Dashboard,
Telegram...). It is NOT specific to Discord. It carries a deterministic
``dedup_key`` derived from the setup's relevant data so the same candidate is
never emitted twice.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

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
    # Optional PNG attached to the message (chart of the idea / trade).
    image_png: bytes | None = None

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

    @classmethod
    def from_trade_opened(cls, paper_trade_dict: dict,
                          image_png: bytes | None = None) -> "OutputEvent":
        """GHOST opened a simulated paper trade — the primary Discord notification.
        Uses signal_id as dedup_key (same invariant: one notification per trade).
        status_payload contains the full PaperTrade.to_dict() — JSON-safe, no
        secrets. Never logs or includes the webhook URL."""
        direction = paper_trade_dict.get("direction", "LONG")
        instrument = paper_trade_dict.get("instrument", "?")
        arrow = "📈" if direction == "LONG" else "📉"
        return cls(
            kind="trade_opened",
            dedup_key=paper_trade_dict["signal_id"],
            title=f"{arrow} {direction} OPENED — {instrument}",
            disclaimer="Simulated paper trade — no real money, no broker connection.",
            status_payload=paper_trade_dict,
            image_png=image_png,
        )

    @classmethod
    def from_trade_closed(cls, paper_trade_dict: dict,
                          image_png: bytes | None = None) -> "OutputEvent":
        """A paper trade reached TP/stop/invalidation. Carries realised P&L so
        the operator sees the outcome without opening the dashboard.
        dedup_key is signal_id + ":closed" — distinct from the open event, so a
        trade produces exactly one open and one close notification."""
        instrument = paper_trade_dict.get("instrument", "?")
        reason = (paper_trade_dict.get("exit_reason") or "CLOSED").upper()
        r = paper_trade_dict.get("r_multiple")
        won = r is not None and r > 0
        mark = "✅" if won else ("➖" if r == 0 else "🛑")
        return cls(
            kind="trade_closed",
            dedup_key=f"{paper_trade_dict.get('signal_id', '')}:closed",
            title=f"{mark} {reason} — {instrument}",
            disclaimer="Simulated paper trade — no real money, no broker connection.",
            status_payload=paper_trade_dict,
            image_png=image_png,
        )

    @classmethod
    def from_trade_progress(cls, paper_trade_dict: dict,
                            milestone_r: float) -> "OutputEvent":
        """An OPEN trade crossed a progress milestone (a multiple of its stop,
        titled in points). dedup_key pins the milestone so a trade oscillating
        around it cannot re-announce it."""
        instrument = paper_trade_dict.get("instrument", "?")
        direction = paper_trade_dict.get("direction", "LONG")
        up = milestone_r > 0
        mark = "🟢" if up else "🟠"
        d = paper_trade_dict
        risk = d.get("initial_risk") or abs((d.get("entry") or 0.0) - (d.get("stop") or 0.0))
        pts = milestone_r * risk
        label = f"{pts:+.1f} pts"
        return cls(
            kind="trade_progress",
            dedup_key=f"{paper_trade_dict.get('signal_id', '')}:r{milestone_r:g}",
            title=f"{mark} {instrument} {direction} at {label}",
            disclaimer="Simulated paper trade update — no real money, no broker connection.",
            status_payload={**paper_trade_dict, "milestone_r": milestone_r},
        )

    @classmethod
    def from_status_digest(cls, payload: dict, timestamp) -> "OutputEvent":
        """Periodic 'here is where we stand' summary. dedup_key includes the
        timestamp so each digest is its own event."""
        raw = f"status_digest|{timestamp.isoformat()}"
        return cls(
            kind="status_digest",
            dedup_key=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            title="📊 Bot status",
            disclaimer="Automated status digest — not a trading signal.",
            status_payload={**payload, "timestamp": timestamp.isoformat()},
        )

    @classmethod
    def from_trade_idea(cls, idea: dict, image_png: bytes | None = None) -> "OutputEvent":
        """Zone strategy: {"event": "idea"} = heads-up before a possible trade,
        {"event": "cancelled"} = it will not happen. One of each per zone."""
        kind = "trade_idea" if idea.get("event") == "idea" else "trade_idea_cancelled"
        return cls(
            kind=kind,
            dedup_key=f"{idea.get('zone_id', '')}:{idea.get('event', '')}",
            title=f"IDEA #{idea.get('number', '?')}",
            disclaimer="Trading simulado en papel — no es consejo financiero.",
            status_payload=dict(idea),
            image_png=image_png,
        )

    @classmethod
    def from_trade_tp1(cls, paper_trade_dict: dict) -> "OutputEvent":
        return cls(
            kind="trade_tp1",
            dedup_key=f"{paper_trade_dict.get('signal_id', '')}:tp1",
            title="TP1",
            disclaimer="Simulated paper trade — no real money, no broker connection.",
            status_payload=paper_trade_dict,
        )

    @classmethod
    def from_daily_record(cls, text: str, day: str) -> "OutputEvent":
        """The trading record after the day's close (one per trading day)."""
        return cls(
            kind="daily_record",
            dedup_key=f"daily_record:{day}",
            title="RÉCORD",
            disclaimer="Trading simulado en papel — no es consejo financiero.",
            status_payload={"text": text, "day": day},
        )

    @classmethod
    def from_note(cls, title: str, text: str, color: int, key: str,
                  image_png: bytes | None = None) -> "OutputEvent":
        """A plain titled message (the paper sub-bots: box, entry, close...)."""
        return cls(
            kind="note",
            dedup_key=f"note:{key}",
            title=title,
            disclaimer="Trading simulado en papel — no es consejo financiero.",
            status_payload={"text": text, "color": color},
            image_png=image_png,
        )

    @classmethod
    def from_day_done(cls, payload: dict) -> "OutputEvent":
        """A daily limit was reached: no more trades this trading day."""
        return cls(
            kind="day_done",
            dedup_key=f"day_done:{payload.get('day', '')}",
            title="FIN DEL DÍA",
            disclaimer="Trading simulado en papel — no es consejo financiero.",
            status_payload=dict(payload),
        )

    @classmethod
    def from_stop_moved(cls, paper_trade_dict: dict, old_stop: float,
                        image_png: bytes | None = None) -> "OutputEvent":
        stop = paper_trade_dict.get("stop") or 0.0
        return cls(
            kind="stop_moved",
            dedup_key=f"{paper_trade_dict.get('signal_id', '')}:stop:{stop:.2f}",
            title="STOP",
            disclaimer="Simulated paper trade — no real money, no broker connection.",
            status_payload={**paper_trade_dict, "old_stop": old_stop},
            image_png=image_png,
        )
