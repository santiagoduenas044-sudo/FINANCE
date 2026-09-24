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
from ..ghost.narrative import render_thesis
from ..setups.models import SetupCandidate
from ..signals.models import Signal
from .base import OutputAdapter
from .events import OutputEvent

log = logging.getLogger("signal_engine.output.discord")

# HTTP statuses worth retrying (rate limit + transient server errors)
_RETRYABLE = {429, 500, 502, 503, 504}
_EMBED_COLOR = 0xF1C40F           # yellow, matching the 🟡 marker
_FIELD_MAX = 1024                 # Discord embed field value limit
_DESCRIPTION_MAX = 4096           # Discord embed description limit


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
    if event.kind == "trade_opened":
        return _build_trade_opened_payload(event)
    if event.kind == "trade_closed":
        return _build_trade_closed_payload(event)
    if event.kind == "trade_progress":
        return _build_trade_progress_payload(event)
    if event.kind == "status_digest":
        return _build_status_digest_payload(event)
    if event.kind in ("trade_idea", "trade_idea_cancelled"):
        return _build_trade_idea_payload(event)
    if event.kind == "trade_tp1":
        return _build_trade_tp1_payload(event)
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
_TRADE_COLOR  = {"LONG": 0x00C851, "SHORT": 0xFF4444}


def _build_trade_opened_payload(event: OutputEvent) -> dict:
    """Rich "Trade Opened" embed for GHOST-approved simulated paper trades.
    Includes full TradePlan: direction, entry, stop, TPs, R multiples, setup,
    regime, session, signal_id (first 8 chars for readability). No secrets."""
    p = event.status_payload or {}
    if (p.get("context") or {}).get("idea_number") is not None:
        return _build_idea_trade_opened_payload(event)
    direction = p.get("direction", "LONG")
    instrument = p.get("instrument", "?")
    entry = p.get("entry")
    stop  = p.get("stop")
    tp1   = p.get("tp1")
    tp2   = p.get("tp2")
    tp3   = p.get("tp3")
    tp1_r = p.get("tp1_r")
    tp2_r = p.get("tp2_r")
    setup = p.get("setup", "—")
    tf    = p.get("timeframe", "—")
    risk  = p.get("initial_risk", 0)
    ctx   = p.get("context") or {}
    regime  = ctx.get("regime", "—")
    session = ctx.get("session", "—")
    mtf     = ctx.get("mtf", "—")
    sig_id  = p.get("signal_id", "")[:8]

    def fp(v): return f"{v:.2f}" if v is not None else "—"
    def fr(v): return f"+{v:.2f}R" if v is not None else "—"

    fields = [
        {"name": "Instrument", "value": instrument, "inline": True},
        {"name": "Direction",  "value": direction,  "inline": True},
        {"name": "Setup",      "value": setup,      "inline": True},
        {"name": "Entry",      "value": fp(entry),  "inline": True},
        {"name": "Stop",       "value": fp(stop),   "inline": True},
        {"name": "Risk (pts)", "value": fp(risk),   "inline": True},
    ]
    if tp1 is not None:
        fields.append({"name": "TP1", "value": f"{fp(tp1)} ({fr(tp1_r)})", "inline": True})
    if tp2 is not None:
        fields.append({"name": "TP2", "value": f"{fp(tp2)} ({fr(tp2_r)})", "inline": True})
    if tp3 is not None:
        fields.append({"name": "TP3", "value": fp(tp3), "inline": True})
    fields += [
        {"name": "Timeframe", "value": tf,      "inline": True},
        {"name": "Regime",    "value": regime,  "inline": True},
        {"name": "Session",   "value": session, "inline": True},
        {"name": "MTF",       "value": mtf,     "inline": True},
        {"name": "Signal ID", "value": sig_id + "…", "inline": True},
    ]
    color = _TRADE_COLOR.get(direction, 0x2ECC71)
    thesis = ctx.get("thesis")
    description = (render_thesis(thesis) if thesis else f"**{event.disclaimer}**")
    embed = {
        "title": event.title,
        "description": _truncate(description, _DESCRIPTION_MAX),
        "color": color,
        "fields": fields,
        "footer": {"text": "GHOST paper trade · " + event.disclaimer},
    }
    return {"embeds": [embed]}


_WIN_COLOR  = 0x00C851
_LOSS_COLOR = 0xFF4444
_FLAT_COLOR = 0x95A5A6


def _pts(v) -> str:
    return f"{v:+.2f}" if v is not None else "—"


