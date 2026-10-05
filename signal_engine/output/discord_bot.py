"""Read-only Discord command bot: !status, !record, !trades, !why, !help.

Webhooks are one-way, so answering a command needs a gateway connection. It
dials OUT over a websocket, so it works from a Mac behind a home router with no
port forwarding and no public URL.

READ-ONLY BY DESIGN. Anyone who can post in the channel can issue a command,
so no command starts, stops, pauses or trades — they only report. `build_reply`
holds the whole behaviour as a pure function over the runtime, and never calls
a mutating method; tests enforce that.

Opt-in: nothing happens unless DISCORD_BOT_TOKEN is set, and the engine never
imports discord.py at module load, so it runs without the package installed.
The token is a secret — never logged, never echoed back.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import unicodedata

import pandas as pd

from .record import (after_fees, commission_per_trade, exit_label,
                     trading_day, usd, won_lost)

log = logging.getLogger("signal_engine.output.discord_bot")

# How the command bot is doing, for the dashboard (Sistema): why !record
# answers or not. state: sin_token | sin_paquete | conectando | conectado |
# token_malo | falta_permiso | error
STATUS: dict = {"state": "sin_token", "user": None, "channel": None,
                "answered": 0, "last_command_at": None, "other_channel": 0}

TOKEN_ENV = "DISCORD_BOT_TOKEN"
CHANNEL_ENV = "DISCORD_BOT_CHANNEL_ID"
_MESSAGE_MAX = 2000               # Discord message length limit

# command -> canonical name. Spanish aliases because the operator writes Spanish.
_ALIASES = {
    "!status": "status", "!estado": "status",
    "!trades": "trades",
    "!record": "record", "!récord": "record", "!historial": "record",
    "!why": "why", "!mercado": "why", "!porque": "why",
    "!orbit": "orbit", "!apertura": "orbit",
    "!versiculo": "verse", "!versículo": "verse", "!palabra": "verse",
    "!dato": "fact", "!curioso": "fact",
    "!setups": "setups", "!setup": "setups", "!sets": "setups",
    "!help": "help", "!ayuda": "help",
}

HELP = (
    "**Comandos de GHOST** (solo lectura — ninguno opera ni detiene el bot)\n"
    "`!status` / `!estado` — estado del bot, feed y posiciones abiertas con su P&L\n"
    "`!record` / `!récord` — récord en dólares: hoy, semana, total y últimos trades\n"
    "`!trades` — últimos trades cerrados y su resultado\n"
    "`!why` / `!mercado` — qué está viendo el bot ahora y por qué\n"
    "`!setups` — todos los set ups: prendido o apagado, qué hace, hoy y total\n"
    "`!orbit` — el bot ORBIT (estrategia APERTURA): caja de hoy, trade abierto y récord\n"
    "`!versiculo` — la palabra del día · `!dato` — un dato curioso\n"
    "`!help` / `!ayuda` — esta lista"
)


def _clip(text: str) -> str:
    return text if len(text) <= _MESSAGE_MAX else text[: _MESSAGE_MAX - 1] + "…"


def _status(runtime) -> str:
    p = runtime.status_digest_payload()
    realised = p.get("realised_points")
    open_pts = p.get("open_pts")
    lines = [
        f"**🤖 GHOST — {p.get('status', '?')}** · activo {p.get('uptime', '—')} "
        f"· feed {p.get('feed', '—')} · estrategia **{p.get('strategy', '—')}**",
        (f"Trades hoy: {p.get('trades_today', 0)} · cerrados: {p.get('closed_today', 0)} "
         f"· realizado: {realised:+.2f} pts · win rate {p.get('win_rate', '—')}"
         if realised is not None else f"Trades hoy: {p.get('trades_today', 0)}"),
        "",
        "**Posiciones abiertas**" + (f" · {open_pts:+.2f} pts en total" if open_pts is not None else ""),
    ]
    lines += p.get("open_trades") or ["_ninguna_"]
    tracker = getattr(runtime, "paper_tracker", None)
    closed = [t for t in (tracker.closed_trades if tracker is not None else [])
              if t.closed_at is not None]
    if closed:
        label, pv = _position(runtime)
        fee = commission_per_trade(label)
        today = trading_day(pd.Timestamp.now(tz="America/New_York"))
        pts_today = [t.pnl_points for t in closed if trading_day(t.closed_at) == today]
        pts_all = [t.pnl_points for t in closed]
        lines.append("")
        lines.append("**Hoy:** " + (won_lost(pts_today, pv) + after_fees(pts_today, pv, fee)
                                   if pts_today else "sin trades cerrados"))
        lines.append("**Total:** " + won_lost(pts_all, pv) + after_fees(pts_all, pv, fee))
    return "\n".join(lines)


def _trades(runtime, limit: int = 5) -> str:
    tracker = getattr(runtime, "paper_tracker", None)
    closed = list(tracker.closed_trades) if tracker is not None else []
    if not closed:
        return "**Últimos trades**\n_todavía no hay trades cerrados_"
    recent = sorted(closed, key=lambda t: t.closed_at or t.opened_at)[-limit:][::-1]
    _label, pv = _position(runtime)
    lines = ["**Últimos trades**"]
    for t in recent:
        mark = "✅" if t.pnl_points > 0.01 else ("➖" if t.pnl_points >= -0.01 else "🛑")
        when = pd.Timestamp(t.closed_at or t.opened_at)
        when = when.tz_convert("America/New_York") if when.tzinfo else when
        lines.append(f"{mark} {when:%d-%b %H:%M} · {t.instrument} {t.direction} · "
                     f"{exit_label(t)} · {usd(t.pnl_points, pv)} ({t.pnl_points:+.2f} pts)")
    return "\n".join(lines)


def _position(runtime) -> tuple[str, float]:
    """('5 MNQ', 10.0): the position every $ figure is for."""
    feeds = list(getattr(runtime, "live_feeds", None) or {}) or ["NQ"]
    try:
        pos = runtime._point_values(feeds[0])
    except Exception:  # noqa: BLE001
        pos = None
    return next(iter(pos.items())) if pos else (f"1 punto {feeds[0]}", 1.0)


def _record(runtime) -> str:
    from .record import day_record
    feeds = list(getattr(runtime, "live_feeds", None) or {}) or ["NQ"]
    return day_record(runtime, pd.Timestamp.now(tz="America/New_York"),
                      "NQ" if "NQ" in feeds else feeds[0])


def _why(runtime) -> str:
    reads = dict(getattr(runtime, "ghost_market_read", {}) or {})
    if not reads:
        return ("**👁 Lo que veo**\n_todavía no he analizado ninguna vela — "
                "el bot tiene que estar ONLINE y procesar al menos una._")
    parts = []
    for sym, lines in reads.items():
        parts.append(f"**👁 Lo que veo en {sym}**\n" + "\n".join(lines))

    tracker = getattr(runtime, "paper_tracker", None)
    for t in (list(tracker.open_trades) if tracker is not None else []):
        thesis = (t.context or {}).get("thesis") if t.is_open else None
        if not thesis:
            continue
        block = [f"**🎯 Trade abierto {t.instrument} {t.direction}**"]
        for key, label in (("por_eso", "Por eso"), ("espero", "Espero"),
                           ("en_contra", "En contra")):
            if thesis.get(key):
                block.append(f"**{label}:** {thesis[key]}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def _orbit(_runtime) -> str:
    """ORBIT is a separate process: read its saved state and trades."""
    from ..subbots.orbit import STORE_DIR, bot_name, settings
    from ..subbots.opening_range import OpeningRangeBot
    if not STORE_DIR.exists():
        return "ORBIT todavía no ha corrido en esta Mac (bash deploy/macos/ctl.sh orbit on)."
    return OpeningRangeBot(STORE_DIR, settings=settings(), name=bot_name()).record_text()


def _verse(_rt) -> str:
    from .inspiration import verse_text
    return "📖 **Palabra del día**\n" + verse_text(pd.Timestamp.now(tz="America/New_York").date())


def _fact(_rt) -> str:
    """A random fact (the scheduled one is the day's; asking again gives another)."""
    import random
    from .inspiration import FACTS
    return "💡 **Dato curioso**\n" + random.choice(FACTS)


def _setups(runtime) -> str:
    from ..api.bots import overview, setups_text
    return setups_text(overview(runtime).get("setups") or [])


_HANDLERS = {"setups": _setups, "status": _status, "trades": _trades, "why": _why, "record": _record,
             "orbit": _orbit, "verse": _verse, "fact": _fact, "help": lambda _rt: HELP}


def _plain(word: str) -> str:
    """'!Récord' -> '!record': no accents, no case (phones add both)."""
    word = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in word if not unicodedata.combining(c))


