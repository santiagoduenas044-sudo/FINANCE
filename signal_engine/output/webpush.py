"""Push notifications to the installed app (iPhone, Android, computer).

Web Push, done by hand so it needs only `cryptography` (no extra package to
build on the Mac or the server):
  * VAPID (RFC 8292): the server signs every request with its own key pair,
    made on first use in data_store/push/ (never in the repository).
  * aes128gcm (RFC 8291 / 8188): every message is encrypted for that one
    phone; the push service (Apple, Google, Mozilla) only carries it.

Each person turns notifications on in the app (Sistema -> Notificaciones);
the phone's subscription is stored with the user and what they want:
  trades   — entries and exits of Set up 1 and 2 (GHOST and ORBIT)
  setups   — entries and exits of the parallel set ups
  daily    — the day's close
  inspiration — the morning verse and the curious fact
Only owners and viewers get the bots' news; a practice-only user gets none.
Sending never blocks a bot: it runs in a background thread and a failure is
only logged. A phone that unsubscribed (404/410) is forgotten.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .base import OutputAdapter
from .events import OutputEvent

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = ("trades", "setups", "daily", "inspiration")
DEFAULT_PREFS = {"trades": True, "setups": True, "daily": True, "inspiration": False}
# who runs this server, for the push services (Apple asks for a real URL or mailto)
CONTACT = os.environ.get("BOT_PUSH_CONTACT", "").strip() or "https://github.com/santiagoduenas044-sudo/FINANCE"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]     # length <= 32


# --------------------------------------------------------------------------- #
# encryption and signing
# --------------------------------------------------------------------------- #
def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None,
            server_key=None) -> bytes:
    """The aes128gcm body for one subscription (RFC 8291)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    ua_pub_bytes = unb64u(p256dh)
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub_bytes)
    as_priv = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub_bytes = as_priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    secret = as_priv.exchange(ec.ECDH(), ua_pub)
    prk_key = _hkdf_extract(unb64u(auth), secret)
    ikm = _hkdf_expand(prk_key, b"WebPush: info\x00" + ua_pub_bytes + as_pub_bytes, 32)
    salt = salt or os.urandom(16)
    prk = _hkdf_extract(salt, ikm)
    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)      # one record, last
    return salt + struct.pack("!IB", 4096, len(as_pub_bytes)) + as_pub_bytes + body


class Vapid:
    """The server's own key pair, kept in data_store/push/vapid.pem."""

    def __init__(self, folder: Path):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        folder.mkdir(parents=True, exist_ok=True)
        pem = folder / "vapid.pem"
        if pem.exists():
            self.key = serialization.load_pem_private_key(pem.read_bytes(), password=None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem.write_bytes(self.key.private_bytes(serialization.Encoding.PEM,
                                                   serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            os.chmod(pem, 0o600)

    @property
    def public_key(self) -> str:
        """For the phone's subscribe() (applicationServerKey)."""
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        return b64u(self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))

    def header(self, endpoint: str, now: float | None = None) -> str:
        """`Authorization: vapid t=<jwt>, k=<key>` for one push service."""
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        u = urlparse(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int((now or time.time()) + 12 * 3600),
                  "sub": CONTACT}
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps(claims, separators=(",", ":")).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())))
        jwt = f"{head}.{body}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        return f"vapid t={jwt}, k={self.public_key}"