def _rmult(v) -> str:
    return f"{v:+.2f}R" if v is not None else "—"


def _build_trade_closed_payload(event: OutputEvent) -> dict:
    """Realised outcome of a paper trade: exit reason, P&L in points and R, and
    how far it ran in each direction before closing (MFE/MAE)."""
    p = event.status_payload or {}
    if (p.get("context") or {}).get("idea_number") is not None:
        return _build_idea_trade_closed_payload(event)
    r = p.get("r_multiple")
    color = _FLAT_COLOR if not r else (_WIN_COLOR if r > 0 else _LOSS_COLOR)
    fields = [
        {"name": "Instrument", "value": p.get("instrument", "—"), "inline": True},
        {"name": "Direction",  "value": p.get("direction", "—"),  "inline": True},
        {"name": "Setup",      "value": p.get("setup", "—"),      "inline": True},
        {"name": "Entry",      "value": _fmt_price(p.get("entry")),     "inline": True},
        {"name": "Exit",       "value": _fmt_price(p.get("exit_price")), "inline": True},
        {"name": "Exit reason", "value": (p.get("exit_reason") or "—"), "inline": True},
        {"name": "P&L (pts)",  "value": _pts(p.get("pnl_points")), "inline": True},
        {"name": "Result",     "value": _rmult(r),                 "inline": True},
        {"name": "Bars held",  "value": str(p.get("bars_held", "—")), "inline": True},
        {"name": "Best (MFE)", "value": _rmult(p.get("mfe_r")),    "inline": True},
        {"name": "Worst (MAE)", "value": _rmult(p.get("mae_r")),   "inline": True},
        {"name": "Signal ID",  "value": p.get("signal_id", "")[:8] + "…", "inline": True},
    ]
    embed = {
        "title": event.title,
        "description": f"**{event.disclaimer}**",
        "color": color,
        "fields": fields,
        "footer": {"text": "GHOST paper trade · " + event.disclaimer},
    }
    return {"embeds": [embed]}


def _build_trade_progress_payload(event: OutputEvent) -> dict:
    """An open trade crossed an R milestone — a short 'where it stands now'
    update rather than the full trade plan."""
    p = event.status_payload or {}
    milestone = p.get("milestone_r", 0.0)
    color = _WIN_COLOR if milestone > 0 else 0xFFA000
    fields = [
        {"name": "Instrument", "value": p.get("instrument", "—"), "inline": True},
        {"name": "Direction",  "value": p.get("direction", "—"),  "inline": True},
        {"name": "Setup",      "value": p.get("setup", "—"),      "inline": True},
        {"name": "Entry",      "value": _fmt_price(p.get("entry")), "inline": True},
        {"name": "Now",        "value": _fmt_price(p.get("current_price")), "inline": True},
        {"name": "P&L (pts)",  "value": _pts(p.get("pnl_points")), "inline": True},
        {"name": "Open P&L",   "value": _rmult(p.get("r_multiple")), "inline": True},
        {"name": "Best (MFE)", "value": _rmult(p.get("mfe_r")),   "inline": True},
        {"name": "Stop",       "value": _fmt_price(p.get("stop")), "inline": True},
        {"name": "TP1",        "value": _fmt_price(p.get("tp1")),  "inline": True},
        {"name": "TP2",        "value": _fmt_price(p.get("tp2")),  "inline": True},
        {"name": "TP1 hit",    "value": "yes" if p.get("tp1_hit") else "no", "inline": True},
    ]
    embed = {
        "title": event.title,
        "description": f"**{event.disclaimer}**",
        "color": color,
        "fields": fields,
        "footer": {"text": "GHOST paper trade · " + event.disclaimer},
    }
    return {"embeds": [embed]}