_PLAIN_ALIASES = {_plain(k): v for k, v in _ALIASES.items()}


def command_of(content: str) -> str | None:
    """Which command a message is, as typed on a phone: '!record', '!Récord',
    '! record', '¡record', '!record.' all count. None when it is not one."""
    text = "".join(c for c in (content or "") if c not in "\u200b\u200c\u200d\ufeff").strip()
    if not text or text[0] not in "!¡":
        return None
    words = text[1:].strip().split()
    if not words:
        return None
    return _PLAIN_ALIASES.get("!" + _plain(words[0]).rstrip(".,;:!?"))


def build_reply(content: str, runtime) -> str | None:
    """The reply to a message, or None when it is not one of our commands.
    Unknown `!` commands are ignored rather than answered, so the bot stays
    quiet in a server where other bots share the prefix."""
    name = command_of(content)
    if name is None:
        return None
    try:
        return _clip(_HANDLERS[name](runtime))
    except Exception as exc:  # noqa: BLE001 - a bad read must not kill the bot
        log.warning("command %s failed (%s)", name, type(exc).__name__)
        return "⚠️ No pude leer el estado del bot ahora mismo. Intenta de nuevo."


def build_files(content: str, runtime) -> list[bytes]:
    """Charts that go with a reply: !status shows each open trade (or the
    market right now when flat)."""
    if command_of(content) != "status":
        return []
    try:
        return list(runtime.status_charts())[:3]
    except Exception as exc:  # noqa: BLE001 - a chart must not block the reply
        log.warning("status chart failed (%s)", type(exc).__name__)
        return []


