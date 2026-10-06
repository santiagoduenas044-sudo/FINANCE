"""Push notifications: encrypted per phone (RFC 8291), signed by the server
(VAPID), sent only to who asked for that kind of news."""
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from signal_engine.output import webpush as wp
from signal_engine.output.events import OutputEvent


def _phone():
    """A phone's subscription keys (what the browser makes)."""
    key = ec.generate_private_key(ec.SECP256R1())
    pub = key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    return key, {"p256dh": wp.b64u(pub), "auth": wp.b64u(auth)}


def _decrypt(body: bytes, key, keys: dict) -> bytes:
    """What the phone does with the message (RFC 8291, receiver side)."""
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_pub = body[21:21 + idlen]
    ct = body[21 + idlen:]
    secret = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ua_pub = wp.unb64u(keys["p256dh"])
    prk_key = wp._hkdf_extract(wp.unb64u(keys["auth"]), secret)
    ikm = wp._hkdf_expand(prk_key, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    prk = wp._hkdf_extract(salt, ikm)
    cek = wp._hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = wp._hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)
    assert rs == 4096
    plain = AESGCM(cek).decrypt(nonce, ct, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


def test_only_that_phone_can_read_the_message():
    key, keys = _phone()
    body = wp.encrypt(b'{"title":"Set up 3"}', keys["p256dh"], keys["auth"])
    assert b"Set up" not in body
    assert _decrypt(body, key, keys) == b'{"title":"Set up 3"}'
    other, _ = _phone()
    with pytest.raises(Exception):
        _decrypt(body, other, keys)


def test_vapid_header_is_signed_by_the_server(tmp_path):
    v = wp.Vapid(tmp_path)
    assert wp.Vapid(tmp_path).public_key == v.public_key           # kept on disk
    h = v.header("https://web.push.apple.com/abc", now=1_000_000)
    t, k = h[len("vapid t="):].split(", k=")
    head, body, sig = t.split(".")
    claims = json.loads(wp.unb64u(body))
    assert claims["aud"] == "https://web.push.apple.com" and claims["exp"] == 1_000_000 + 12 * 3600
    raw = wp.unb64u(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), wp.unb64u(k))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))


class _Res:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_send_respects_preferences_roles_and_forgets_gone_phones(tmp_path):
    store = wp.PushStore(tmp_path)
    keys = {k: _phone()[1] for k in ("a", "b", "c", "gone")}
    store.add("Rickybobby", {"endpoint": "https://push.example/a", "keys": keys["a"]})
    store.add("Rickybobby", {"endpoint": "https://push.example/b", "keys": keys["b"]},
              {"setups": False})
    store.add("amigo", {"endpoint": "https://push.example/c", "keys": keys["c"]})       # practice user
    store.add("Rickybobby", {"endpoint": "https://push.example/gone", "keys": keys["gone"]})
    hits = []

    def opener(req, timeout):
        hits.append(req.full_url)
        assert req.headers["Content-encoding"] == "aes128gcm" and req.headers["Authorization"].startswith("vapid t=")
        return _Res(410 if req.full_url.endswith("gone") else 201)

    roles = {"Rickybobby": "owner", "amigo": "practica"}.get
    n = wp.send(store, {"title": "x"}, "setups", roles=roles, opener=opener)
    assert n == 1 and sorted(hits) == ["https://push.example/a", "https://push.example/gone"]
    assert "https://push.example/gone" not in store.all()                           # forgotten
    hits.clear()
    assert wp.send(store, {"title": "x"}, "trades", roles=roles, opener=opener) == 2  # a and b
    with pytest.raises(ValueError):
        store.add("x", {"endpoint": "http://not-https", "keys": keys["a"]})


def test_which_events_become_notifications():
    note = lambda key: OutputEvent.from_note("t", "x", 1, key)                      # noqa: E731
    assert wp.category_of(note("setup:zonas_1m:s1:open")) == "setups"
    assert wp.category_of(note("setup:apertura_30m:open:APERTURA-2026-10-06")) == "setups"
    assert wp.category_of(note("open:APERTURA-2026-10-06")) == "trades"             # ORBIT itself
    assert wp.category_of(note("verse:2026-10-06")) == "inspiration"
    assert wp.category_of(note("online:123")) is None
    assert wp.category_of(OutputEvent.from_daily_record("**Hoy** GANARON +$290", "2026-10-06")) == "daily"
    m = wp.message_of(OutputEvent.from_daily_record("**Hoy los bots GANARON +$290**\n\n👻 **GHOST**", "2026-10-06"))
    assert m["title"].startswith("🔔 Cierre del día") and "**" not in m["body"] and "GANARON +$290" in m["body"]
    m = wp.message_of(note("setup:zonas_1m:s1:open"))
    assert m["title"] == "t" and m["url"] == "/?ver=bots"


def test_adapter_sends_once_per_event(tmp_path):
    store = wp.PushStore(tmp_path)
    store.add("Rickybobby", {"endpoint": "https://push.example/a", "keys": _phone()[1]})
    sent = []
    a = wp.PushOutputAdapter(store, roles=lambda u: "owner", sync=True)
    wp_send = wp.send
    try:
        wp.send = lambda st, msg, cat, roles=None: sent.append((msg["title"], cat)) or 1
        ev = OutputEvent.from_note("🟢 Set up 3 · IMÁN 1 — COMPRA NQ", "x", 1, "setup:zonas_1m:s1:open")
        assert a.emit(ev) and not a.emit(ev)
        assert not a.emit(OutputEvent.from_note("en línea", "x", 1, "online:1"))
    finally:
        wp.send = wp_send
    assert sent == [("🟢 Set up 3 · IMÁN 1 — COMPRA NQ", "setups")]


def test_matches_the_rfc_8291_example():
    """The worked example of RFC 8291 (Appendix A), byte for byte."""
    as_priv = ec.derive_private_key(
        int.from_bytes(wp.unb64u("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
    body = wp.encrypt(b"When I grow up, I want to be a watermelon",
                      "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                      "BTBZMqHH6r4Tts7J_aSIgg", salt=wp.unb64u("DGv6ra1nlYgDCS1FRnbzlw"), server_key=as_priv)
    assert wp.b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6"
        "TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_a_viewer_turns_notifications_on_and_off_for_themself(tmp_path, monkeypatch):
    from tests.test_web_login import _client
    c, acc = _client(tmp_path, monkeypatch)
    acc.add("santi", "clave-segura-1", "owner")
    acc.add("amigo", "clave-amigo-1", "viewer")
    assert c.get("/api/push").status_code == 401
    c.post("/api/login", json={"user": "amigo", "password": "clave-amigo-1"})
    d = c.get("/api/push").json()
    assert len(wp.unb64u(d["key"])) == 65 and d["phones"] == 0
    sub = {"endpoint": "https://push.example/amigo", "keys": _phone()[1]}
    r = c.post("/api/push/subscribe", json={"subscription": sub, "prefs": {"inspiration": True}})
    assert r.status_code == 200 and r.json()["phones"] == 1
    assert c.get("/api/push").json()["prefs"]["inspiration"] is True
    assert c.post("/api/push/prefs", json={"prefs": {"setups": False}}).json()["phones"] == 1
    assert c.get("/api/push").json()["prefs"]["setups"] is False
    assert c.post("/api/push/subscribe", json={"subscription": {"endpoint": "x"}}).status_code == 400
    assert c.post("/api/control/start").status_code == 403          # still read-only otherwise
    assert c.post("/api/push/unsubscribe", json={"endpoint": sub["endpoint"]}).json()["phones"] == 0