def _build_status_digest_payload(event: OutputEvent) -> dict:
    """Periodic summary so the operator can confirm the bot is alive and see
    where the day stands without reaching the dashboard."""
    p = event.status_payload or {}
    open_lines = p.get("open_trades") or []
    body = "\n".join(open_lines) if open_lines else "_no open trades_"
    fields = [
        {"name": "Bot",          "value": p.get("status", "—"),  "inline": True},
        {"name": "Uptime",       "value": p.get("uptime", "—"),  "inline": True},
        {"name": "Data feed",    "value": p.get("feed", "—"),    "inline": True},
        {"name": "Trades today", "value": str(p.get("trades_today", 0)), "inline": True},
        {"name": "Open",         "value": str(p.get("open_count", 0)),   "inline": True},
        {"name": "Closed today", "value": str(p.get("closed_today", 0)), "inline": True},
        {"name": "Realised today", "value": _pts(p.get("realised_points")), "inline": True},
        {"name": "Win rate",     "value": p.get("win_rate", "—"), "inline": True},
        {"name": "Open P&L",     "value": _rmult(p.get("open_r")), "inline": True},
        {"name": "Open positions", "value": _truncate(body), "inline": False},
    ]
    for sym, lines in (p.get("market_read") or {}).items():
        if lines:
            fields.append({"name": f"👁 Lo que veo en {sym}",
                           "value": _truncate("\n".join(lines)), "inline": False})
    embed = {
        "title": event.title,
        "description": f"**{event.disclaimer}**",
        "color": 0x3498DB,
        "fields": fields,
        "footer": {"text": event.disclaimer},
    }
    return {"embeds": [embed]}


# --------------------------------------------------------------------------- #
# Zone strategy: trade-idea lifecycle (heads-up -> confirmed/direct/cancelled
# -> outcome). Plain Spanish, numbers first, no tables: read on a phone.
# --------------------------------------------------------------------------- #
_IDEA_COLOR = 0xF1C40F
_CANCEL_COLOR = 0x95A5A6


def _px(v) -> str:
    return f"{v:,.2f}" if v is not None else "—"


def _side(direction: str) -> str:
    return "COMPRA" if direction == "LONG" else "VENTA"


def _usd(points, per_point: dict | None) -> str:
    """' ≈ $70 MNQ / $700 NQ' (micro first) — empty when the point values are
    not confirmed (see contracts.yaml operator_confirmed)."""
    if not per_point or points is None:
        return ""
    parts = [f"${abs(points) * pv:,.0f} {sym}"
             for sym, pv in sorted(per_point.items(), key=lambda kv: kv[1])]
    return " ≈ " + " / ".join(parts)


def _embed(title: str, description: str, color: int, footer: str) -> dict:
    return {"embeds": [{"title": title,
                        "description": _truncate(description, _DESCRIPTION_MAX),
                        "color": color, "footer": {"text": footer}}]}


def _targets_line(tp1, final, rr=None, *, breakeven: bool = False) -> str:
    out = f"🎯 TP1 {_px(tp1)}" + (" → stop a BE" if breakeven else "")
    if final is not None:
        out += f" · TP {_px(final)}" + (f" ({rr:g}R)" if rr else "")
    return out


# Short on purpose: a signal is read in a few seconds on a phone. Every line is
# a number to act on; the rules behind them live in the strategy, not the text.
def _build_trade_idea_payload(event: OutputEvent) -> dict:
    p = event.status_payload or {}
    num, sym = p.get("number", "?"), p.get("symbol", "?")
    side = _side(p.get("direction", "LONG"))
    if event.kind == "trade_idea_cancelled":
        return _embed(f"⚪ IDEA #{num} CANCELADA — {sym}",
                      f"{p.get('reason', 'sin reacción').capitalize()}. Sin entrada.",
                      _CANCEL_COLOR, event.disclaimer)
    plan = p.get("plan") or {}
    risk = p.get("risk_points")
    lines = [f"Vigila **{_px(p.get('mid'))}** (zona {_px(p.get('bottom'))} – {_px(p.get('top'))})",
             f"🛑 SL {_px(p.get('stop'))} · ~{risk:.0f} pts{_usd(risk, p.get('usd_per_point'))}"]
    if plan.get("tp1") is not None:
        lines.append(_targets_line(plan["tp1"], plan.get("final"), plan.get("plan_rr")))
    lines.append("⚠️ **No entres todavía** — espero reacción en la zona.")
    return _embed(f"🟡 IDEA #{num} — posible {side} · {sym}", "\n".join(lines),
                  _IDEA_COLOR, event.disclaimer)


def _build_idea_trade_opened_payload(event: OutputEvent) -> dict:
    p = event.status_payload or {}
    ctx = p.get("context") or {}
    num, sym = ctx.get("idea_number"), p.get("instrument", "?")
    direction = p.get("direction", "LONG")
    side = _side(direction)
    entry, stop = p.get("entry"), p.get("stop")
    mid = ctx.get("zone_mid", entry)
    title = (f"⚡ SEÑAL DIRECTA #{num} — {side} · {sym}" if ctx.get("idea_direct")
             else f"🟢 IDEA #{num} — {side} · {sym}")
    risk = abs(mid - stop) if mid is not None and stop is not None else None
    lines = [f"✅ **Entra ahora {_px(entry)}** · óptima {_px(mid)}",
             f"🛑 SL {_px(stop)}" + (f" · ~{risk:.0f} pts{_usd(risk, ctx.get('usd_per_point'))}"
                                     if risk is not None else ""),
             _targets_line(p.get("tp1"), p.get("tp2"), ctx.get("plan_rr"), breakeven=True)]
    return _embed(title, "\n".join(lines), _TRADE_COLOR.get(direction, _WIN_COLOR),
                  event.disclaimer)