# --------------------------------------------------------------------------- #
# who gets what
# --------------------------------------------------------------------------- #
class PushStore:
    """Subscriptions (one per phone/browser) with their user and preferences,
    in data_store/push/subscriptions.json."""

    def __init__(self, root: Path = ROOT):
        self.dir = Path(root) / "data_store" / "push"
        self.file = self.dir / "subscriptions.json"
        self._lock = threading.Lock()
        self._vapid = None

    @property
    def vapid(self) -> Vapid:
        if self._vapid is None:
            self._vapid = Vapid(self.dir)
        return self._vapid

    def all(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.chmod(tmp, 0o600)
        tmp.replace(self.file)

    def add(self, user: str, sub: dict, prefs: dict | None = None) -> None:
        endpoint = str(sub.get("endpoint") or "")
        keys = sub.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("suscripción inválida")
        with self._lock:
            data = self.all()
            old = data.get(endpoint, {})
            data[endpoint] = {"user": user, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                              "prefs": clean_prefs(prefs or old.get("prefs")),
                              "since": old.get("since") or time.strftime("%Y-%m-%d")}
            self._write(data)

    def remove(self, endpoint: str) -> bool:
        with self._lock:
            data = self.all()
            if data.pop(endpoint, None) is None:
                return False
            self._write(data)
            return True

    def set_prefs(self, user: str, prefs: dict) -> int:
        """The same preferences on every phone of `user`; how many."""
        with self._lock:
            data = self.all()
            n = 0
            for d in data.values():
                if d.get("user") == user:
                    d["prefs"] = clean_prefs(prefs)
                    n += 1
            if n:
                self._write(data)
            return n

    def of(self, user: str) -> list[dict]:
        return [{"endpoint": e, **d} for e, d in self.all().items() if d.get("user") == user]


def clean_prefs(prefs: dict | None) -> dict:
    p = dict(DEFAULT_PREFS)
    for k in CATEGORIES:
        if isinstance(prefs, dict) and k in prefs:
            p[k] = bool(prefs[k])
    return p


def send_one(store: PushStore, endpoint: str, keys: dict, message: dict, ttl: int = 3600,
             opener=urllib.request.urlopen) -> int:
    """POST one encrypted message; the push service's HTTP status (0 = no answer)."""
    body = encrypt(json.dumps(message).encode(), keys["p256dh"], keys["auth"])
    req = urllib.request.Request(endpoint, data=body, method="POST", headers={
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        "TTL": str(ttl), "Urgency": "high", "Authorization": store.vapid.header(endpoint)})
    try:
        with opener(req, timeout=10) as res:
            return res.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return 0


def send(store: PushStore, message: dict, category: str, *, roles=None, user: str | None = None,
         opener=urllib.request.urlopen) -> int:
    """Send `message` ({title, body, tag, url}) to every phone that wants
    `category` (and whose user has one of `roles`, or is `user`). Returns how
    many got it; phones that are gone are forgotten."""
    sent = 0
    for endpoint, d in store.all().items():
        if user is not None and d.get("user") != user:
            continue
        if user is None and not d.get("prefs", {}).get(category, DEFAULT_PREFS.get(category)):
            continue
        if user is None and roles is not None and roles(d.get("user")) not in ("owner", "viewer"):
            continue
        status = send_one(store, endpoint, d["keys"], message, opener=opener)
        if status in (404, 410):
            store.remove(endpoint)
        elif 200 <= status < 300:
            sent += 1
        else:
            log.warning("push not delivered (HTTP %s)", status)
    return sent


# --------------------------------------------------------------------------- #
# from the bots' events to a notification
# --------------------------------------------------------------------------- #
_MD = re.compile(r"\*\*|__|`|(?<!\w)_|_(?!\w)")


def plain(text: str) -> str:
    return _MD.sub("", str(text or "")).strip()


def category_of(event: OutputEvent) -> str | None:
    """Which preference an event belongs to; None = not a notification."""
    k = event.kind
    if k in ("trade_opened", "trade_closed", "trade_tp1", "stop_moved", "trade_idea", "day_done"):
        return "trades"
    if k == "daily_record":
        return "daily"
    if k == "note":
        key = event.dedup_key.split(":", 1)[1] if ":" in event.dedup_key else ""
        head = key.split(":", 1)[0]
        if head in ("setup", "switch"):
            return "setups"
        if head in ("verse", "fact"):
            return "inspiration"
        if head == "briefing":
            return "daily"
        if head in ("open", "close", "be", "box", "none", "missed"):
            return "trades"                     # ORBIT itself: Set up 2
    return None


def message_of(event: OutputEvent) -> dict:
    """{title, body, tag, url}: the same words as the Discord message."""
    from .discord import build_discord_payload
    if event.kind == "daily_record":
        p = event.status_payload or {}
        lines = [plain(x) for x in str(p.get("text", "")).splitlines() if x.strip()]
        return {"title": f"🔔 Cierre del día · {p.get('day', '')}", "body": "\n".join(lines[:4]),
                "tag": event.dedup_key, "url": "/"}
    emb = (build_discord_payload(event).get("embeds") or [{}])[0]
    body = plain(emb.get("description") or "")
    return {"title": plain(emb.get("title") or event.title)[:120], "body": body[:400],
            "tag": event.dedup_key, "url": "/?ver=bots" if category_of(event) == "setups" else "/"}


class PushOutputAdapter(OutputAdapter):
    """Sends the bots' events as push notifications (in the background)."""
    name = "push"

    def __init__(self, store: PushStore | None = None, roles=None, sync: bool = False):
        self.store = store or PushStore()
        self.roles = roles or _account_roles(self.store)
        self.sync = sync
        self._sent: set[str] = set()

    def emit(self, event: OutputEvent) -> bool:
        try:
            from .discord import worth_sending
            cat = category_of(event) if worth_sending(event) else None   # same as Discord
            if cat is None or event.dedup_key in self._sent or not self.store.all():
                return False
            self._sent.add(event.dedup_key)
            msg = message_of(event)
            job = lambda: send(self.store, msg, cat, roles=self.roles)        # noqa: E731
            if self.sync:
                job()
            else:
                threading.Thread(target=job, name="push", daemon=True).start()
            return True
        except Exception as exc:  # noqa: BLE001 - a notification never breaks a bot
            log.warning("push failed (%s)", type(exc).__name__)
            return False


def _account_roles(store: PushStore):
    """user -> role, read fresh each time (a user removed gets nothing)."""
    from ..api.accounts import Accounts
    acc = Accounts(store.dir.parent.parent)

    def role(user: str | None) -> str | None:
        if user == "dueño":                      # the DASHBOARD_TOKEN owner
            return "owner"
        return (acc.users().get(user or "") or {}).get("role")
    return role


def push_enabled() -> bool:
    return os.environ.get("BOT_PUSH", "on").strip().lower() not in ("off", "0", "false")
