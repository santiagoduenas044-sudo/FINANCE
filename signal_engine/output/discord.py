"""Discord output adapter (Phase 5.5).

Turns SetupCandidate OutputEvents into readable Discord webhook messages.
Discord is a purely external observability layer: the core never imports or
depends on this module, and a Discord failure never affects the Setup Engine.

Security: the webhook URL comes from an environment variable, is never stored in
config/logs/tests/docs, and is never included in exception messages or log lines.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from ..config.schema import DiscordOutputConfig
from ..setups.models import SetupCandidate
from ..signals.models import Signal
from .base import OutputAdapter
from .events import OutputEvent

log = logging.getLogger("signal_engine.output.discord")

# HTTP statuses worth retrying (rate limit + transient server errors)
_RETRYABLE = {429, 500, 502, 503, 504}
_EMBED_COLOR = 0xF1C40F           # yellow, matching the 🟡 marker
_FIELD_MAX = 1024                 # Discord embed field value limit


def _fmt_price(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def _truncate(s: str, limit: int = _FIELD_MAX) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def render_fields(c: SetupCandidate) -> list[tuple[str, str]]:
    """Human-readable (name, value) pairs. Only includes fields that exist."""
    tps = c.take_profits or []
    fields: list[tuple[str, str]] = [
        ("Instrument", c.instrument),
        ("Setup", c.setup_name),
        ("Bias", c.direction.value),                    # BULLISH/BEARISH, NOT long/short
        ("Timestamp", f"{c.timestamp.isoformat()}"),
        ("Timeframe", c.timeframe),
        ("Entry", _fmt_price(c.entry)),
        ("Stop Loss", _fmt_price(c.stop_loss)),
    ]
    if len(tps) >= 1:
        fields.append(("Take Profit 1", _fmt_price(tps[0])))
    if len(tps) >= 2:
        fields.append(("Take Profit 2", _fmt_price(tps[1])))
    if c.risk_reward:
        fields.append(("R:R", ", ".join(f"{r:.2f}" for r in c.risk_reward)))
    if c.market_context:
        ctx = ", ".join(f"{k}={v}" for k, v in c.market_context.items())
        fields.append(("Market context", _truncate(ctx)))
    if c.reasons:
        fields.append(("Reasons", _truncate("\n".join(f"• {r}" for r in c.reasons))))
    if c.referenced:
        refs = ", ".join(
            f"{r.kind}={r.price:.2f}" for r in c.referenced if r.price is not None
        )
        if refs:
            fields.append(("Referenced liquidity / structure", _truncate(refs)))
    if c.invalidation_conditions:
        fields.append(("Invalidation", _truncate("\n".join(c.invalidation_conditions))))
    fields.append(("Expiration", f"{c.valid_until.isoformat()}"))
    fields.append(("Status", c.status.value))
    return fields


def render_signal_fields(s: Signal) -> list[tuple[str, str]]:
    """Same shape as render_fields(), for a definitive Signal (Phase 6/9) —
    Direction here IS LONG/SHORT (the final decision), unlike a candidate's
    BULLISH/BEARISH bias."""
    tps = s.take_profits or []
    fields: list[tuple[str, str]] = [
        ("Instrument", s.instrument),
        ("Setup", s.setup),
        ("Direction", s.direction.value),               # LONG / SHORT
        ("Timestamp", s.timestamp.isoformat()),
        ("Timeframe", s.timeframe),
        ("Entry", _fmt_price(s.entry)),
        ("Stop", _fmt_price(s.stop)),
    ]
    if len(tps) >= 1:
        fields.append(("Take Profit 1", _fmt_price(tps[0])))
    if len(tps) >= 2:
        fields.append(("Take Profit 2", _fmt_price(tps[1])))
    if s.risk_reward:
        fields.append(("R:R", ", ".join(f"{r:.2f}" for r in s.risk_reward)))
    if s.market_context:
        ctx = ", ".join(f"{k}={v}" for k, v in s.market_context.items())
        fields.append(("Market context", _truncate(ctx)))
    if s.reasons:
        fields.append(("Reasons", _truncate("\n".join(f"• {r}" for r in s.reasons))))
    if s.invalidation_conditions:
        fields.append(("Invalidation", _truncate("\n".join(s.invalidation_conditions))))
    if s.expiration is not None:
        fields.append(("Expiration", s.expiration.isoformat()))
    return fields


_STATUS_COLOR = {
    "BOT_STARTED": 0x2ECC71,   # green
    "BOT_PAUSED": 0xF1C40F,    # yellow
    "BOT_STOPPED": 0x95A5A6,   # gray
    "BOT_ERROR": 0xE74C3C,     # red
}
_STATUS_EMOJI = {
    "BOT_STARTED": "🟢", "BOT_PAUSED": "🟡", "BOT_STOPPED": "🔴", "BOT_ERROR": "🔴",
}


def build_discord_payload(event: OutputEvent) -> dict:
    """Pure function: OutputEvent -> Discord webhook JSON payload (embed)."""
    if event.kind == "runtime_status":
        return _build_runtime_status_payload(event)
    if event.kind == "signal":
        return _build_signal_payload(event)

    c = event.candidate
    embed = {
        "title": event.title,                               # 🟡 SETUP CANDIDATE
        "description": (f"**{event.disclaimer}**\n"
                        f"_Not a final LONG/SHORT recommendation._"),
        "color": _EMBED_COLOR,
        "fields": [{"name": n, "value": v or "-", "inline": True}
                   for n, v in render_fields(c)],
        "footer": {"text": event.disclaimer},
    }
    return {"embeds": [embed]}


_SIGNAL_COLOR = {"LONG": 0x2ECC71, "SHORT": 0xE74C3C}


def _build_signal_payload(event: OutputEvent) -> dict:
    s = event.signal
    embed = {
        "title": event.title,
        "description": f"**{event.disclaimer}**",
        "color": _SIGNAL_COLOR.get(s.direction.value, 0x3498DB),
        "fields": [{"name": n, "value": v or "-", "inline": True}
                  for n, v in render_signal_fields(s)],
        "footer": {"text": event.disclaimer},
    }
    return {"embeds": [embed]}


def _build_runtime_status_payload(event: OutputEvent) -> dict:
    """Bot lifecycle notification (Phase 9) — a distinct, simple embed. Never
    includes secrets (status_payload is a plain, pre-sanitized dict)."""
    p = event.status_payload or {}
    status = p.get("status", "UNKNOWN")
    emoji = _STATUS_EMOJI.get(status, "⚪")
    embed = {
        "title": f"{emoji} {event.title}",
        "description": f"**{event.disclaimer}**",
        "color": _STATUS_COLOR.get(status, 0x95A5A6),
        "fields": [
            {"name": "Status", "value": status, "inline": True},
            {"name": "Timestamp", "value": p.get("timestamp", "-"), "inline": True},
            {"name": "Detail", "value": p.get("detail") or "-", "inline": False},
        ],
        "footer": {"text": event.disclaimer},
    }
    return {"embeds": [embed]}


def _urllib_transport(url: str, data: bytes, timeout: float) -> int:
    """Default transport. Returns the HTTP status code. Raises on
    connection/timeout errors (handled by the adapter). Never leaks the URL."""
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", resp.getcode()))
    except urllib.error.HTTPError as exc:
        # convert HTTP errors into a status code (no URL in the return)
        return int(exc.code)


class DiscordOutputAdapter(OutputAdapter):
    name = "discord"

    def __init__(self, config: DiscordOutputConfig, *, webhook_url: str | None = None,
                 transport=None, sleep=None):
        self.config = config
        # webhook resolved from env by default; kept private, never logged
        self._webhook = webhook_url if webhook_url is not None \
            else os.environ.get(config.webhook_env)
        self._transport = transport or _urllib_transport
        import time
        self._sleep = sleep or time.sleep
        self._seen: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self._webhook)

    def __repr__(self) -> str:
        # defensive redaction: never let the webhook leak via repr()/logging
        # of the adapter object itself (e.g. an uncaught exception traceback
        # that prints locals, or a debug log of "adapter=%r").
        return (f"DiscordOutputAdapter(enabled={self.config.enabled}, "
                f"configured={self.configured}, webhook_env={self.config.webhook_env!r})")

    def emit(self, event: OutputEvent) -> bool:
        if not self.config.enabled:
            return False
        if not self._webhook:
            log.warning("Discord webhook not configured (env %s unset); skipping.",
                        self.config.webhook_env)
            return False
        if self.config.deduplication_enabled and event.dedup_key in self._seen:
            return False
        payload = build_discord_payload(event)
        ok = self._send_with_retry(payload)
        if ok and self.config.deduplication_enabled:
            self._seen.add(event.dedup_key)
        return ok

    def _send_with_retry(self, payload: dict) -> bool:
        data = json.dumps(payload).encode("utf-8")
        attempts = max(1, self.config.retry_count + 1)
        for i in range(attempts):
            last = i == attempts - 1
            try:
                status = self._transport(self._webhook, data, self.config.timeout_seconds)
            except TimeoutError:
                if last:
                    log.warning("Discord request timed out after %d attempt(s).", attempts)
                    return False
                self._backoff(i)
                continue
            except Exception as exc:            # connection failed, etc.
                if last:
                    # log only the exception TYPE, never its message/URL
                    log.warning("Discord request failed (%s).", type(exc).__name__)
                    return False
                self._backoff(i)
                continue

            if 200 <= status < 300:
                return True
            if status in _RETRYABLE and not last:
                self._backoff(i)
                continue
            log.warning("Discord returned HTTP %d; giving up.", status)
            return False
        return False

    def _backoff(self, attempt: int) -> None:
        # bounded, linear backoff (no infinite loops)
        self._sleep(0.5 * (attempt + 1))