def _channel_filter() -> int | None:
    raw = os.environ.get(CHANNEL_ENV, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("%s is not a numeric channel id — ignoring the restriction.",
                    CHANNEL_ENV)
        return None


def start_command_bot(runtime, *, logger: logging.Logger | None = None
                      ) -> threading.Thread | None:
    """Start the bot in a daemon thread with its own event loop. Returns the
    thread, or None when not configured / discord.py unavailable — in which
    case the rest of the bot runs exactly as before."""
    lg = logger or log
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        STATUS["state"] = "sin_token"
        lg.info("Discord command bot disabled: $%s not set.", TOKEN_ENV)
        return None
    try:
        import discord  # noqa: F401 - optional dependency
    except ImportError:
        STATUS["state"] = "sin_paquete"
        lg.warning("Discord command bot disabled: discord.py is not installed "
                   "(pip install -r requirements.txt).")
        return None

    channel_id = _channel_filter()
    STATUS.update(state="conectando", channel=channel_id)

    async def _main() -> None:
        import discord
        intents = discord.Intents.default()
        # Privileged: must also be switched on in the Developer Portal
        # (Bot -> Message Content Intent), or every message arrives empty.
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            STATUS.update(state="conectado", user=str(client.user))
            lg.info("Discord command bot connected as %s%s", client.user,
                    f" (channel {channel_id} only)" if channel_id else "")

        @client.event
        async def on_message(message) -> None:
            if message.author.bot:
                return
            if channel_id is not None and message.channel.id != channel_id:
                if command_of(message.content):
                    STATUS["other_channel"] += 1      # typed in a channel it ignores
                return
            reply = build_reply(message.content, runtime)
            if reply:
                STATUS["answered"] += 1
                STATUS["last_command_at"] = pd.Timestamp.now(tz="UTC").isoformat()
                pngs = await asyncio.to_thread(build_files, message.content, runtime)
                files = [discord.File(io.BytesIO(png), filename=f"grafica{i + 1}.png")
                         for i, png in enumerate(pngs)]
                if files:
                    await message.channel.send(reply, files=files)
                else:
                    await message.channel.send(reply)

        async with client:
            await client.start(token)

    def _run() -> None:
        import discord
        try:
            asyncio.run(_main())
        except discord.LoginFailure:
            STATUS["state"] = "token_malo"
            lg.error("Discord command bot: login failed — $%s is invalid.", TOKEN_ENV)
        except discord.PrivilegedIntentsRequired:
            STATUS["state"] = "falta_permiso"
            lg.error("Discord command bot: enable 'Message Content Intent' in the "
                     "Developer Portal (Bot tab), then restart.")
        except Exception as exc:  # noqa: BLE001
            STATUS["state"] = "error"
            lg.error("Discord command bot stopped (%s).", type(exc).__name__)

    t = threading.Thread(target=_run, name="discord-command-bot", daemon=True)
    t.start()
    return t