_EXIT_ES = {"TP1": "TP1", "TP2": "TP final", "STOP": "Stop", "INVALIDATION": "Invalidado",
            "EOD": "Cierre 4pm", "CANCELLED": "Cancelado"}


def _build_idea_trade_closed_payload(event: OutputEvent) -> dict:
    p = event.status_payload or {}
    ctx = p.get("context") or {}
    num = ctx.get("idea_number")
    pts = p.get("pnl_points") or 0.0
    r = p.get("r_multiple") or 0.0
    reason = (p.get("exit_reason") or "").upper()
    label = _EXIT_ES.get(reason, reason.title() or "Cerrado")
    if reason == "STOP" and p.get("tp1_hit"):
        label = "BE tras parcial" if p.get("partial_fraction") else "Breakeven"
    mark = "✅" if pts > 1e-6 else ("➖" if abs(pts) <= 1e-6 else "🛑")
    usd = "".join(f" · {'+' if pts >= 0 else '−'}${abs(pts) * pv:,.0f} {s}"
                  for s, pv in sorted((ctx.get("usd_per_point") or {}).items(),
                                      key=lambda kv: kv[1]))
    return _embed(
        f"{mark} IDEA #{num} — {label} · {pts:+.2f} pts",
        f"{_px(p.get('entry'))} → {_px(p.get('exit_price'))} · {r:+.2f}R{usd}",
        _FLAT_COLOR if pts == 0 else (_WIN_COLOR if pts > 0 else _LOSS_COLOR),
        event.disclaimer)


def _build_trade_tp1_payload(event: OutputEvent) -> dict:
    p = event.status_payload or {}
    num = (p.get("context") or {}).get("idea_number")
    name = f"IDEA #{num}" if num is not None else p.get("setup", "trade")
    entry, tp1 = p.get("entry"), p.get("tp1")
    gained = abs(tp1 - entry) if entry is not None and tp1 is not None else None
    frac = p.get("partial_fraction") or 0
    head = f"🎯 {name} — TP1" + (f" +{gained:.2f} pts" if gained is not None else "")
    return _embed(
        head + f" · {p.get('instrument', '?')}",
        (f"{frac:.0%} cerrado · " if frac else "")
        + f"stop a BE {_px(entry)} · resto a {_px(p.get('tp2'))}",
        _WIN_COLOR, event.disclaimer)


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
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://github.com/santiagoduenas044-sudo/FINANCE, 1.0)",
        },
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
            log.debug("Discord emit skipped: adapter disabled. kind=%s", event.kind)
            return False
        if not self._webhook:
            log.warning("Discord emit skipped: webhook env=%s not set. kind=%s",
                        self.config.webhook_env, event.kind)
            return False
        if self.config.deduplication_enabled and event.dedup_key in self._seen:
            log.debug("Discord emit dedup: already sent dedup_key=%.8s… kind=%s",
                      event.dedup_key, event.kind)
            return False
        log.info("Discord SEND kind=%s dedup_key=%.8s…", event.kind, event.dedup_key)
        payload = build_discord_payload(event)
        ok = self._send_with_retry(payload)
        if ok:
            log.info("Discord SENT OK kind=%s dedup_key=%.8s…", event.kind, event.dedup_key)
            if self.config.deduplication_enabled:
                self._seen.add(event.dedup_key)
        else:
            log.warning("Discord FAILED kind=%s dedup_key=%.8s…", event.kind, event.dedup_key)
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
                log.debug("Discord HTTP %d (attempt %d).", status, i + 1)
                return True
            if status in _RETRYABLE and not last:
                log.warning("Discord HTTP %d (attempt %d), retrying…", status, i + 1)
                self._backoff(i)
                continue
            log.warning("Discord HTTP %d (attempt %d); giving up.", status, i + 1)
            return False
        return False

    def _backoff(self, attempt: int) -> None:
        # bounded, linear backoff (no infinite loops)
        self._sleep(0.5 * (attempt + 1))
