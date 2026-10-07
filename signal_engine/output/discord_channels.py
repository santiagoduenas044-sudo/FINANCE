"""One Discord channel per set up.

The command bot (DISCORD_BOT_TOKEN) creates, in the same server, a category
"Set ups" with one channel per set up and a webhook in each. From then on
every message of a set up (heads-up, entry, exit, its chart) goes to its own
channel; the day's close, the verse/fact and errors stay in the general
channel (DISCORD_WEBHOOK_URL / ORBIT's). A set up without a channel keeps
writing in the general one, as before.

The webhooks are secrets: they live only in data_store/discord_channels.json
(never printed, never sent to the page). The bot needs two permissions in
the server: Manage Channels and Manage Webhooks.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data_store" / "discord_channels.json"
API = "https://discord.com/api/v10"
CATEGORY = "Set ups"
WEBHOOK_NAME = "GHOST"
TOKEN_ENV = "DISCORD_BOT_TOKEN"
CHANNEL_ENV = "DISCORD_BOT_CHANNEL_ID"
# what a set up says on its own: its trades and their heads-ups
_TRADE_KINDS = {"trade_idea", "trade_idea_cancelled", "trade_opened", "trade_closed",
                "trade_progress", "trade_tp1", "stop_moved", "day_done", "signal"}
_ORBIT_OWN = {"open", "close", "be", "box", "none", "missed"}
log = logging.getLogger(__name__)
_lock = threading.Lock()
_cache: dict = {"mtime": None, "data": {}}


class ChannelError(Exception):
    """Something the owner can fix (shown as is on the page)."""


# --------------------------------------------------------------------------- #
# the set ups and their channels
# --------------------------------------------------------------------------- #
def setups() -> list[dict]:
    """[{n, id, label, about, on}] for the bots and every parallel set up."""
    from ..runtime.shadows import is_on, load_specs
    from ..versions import MAIN_SETUP, main_setup_label
    out = [{"n": MAIN_SETUP[b], "id": b.lower(), "label": main_setup_label(b),
            "about": "", "on": True} for b in ("GHOST", "ORBIT")]
    for bot in ("ghost", "orbit"):
        for sp in load_specs(bot):
            try:
                on = is_on(bot, sp)
            except Exception:  # noqa: BLE001
                on = bool(getattr(sp, "enabled", True))
            out.append({"n": sp.setup, "id": sp.id, "label": sp.label,
                        "about": getattr(sp, "about", "") or "", "on": on})
    return sorted(out, key=lambda s: s["n"])


def _load(path: Path = STORE) -> dict:
    try:
        m = path.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != (path, m):
        try:
            _cache["data"] = json.loads(path.read_text())
        except (OSError, ValueError):
            _cache["data"] = {}
        _cache["mtime"] = (path, m)
    return _cache["data"]


def _save(data: dict, path: Path = STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def webhook_for(n: int | None, path: Path = STORE) -> str | None:
    if n is None:
        return None
    return ((_load(path).get("setups") or {}).get(str(n)) or {}).get("webhook")


def setup_of(event, main_setup: int | None) -> int | None:
    """Which set up a message belongs to (None = the general channel)."""
    if event.kind in _TRADE_KINDS:
        return main_setup
    if event.kind != "note":
        return None
    parts = event.dedup_key.split(":")[1:]               # "note:<key>"
    if len(parts) > 1 and parts[0] == "setup":
        return {s["id"]: s["n"] for s in setups()}.get(parts[1])
    if parts and parts[0] in _ORBIT_OWN:                  # ORBIT's own trades
        return main_setup
    return None


def status(path: Path = STORE) -> dict:
    """For Sistema: which set ups have their channel (names only, never URLs)."""
    data = _load(path)
    have = data.get("setups") or {}
    rows = [{"n": s["n"], "label": s["label"], "on": s["on"],
             "channel": (have.get(str(s["n"])) or {}).get("name")} for s in setups()]
    return {"bot": bool(os.environ.get(TOKEN_ENV, "").strip()), "rows": rows,
            "history": data.get("history") or {}, "error": data.get("error")}


# --------------------------------------------------------------------------- #
# creating them (Discord REST with the bot token)
# --------------------------------------------------------------------------- #
def _api(method: str, path: str, body: dict | None = None, token: str | None = None):
    token = token or os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ChannelError("Falta el token del bot de comandos (DISCORD_BOT_TOKEN).")
    req = urllib.request.Request(
        API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "DiscordBot (https://github.com/santiagoduenas044-sudo/FINANCE, 1.0)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:                  # rate limited: wait and retry
                try:
                    wait = float(json.loads(exc.read()).get("retry_after", 1.0))
                except (ValueError, AttributeError):
                    wait = 1.0
                time.sleep(min(wait, 10.0) + 0.2)
                continue
            if exc.code == 403:
                raise ChannelError("Al bot le falta permiso. En Discord: Ajustes del servidor → "
                                   "Roles → el rol del bot → activa «Gestionar canales» y "
                                   "«Gestionar webhooks».") from None
            if exc.code == 401:
                raise ChannelError("El token del bot no sirve. Vuelve a ponerlo con "
                                   "bash deploy/macos/ctl.sh discord comandos") from None
            raise ChannelError(f"Discord respondió {exc.code}.") from None
        except urllib.error.URLError:
            raise ChannelError("No pude hablar con Discord (sin internet).") from None
    raise ChannelError("Discord pidió esperar demasiadas veces; prueba en un minuto.")


def _slug(label: str) -> str:
    s = unicodedata.normalize("NFKD", label.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c if c.isalnum() else "-" for c in s.replace("×", "x"))
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-")[:90]


def _guild(api) -> str:
    ch = os.environ.get(CHANNEL_ENV, "").strip()
    if ch:
        return str(api("GET", f"/channels/{ch}")["guild_id"])
    guilds = api("GET", "/users/@me/guilds") or []
    if len(guilds) != 1:
        raise ChannelError("No sé en qué servidor crear los canales: pon DISCORD_BOT_CHANNEL_ID "
                           "(el canal donde contesta el bot).")
    return str(guilds[0]["id"])


def create_channels(api=_api, path: Path = STORE, only_on: bool = True) -> dict:
    """Category + one channel and webhook per set up that is on. Running it
    again only adds what is missing (a channel deleted by hand is made again)."""
    with _lock:
        data = dict(_load(path))
        try:
            guild = _guild(api)
            existing = api("GET", f"/guilds/{guild}/channels") or []
            by_id = {str(c["id"]): c for c in existing}
            cat = next((c for c in existing if c.get("type") == 4 and c.get("name") == CATEGORY), None)
            if cat is None:
                cat = api("POST", f"/guilds/{guild}/channels", {"name": CATEGORY, "type": 4})
            have = dict(data.get("setups") or {})
            made = []
            for s in setups():
                if only_on and not s["on"]:
                    continue
                row = have.get(str(s["n"])) or {}
                ch = by_id.get(str(row.get("channel"))) if row.get("channel") else None
                if ch is None:
                    name = _slug(s["label"])
                    ch = next((c for c in existing if c.get("type") == 0 and c.get("name") == name), None) \
                        or api("POST", f"/guilds/{guild}/channels",
                               {"name": name, "type": 0, "parent_id": cat["id"],
                                "topic": (s["about"] or s["label"])[:1000]})
                    made.append(s["label"])
                if not row.get("webhook") or str(row.get("channel")) != str(ch["id"]):
                    hooks = api("GET", f"/channels/{ch['id']}/webhooks") or []
                    hook = next((h for h in hooks if h.get("name") == WEBHOOK_NAME and h.get("token")), None) \
                        or api("POST", f"/channels/{ch['id']}/webhooks", {"name": WEBHOOK_NAME})
                    row = {"channel": str(ch["id"]), "name": ch["name"],
                           "webhook": f"https://discord.com/api/webhooks/{hook['id']}/{hook['token']}"}
                have[str(s["n"])] = row
            data.update({"guild": guild, "category": str(cat["id"]), "setups": have, "error": None})
            _save(data, path)
            return {"made": made, "total": len(have)}
        except ChannelError as exc:
            data["error"] = str(exc)
            _save(data, path)
            raise


# --------------------------------------------------------------------------- #
# sending each set up's past trades to its channel
# --------------------------------------------------------------------------- #
def _ny(ts):
    import pandas as pd
    t = pd.Timestamp(ts)
    return t.tz_localize("America/New_York") if t.tzinfo is None else t.tz_convert("America/New_York")


def _usd(v: float) -> str:
    return f"{'+' if v >= 0 else '−'}${abs(v):,.0f}"


_WHY = {"TP1": "objetivo", "TP2": "objetivo", "STOP": "stop", "EOD": "cierre del día",
        "INVALIDATION": "invalidado"}


def history_payloads(label: str, trades: list[dict], per_message: int = 15) -> list[dict]:
    """The set up's record as a few messages: a header, then its trades."""
    v = sum(t.get("net_usd") or 0.0 for t in trades)
    wins = sum(1 for t in trades if (t.get("net_usd") or 0) > 0)
    head = {"title": f"📒 Historial · {label}", "color": 0x3498DB,
            "description": (f"**{len(trades)} trades · ganó {wins} · {_usd(v)}** con comisiones\n"
                            f"Desde el {trades[0]['day']} · en papel" if trades
                            else "Todavía sin trades cerrados.")}
    out = [{"embeds": [head]}]
    for i in range(0, len(trades), per_message):
        lines = []
        for t in trades[i:i + per_message]:
            u = t.get("net_usd") or 0.0
            o = _ny(t["opened_at"]) if t.get("opened_at") else None
            why = _WHY.get(str(t.get("why") or "").upper(), str(t.get("why") or "").lower())
            mark = "✅" if u > 0 else "🛑" if u < 0 else "➖"
            when = f"{o:%d-%m %H:%M} · " if o is not None else ""
            side = "COMPRA" if t.get("side") == "LONG" else "VENTA"
            lines.append(f"{mark} {when}{side} {(t.get('entry') or 0):,.2f} → "
                         f"{(t.get('exit') or 0):,.2f} · {why} · **{_usd(u)}**")
        out.append({"embeds": [{"color": 0x2B2D31, "description": "\n".join(lines)}]})
    return out


def send_history(log_data: dict, post=None, path: Path = STORE, pause: float = 1.2) -> dict:
    """Each set up's past trades to its own channel (only set ups that have
    one). `post(url, payload) -> status`."""
    from ..output.record_pdf import setups_of
    from .discord import _urllib_transport
    post = post or (lambda url, payload: _urllib_transport(url, json.dumps(payload).encode(), 15))
    data = dict(_load(path))
    have = data.get("setups") or {}
    sent = {}
    for so, trades in setups_of(log_data):
        url = (have.get(str(so["setup"])) or {}).get("webhook")
        if not url:
            continue
        label = so.get("setup_label") or so.get("name") or so["id"]
        n = 0
        for payload in history_payloads(label, trades):
            code = post(url, payload)
            if code == 429:
                time.sleep(3.0)
                code = post(url, payload)
            n += 1 if 200 <= code < 300 else 0
            time.sleep(pause)
        sent[str(so["setup"])] = {"trades": len(trades), "messages": n}
    data["history"] = {"at": time.strftime("%Y-%m-%d %H:%M"), "sent": sent}
    _save(data, path)
    return sent
